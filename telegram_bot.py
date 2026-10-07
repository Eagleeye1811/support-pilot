"""Real Telegram channel: long-polls the Bot API and runs every message through the agents.

Long polling needs no public webhook URL, so it runs inside the Streamlit app (one
background thread per server process) or standalone with `python telegram_bot.py`.
Each Telegram chat maps to one SupportPilot conversation.

- Text and photos in (photos are stored as evidence and attached to the ticket).
- Replies out, with product images chosen by the Media Agent and ⭐ buttons for CSAT.
- After a ticket is created the bot offers an email copy; the address is used by the Email Agent.
- Ticket updates made by a human agent on the website reach the chat (and the email, if given).
Every turn is written to the shared ``turns`` log, which the website replays live.
"""
from __future__ import annotations

import json
import re
import threading
import time

import requests

import agents as A
from agents.email_agent import EMAIL_RE, mask
from agents.mcp_bus import get_bus
from agents.media import product_image_bytes, save_attachment
from agents.orchestrator_agent import Tracer
from agents.shared_agent import get_secret, iso
from agents.turns import record_turn

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
POLL_TIMEOUT = 10  # seconds; also how often website ticket updates are pushed to Telegram
THREAD_NAME = "supportpilot-telegram"
CONFLICT_BACKOFF = 15  # seconds to wait when another poller holds the token (Telegram 409 Conflict)
DEMO_CUSTOMERS = ["CUST1001", "CUST1002", "CUST1003", "CUST1005", "CUST1006", "CUST1008", "CUST1009", "CUST1010", "CUST1012"]
EMAIL_OFFER = "📧 Want a copy of this ticket by email? Just reply with your email address (or say *skip*)."
HELP = ("Tell me your problem in your own words — orders, payments, refunds, login, subscriptions or internet. "
        "You can also send a photo of the item.\n\n"
        "/login — act as a demo customer (so I can see orders and accounts)\n"
        "/guest — continue without an account\n"
        "/email you@example.com — get ticket updates by email\n"
        "/new — start a fresh conversation\n"
        "/help — show this message")


def get_token():
    return get_secret("TELEGRAM_BOT_TOKEN")


def decision_of(r):
    d = r.get("decision")
    return d["decision"] if isinstance(d, dict) else d


class TelegramBot:
    def __init__(self, store, token, use_llm=True):
        self.store, self.token, self.use_llm = store, token, use_llm
        self.http = requests.Session()
        self.started = iso()
        self.username = None
        self._stop = threading.Event()
        self._groups = {}  # media_group_id → ticket id, so a photo album becomes one case

    # ---- Bot API ---------------------------------------------------------------------------
    def call(self, method, http_timeout=15, files=None, **params):
        url = API.format(token=self.token, method=method)
        if files:  # multipart upload: nested values must be JSON strings
            data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
            r = self.http.post(url, data=data, files=files, timeout=http_timeout + 30)
        else:
            r = self.http.post(url, json=params, timeout=http_timeout)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description', r.status_code)}")
        return data["result"]

    def download(self, file_id):
        path = self.call("getFile", file_id=file_id)["file_path"]
        r = self.http.get(FILE_API.format(token=self.token, path=path), timeout=30)
        r.raise_for_status()
        return r.content

    def send(self, chat_id, text, buttons=None):
        params = dict(chat_id=chat_id, text=text[:4000])
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:  # replies use *bold*; fall back to plain text if Telegram rejects the markup
            return self.call("sendMessage", parse_mode="Markdown", **params)
        except RuntimeError:
            return self.call("sendMessage", **params)

    def send_products(self, chat_id, media):
        """Send the Media Agent's product images (an album for ordered-vs-received)."""
        photos = [(m, product_image_bytes(m["sku"])) for m in media]
        photos = [(m, raw) for m, raw in photos if raw]
        if not photos:
            return None
        if len(photos) == 1:
            m, raw = photos[0]
            return self.call("sendPhoto", files={"photo": (f"{m['sku']}.png", raw, "image/png")},
                             chat_id=chat_id, caption=m["caption"])
        files = {m["sku"]: (f"{m['sku']}.png", raw, "image/png") for m, raw in photos}
        album = [dict(type="photo", media=f"attach://{m['sku']}", caption=m["caption"]) for m, _ in photos]
        return self.call("sendMediaGroup", files=files, chat_id=chat_id, media=album)

    def status(self, **fields):
        doc = self.store.get("meta", "telegram") or {}
        doc.update(fields, id="telegram", updated=iso())
        self.store.put("meta", "telegram", doc)

    # ---- chats -----------------------------------------------------------------------------
    def chat(self, chat_id, sender=None):
        doc = self.store.get("telegram_chats", chat_id)
        if doc is None:
            doc = dict(id=str(chat_id), chat_id=chat_id, conv_id=None, customer_id=None, contact_email=None,
                       awaiting_email=False, last_ticket=None, created_at=iso())
        if sender:
            doc.update(first_name=sender.get("first_name") or "Telegram user", username=sender.get("username"))
        return doc

    def save_chat(self, doc):
        doc["updated_at"] = iso()
        self.store.put("telegram_chats", doc["id"], doc)

    def login_buttons(self):
        rows = []
        for cid in DEMO_CUSTOMERS:
            c = self.store.get("customers", cid)
            if c:
                rows.append([{"text": f"{c['name']} · {c.get('tier', '')}", "callback_data": f"login:{cid}"}])
        rows.append([{"text": "Continue as guest", "callback_data": "login:guest"}])
        return rows

    def set_customer(self, doc, cid):
        doc.update(customer_id=None if cid == "guest" else cid, conv_id=None, awaiting_email=False)
        self.save_chat(doc)
        if cid == "guest":
            return "You're chatting as a guest. Ask any question — or share an order ID, email or phone so I can find your account."
        c = self.store.get("customers", cid) or {}
        return f"You're now {c.get('name', cid)} ({c.get('tier', 'customer')}). Describe the problem in your own words."

    # ---- message handling --------------------------------------------------------------------
    def handle_update(self, upd):
        if "callback_query" in upd:
            return self.handle_callback(upd["callback_query"])
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return None
        doc = self.chat(msg["chat"]["id"], msg.get("from"))
        if msg.get("photo"):
            return self.handle_photo(doc, msg)
        if not msg.get("text"):
            return self.send(doc["chat_id"], "I can read text and photos — please type your issue or send a picture.")
        text = msg["text"].strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if cmd == "/start":
            self.save_chat(doc)
            return self.send(doc["chat_id"], f"Hi {doc['first_name']}! 👋 I'm the SupportPilot assistant.\n\n" + HELP +
                             "\n\nThis is a demo with sample customers — pick one to try account issues:", self.login_buttons())
        if cmd == "/login":
            return self.send(doc["chat_id"], "Who do you want to be?", self.login_buttons())
        if cmd == "/guest":
            return self.send(doc["chat_id"], self.set_customer(doc, "guest"))
        if cmd == "/new":
            doc.update(conv_id=None, awaiting_email=False)
            self.save_chat(doc)
            return self.send(doc["chat_id"], "Started a new conversation. What can I help with?")
        if cmd == "/email":
            return self.capture_email(doc, text[len("/email"):].strip())
        if cmd:
            return self.send(doc["chat_id"], HELP)
        if doc.get("awaiting_email"):
            if re.fullmatch(r"(skip|no|nope|no thanks|not now)[.!]?", text, re.I):
                doc["awaiting_email"] = False
                self.save_chat(doc)
                return self.send(doc["chat_id"], "No problem — I'll keep you updated here on Telegram.")
            if EMAIL_RE.match(text):
                return self.capture_email(doc, text)
            doc["awaiting_email"] = False  # they moved on to something else
        return self.run_turn(doc, text, msg)

    def handle_callback(self, cq):
        data, chat_id = cq.get("data", ""), cq["message"]["chat"]["id"]
        self.call("answerCallbackQuery", callback_query_id=cq["id"])
        doc = self.chat(chat_id, cq.get("from"))
        if data.startswith("login:"):
            return self.send(chat_id, self.set_customer(doc, data.split(":", 1)[1]))
        if data.startswith("csat:"):
            return self.run_turn(doc, data.split(":", 1)[1], None)
        return None

    def handle_photo(self, doc, msg):
        best = min(msg["photo"], key=lambda p: abs(p.get("width", 0) - 800))  # ~800px is plenty for evidence
        try:
            att = save_attachment(self.store, self.download(best["file_id"]), "image/jpeg", "telegram", msg.get("caption", ""))
        except Exception as exc:
            return self.send(doc["chat_id"], f"Sorry, I couldn't receive that photo ({exc}). Please try again.")
        group = msg.get("media_group_id")
        if group and group in self._groups:  # the rest of an album: attach to the same case silently
            t = self.store.get("tickets", self._groups[group]) if self._groups[group] else None
            if t:
                t["attachments"] = (t.get("attachments") or []) + [att["id"]]
                self.store.put("tickets", t["id"], t)
            return None
        text = (msg.get("caption") or "").strip() or "Here is a photo of the item I received."
        r = self.run_turn(doc, text, msg, attachments=[att["id"]])
        if group:
            self._groups[group] = (r.get("ticket") or {}).get("id")
        return r

    def run_turn(self, doc, text, msg, attachments=None):
        self.call("sendChatAction", chat_id=doc["chat_id"], action="typing")
        payload = dict(message=dict(text=text, chat={"id": doc["chat_id"]},
                                    **{"from": (msg or {}).get("from") or {"username": doc.get("username")}}))
        before = iso()
        r = A.handle_message(self.store, conversation_id=doc["conv_id"], customer_id=doc["customer_id"], channel="telegram",
                             payload=payload, use_llm=self.use_llm, attachments=attachments,
                             contact_email=doc.get("contact_email"))
        conv = r.pop("conversation", {}) or {}
        doc["conv_id"] = r["conversation_id"]
        if conv.get("customer_id") and not doc.get("customer_id"):
            doc["customer_id"] = conv["customer_id"]  # identified from an order id / phone / email in the message
        reply = r.get("reply_channel") or r["reply"]
        if r.get("ticket"):
            doc["last_ticket"] = r["ticket"]["id"]
            if decision_of(r) in ("auto_resolve", "escalate") and not doc.get("contact_email") and not doc.get("email_offered"):
                reply += "\n\n" + EMAIL_OFFER  # offered once per chat; /email works any time
                doc.update(awaiting_email=True, email_offered=True)
        self.save_chat(doc)
        # notifications raised during this turn are already covered by the reply
        for n in self.store.list("notifications", where=lambda n: n.get("created_at", "") >= before):
            self.store.claim("telegram_sent", n["id"], dict(id=n["id"], ts=iso(), suppressed=True))
        record_turn(self.store, "telegram", text, r, chat_id=doc["chat_id"])
        buttons = [[{"text": "⭐" * i, "callback_data": f"csat:{i}"} for i in range(1, 6)]] \
            if r.get("awaiting") == "csat" else None
        self.send(doc["chat_id"], reply, buttons)
        if r.get("media"):
            try:
                self.send_products(doc["chat_id"], r["media"])
            except Exception as exc:  # images are a bonus; never lose the reply over them
                self.status(error=f"sending product images: {exc}")
        return r

    def capture_email(self, doc, address):
        address = address.strip().lower()
        if not EMAIL_RE.match(address):
            return self.send(doc["chat_id"], "That doesn't look like an email address — try again, e.g. /email you@gmail.com")
        doc.update(contact_email=address, awaiting_email=False)
        self.save_chat(doc)
        conv = self.store.get("conversations", doc["conv_id"]) if doc.get("conv_id") else None
        if conv:
            conv["contact_email"] = address
            self.store.put("conversations", conv["id"], conv)
        ticket = self.store.get("tickets", doc["last_ticket"]) if doc.get("last_ticket") else None
        if not ticket:
            return self.send(doc["chat_id"], f"Saved {mask(address)} — I'll email you whenever a ticket is created or updated.")
        rec = self.email_turn(doc, f"My email is {mask(address)}", ticket, kind="ticket")
        note = {"sent": f"📧 Sent the ticket details to {mask(address)}. You'll get updates there too.",
                "not_configured": "Saved your email, but email sending isn't set up on this demo yet.",
                "rate_limited": "Saved your email — I've sent several emails already, so I'll hold off for a bit.",
                "blocked": f"Saved your email, but I couldn't send to it ({rec.get('error', '')})."}
        return self.send(doc["chat_id"], note.get(rec["status"],
                                                  f"Saved your email, but sending failed ({rec.get('error', 'unknown error')})."))

    def email_turn(self, doc, text, ticket, kind="ticket", message=""):
        """Run just the Email Agent (over MCP) and log it as a turn so the website shows it."""
        T = Tracer(get_bus(self.store))
        T.note("Master Orchestrator (Planner)", "Email request → Email Agent")
        conv = self.store.get("conversations", ticket.get("conversation_id") or "") or {}
        last_reply = next((m["text"] for m in reversed(conv.get("messages", [])) if m["role"] == "assistant"), "")
        media = [s for m in conv.get("messages", []) for s in (m.get("meta") or {}).get("media", [])][-2:]
        ctx = A.customer_context(self.store, ticket.get("customer_id")) if ticket.get("customer_id") else None
        rec = T.tool("Email Agent", "send_email",
                     dict(to=doc["contact_email"], kind=kind, ticket_id=ticket["id"], reply=last_reply, message=message,
                          media=media, customer_name=(ctx or {}).get("first_name")),
                     lambda r: f"{r['status']} → {r['to_masked']}")
        record_turn(self.store, "telegram", text, dict(conversation_id=ticket.get("conversation_id"), channel="telegram",
                                                       ticket=ticket, email=rec, trace=T.steps, context=ctx, media=[],
                                                       decision="email",
                                                       reply=f"Email {rec['status'].replace('_', ' ')} → {rec['to_masked']}"),
                    chat_id=doc["chat_id"])
        return rec

    def deliver_notifications(self):
        """Send ticket updates made on the website (status changes, SLA escalations) to the customer's chat and email."""
        chats = {c["conv_id"]: c for c in self.store.list("telegram_chats") if c.get("conv_id")}
        for n in self.store.list("notifications", where=lambda n: n.get("created_at", "") >= self.started and n.get("ticket_id")):
            if self.store.get("telegram_sent", n["id"]):
                continue
            t = self.store.get("tickets", n["ticket_id"]) or {}
            chat = chats.get(t.get("conversation_id"))
            # claim before sending, so a second poller (or a retry) can never deliver the same update twice
            if not self.store.claim("telegram_sent", n["id"], dict(id=n["id"], ts=iso(), delivered=bool(chat))):
                continue
            if chat:
                self.send(chat["chat_id"], f"🔔 Update on {n['ticket_id']}: {n['message']}")
                if chat.get("contact_email"):
                    self.email_turn(chat, f"Ticket {n['ticket_id']} updated on the website", t, kind="update",
                                    message=n["message"])

    # ---- loop ----------------------------------------------------------------------------------
    def run(self):
        offset = None
        try:
            me = self.call("getMe")
            self.username = me.get("username")
            self.call("deleteWebhook")  # getUpdates does not work while a webhook is set
            self.status(running=True, username=self.username, started_at=self.started, error=None)
        except Exception as exc:
            self.status(running=False, error=f"Could not connect: {exc}")
            return
        while not self._stop.is_set():
            try:
                updates = self.call("getUpdates", http_timeout=POLL_TIMEOUT + 10, **self._poll_params(offset))
                for upd in updates:
                    offset = upd["update_id"] + 1
                    if self._stop.is_set():
                        break
                    self.process(upd)
                self.deliver_notifications()
                self.status(running=True, last_poll=iso(), error=None)
            except Exception as exc:
                if "Conflict" in str(exc):  # another poller holds the token: wait it out instead of fighting
                    self.status(running=True, last_poll=iso(),
                                error="Another copy of this bot is polling the same token — waiting for it to stop")
                    self._stop.wait(CONFLICT_BACKOFF)
                else:
                    self.status(running=True, error=f"{type(exc).__name__}: {exc}", last_poll=iso())
                    self._stop.wait(5)
        if offset is not None:  # tell Telegram what we handled, so the next poller does not get it again
            try:
                self.call("getUpdates", offset=offset, timeout=0)
            except Exception:
                pass

    def process(self, upd):
        """Handle one update exactly once, even if two pollers or a restart see it again."""
        if not self.store.claim("telegram_updates", upd["update_id"], dict(ts=iso())):
            return
        try:
            self.handle_update(upd)
        except Exception as exc:  # one bad message must not stop the bot
            self.status(error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _poll_params(offset):
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "edited_message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return params

    def stop(self):
        self._stop.set()


def _running_thread(token):
    """The live bot thread for this token. Found via threading (not a module global), so it survives
    Streamlit hot-reloading this module when new code is deployed."""
    for t in threading.enumerate():
        if t.name == THREAD_NAME and t.is_alive() and getattr(t, "bot_token", None) == token:
            return t
    return None


def _legacy_threads(token):
    """Bot threads started by earlier versions of this file (named "telegram-bot", no attributes)."""
    out = []
    for t in threading.enumerate():
        bot = getattr(getattr(t, "_target", None), "__self__", None)
        if t.name == "telegram-bot" and t.is_alive() and getattr(bot, "token", None) == token and hasattr(bot, "stop"):
            out.append(t)
    return out


def start_in_background(store, token=None, use_llm=True):
    """Start the bot on a daemon thread; returns the bot, or None when no token is configured.

    Exactly one poller per process: an existing bot running this code is reused; one running older code
    (left behind by a hot reload) is asked to stop and the new bot starts polling once it has exited.
    """
    token = token or get_token()
    if not token:
        return None
    old = _running_thread(token)
    if old is not None and type(old.bot) is TelegramBot and not old.bot._stop.is_set():
        return old.bot
    retiring = ([old] if old is not None else []) + _legacy_threads(token)
    for t in retiring:
        t.bot.stop() if hasattr(t, "bot") else t._target.__self__.stop()
    bot = TelegramBot(store, token, use_llm)

    def run():
        for t in retiring:  # let previous pollers finish their last long-poll and hand over
            t.join(POLL_TIMEOUT + 25)
        bot.run()

    thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
    thread.bot_token, thread.bot = token, bot
    thread.start()
    return bot


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
    b = TelegramBot(A.Store(), tok, use_llm=A.llm_enabled())
    print("SupportPilot Telegram bot running — press Ctrl+C to stop.")
    b.run()
