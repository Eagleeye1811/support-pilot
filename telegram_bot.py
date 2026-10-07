"""Real Telegram channel: long-polls the Bot API and runs every message through the agents.

Long polling needs no public webhook URL, so it runs inside the Streamlit app (one
background thread per server process) or standalone with `python telegram_bot.py`.
Each Telegram chat maps to one SupportPilot conversation; every turn is stored so the
Streamlit "Telegram live" tab can replay how the agents handled it. Ticket updates made
by a human agent on the website are delivered back to the customer's Telegram chat.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

import agents as A
from agents.email_agent import EMAIL_RE, mask, send_email
from agents.media import product_image_bytes, save_attachment, ticket_images
from telegram_wrong_item import WrongItemFlow
from agents.shared_agent import _load_env_file, iso

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
# "send me a confirmation mail", "can you email me the details", "mail me"...
EMAIL_REQUEST = re.compile(r"\b(e-?mail|mail)\b.*\b(send|confirm|confirmation|copy|details|me)\b|"
                           r"\b(send|confirm|confirmation)\b.*\b(e-?mail|mail)\b|^\s*(e-?mail|mail) me\b", re.I)
# "send it", "yes please", "ok go ahead" while we wait for an email address
GO_AHEAD = re.compile(r"^\s*(yes|yeah|yep|sure|ok|okay|please|pls|go ahead|do it|send( it| me| please)?|"
                      r"give( me)?( it)?|share( it)?)\b", re.I)
EMAIL_FIND = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
POLL_TIMEOUT = 10  # seconds; also how often website ticket updates are pushed to Telegram
THREAD_NAME = "supportpilot-telegram"
OLD_THREAD_NAMES = {THREAD_NAME, "telegram-bot"}  # names used by earlier versions of this file
CONFLICT_BACKOFF = 15  # seconds to wait when another poller holds the token (Telegram 409 Conflict)
EMAIL_OFFER = "📧 Want a copy of this ticket by email? Just reply with your email address (or say *skip*)."
DEMO_CUSTOMERS = ["CUST1001", "CUST1002", "CUST1003", "CUST1005", "CUST1006", "CUST1008", "CUST1009", "CUST1010", "CUST1012"]
HELP = ("Tell me your problem in your own words — orders, payments, refunds, login, subscriptions or internet.\n\n"
        "/login — act as a demo customer (so I can see orders and accounts)\n"
        "/guest — continue without an account\n"
        "/email you@example.com — get ticket emails and updates\n"
        "/new — start a fresh conversation\n"
        "/help — show this message")


def get_token():
    _load_env_file()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "your_token_here":
        try:
            import streamlit as st
            token = str(st.secrets.get("TELEGRAM_BOT_TOKEN", "")).strip()
        except Exception:
            token = ""
    return token if token and token != "your_token_here" else None


class TelegramBot(WrongItemFlow):
    def __init__(self, store, token, use_llm=True):
        self.store, self.token, self.use_llm = store, token, use_llm
        self.http = requests.Session()
        self.started = iso()
        self.username = None
        self._stop = threading.Event()

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

    def send(self, chat_id, text, buttons=None):
        params = dict(chat_id=chat_id, text=text[:4000])
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:  # replies use *bold*; fall back to plain text if Telegram rejects the markup
            return self.call("sendMessage", parse_mode="Markdown", **params)
        except RuntimeError:
            return self.call("sendMessage", **params)

    def send_photo(self, chat_id, sku, caption, buttons=None):
        """A product picture with a caption (and optional buttons)."""
        params = dict(chat_id=chat_id, caption=caption[:1000])
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        files = {"photo": (f"{sku}.png", product_image_bytes(sku), "image/png")}
        try:
            return self.call("sendPhoto", files=files, parse_mode="Markdown", **params)
        except RuntimeError:
            return self.call("sendPhoto", files=files, **params)

    def download(self, file_id):
        path = self.call("getFile", file_id=file_id)["file_path"]
        r = self.http.get(FILE_API.format(token=self.token, path=path), timeout=30)
        r.raise_for_status()
        return r.content

    def status(self, **fields):
        doc = self.store.get("meta", "telegram") or {}
        doc.update(fields, id="telegram", updated=iso())
        self.store.put("meta", "telegram", doc)

    # ---- chats -----------------------------------------------------------------------------
    def chat(self, chat_id, sender=None):
        doc = self.store.get("telegram_chats", chat_id)
        if doc is None:
            doc = dict(id=str(chat_id), chat_id=chat_id, conv_id=None, customer_id=None, created_at=iso())
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
        doc.update(customer_id=None if cid == "guest" else cid, conv_id=None)
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
            doc.pop("flow", None)
            self.save_chat(doc)
            return self.send(doc["chat_id"], "Started a new conversation. What can I help with?")
        if cmd == "/email":
            return self.capture_email(doc, text[len("/email"):].strip())
        if cmd:
            return self.send(doc["chat_id"], HELP)
        found = EMAIL_FIND.search(text)
        if doc.get("awaiting_email"):
            if re.fullmatch(r"(skip|no|nope|no thanks|not now)[.!]?", text, re.I):
                return self.skip_email(doc)
            if found:  # "my email is asha@gmail.com" works as well as the bare address
                return self.capture_email(doc, found.group(0))
            if doc.get("contact_email") and GO_AHEAD.search(text):
                return self.capture_email(doc, doc["contact_email"])
            if GO_AHEAD.search(text) or EMAIL_REQUEST.search(text):  # "send it", "yes please" — still need the address
                return self.send(doc["chat_id"], "Happy to! ✉️ Just type the email address you'd like it sent to "
                                                 "(for example: name@gmail.com).", [[{"text": "No thanks", "callback_data": "email:skip"}]])
            doc["awaiting_email"] = False  # they moved on to something else
        if EMAIL_REQUEST.search(text) or (found and doc.get("last_ticket") and len(text.split()) <= 8):
            return self.email_request(doc, text, found.group(0) if found else None)
        if doc.get("flow") and self.wi_continue(doc, text=text):
            return None
        u = A.understand(text, use_llm=False)
        if u["intent"] == "wrong_item":  # guided, step-by-step conversation instead of a one-shot answer
            return self.wi_start(doc, text, u)
        return self.run_turn(doc, text, msg)

    def handle_photo(self, doc, msg):
        best = min(msg["photo"], key=lambda p: abs(p.get("width", 0) - 800))  # ~800px is plenty for evidence
        caption = (msg.get("caption") or "").strip()
        try:
            att = save_attachment(self.store, self.download(best["file_id"]), "image/jpeg", "telegram", caption)
        except Exception as exc:
            return self.send(doc["chat_id"], f"Sorry, I couldn't receive that photo ({exc}). Please try again.")
        if doc.get("flow") and doc["flow"].get("step") == "photo":
            return self.wi_continue(doc, text=caption, photo_att=att["id"])
        if caption and A.understand(caption, use_llm=False)["intent"] == "wrong_item":
            doc.update(pending_photo=att["id"], pending_caption=caption)
            return self.wi_start(doc, caption, A.understand(caption, use_llm=False))
        doc.update(pending_photo=att["id"], pending_caption=caption)  # keep it for when we know what the issue is
        self.save_chat(doc)
        return self.send(doc["chat_id"], "Thanks for the photo 📸 What went wrong with this item? "
                                         "(For example: “I received the wrong item”.)")

    def handle_callback(self, cq):
        data, chat_id = cq.get("data", ""), cq["message"]["chat"]["id"]
        self.call("answerCallbackQuery", callback_query_id=cq["id"])
        doc = self.chat(chat_id, cq.get("from"))
        if data.startswith("login:"):
            return self.send(chat_id, self.set_customer(doc, data.split(":", 1)[1]))
        if data.startswith("csat:"):
            return self.run_turn(doc, data.split(":", 1)[1], None)
        if data == "email:skip":
            return self.skip_email(doc)
        if data.startswith("wi:") and doc.get("flow"):
            return self.wi_continue(doc, data=data)
        return None

    def run_turn(self, doc, text, msg, attachments=None, defer_csat=False, offer_email=True):
        self.call("sendChatAction", chat_id=doc["chat_id"], action="typing")
        payload = dict(message=dict(text=text, chat={"id": doc["chat_id"]},
                                    **{"from": (msg or {}).get("from") or {"username": doc.get("username")}}))
        before = iso()
        r = A.handle_message(self.store, conversation_id=doc["conv_id"], customer_id=doc["customer_id"], channel="telegram",
                             payload=payload, use_llm=self.use_llm, contact_email=doc.get("contact_email"),
                             attachments=attachments, defer_csat=defer_csat)
        conv = r.pop("conversation", {}) or {}
        doc["conv_id"] = r["conversation_id"]
        if conv.get("customer_id") and not doc.get("customer_id"):
            doc["customer_id"] = conv["customer_id"]  # identified from an order id / phone / email in the message
        reply = r.get("reply_channel") or r["reply"]
        if r.get("ticket"):
            doc["last_ticket"] = r["ticket"]["id"]
            if offer_email and not doc.get("contact_email") and not doc.get("email_offered"):
                reply += "\n\n" + EMAIL_OFFER  # offered once per chat; /email works any time
                doc.update(awaiting_email=True, email_offered=True)
        self.save_chat(doc)
        # notifications raised during this turn are already covered by the reply
        for n in self.store.list("notifications", where=lambda n: n.get("created_at", "") >= before):
            self.store.claim("telegram_sent", n["id"], dict(id=n["id"], ts=iso(), suppressed=True))
        self.record(doc, text, r)
        buttons = [[{"text": "⭐" * i, "callback_data": f"csat:{i}"} for i in range(1, 6)]] \
            if r.get("awaiting") == "csat" and not defer_csat else None
        self.send(doc["chat_id"], reply, buttons)
        return r

    def record(self, doc, text, result):
        """Store the turn so the website's Telegram live tab can replay it."""
        seq = self.store.next_seq("telegram_turn", 1)
        self.store.put("telegram_turns", f"{seq:06d}", dict(id=f"{seq:06d}", seq=seq, chat_id=doc["chat_id"],
                                                             conv_id=doc.get("conv_id"), text=text, ts=iso(), result=result))

    # ---- email -----------------------------------------------------------------------------------
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
        ticket = self.store.get("tickets", doc["last_ticket"]) if doc.get("last_ticket") and not doc.get("flow") else None
        if not ticket:  # nothing to send yet (or a new case is in progress): the confirmation goes out with the ticket
            return self.send(doc["chat_id"], f"Saved {mask(address)} — I'll email you the confirmation as soon as your "
                                             "ticket is created, and updates after that.")
        rec = self.email(doc, ticket, "ticket", text=f"My email is {mask(address)}")
        note = {"sent": f"📧 Done — the confirmation for {ticket['id']} is on its way to {mask(address)}. "
                        "You'll get updates there too.",
                "not_configured": "Saved your email, but email sending isn't set up on this demo yet.",
                "rate_limited": "Saved your email — I've sent several emails already, so I'll hold off for a bit.",
                "blocked": f"Saved your email, but I couldn't send to it ({rec.get('error', '')})."}
        self.send(doc["chat_id"], note.get(rec["status"], f"Saved your email, but sending failed ({rec.get('error', 'unknown error')})."))
        return self.ask_rating(doc)

    def skip_email(self, doc):
        doc["awaiting_email"] = False
        self.save_chat(doc)
        self.send(doc["chat_id"], "No problem — I'll keep you updated here on Telegram.")
        return self.ask_rating(doc)

    def email_request(self, doc, text, address=None):
        """The customer asked for a confirmation mail in their own words — at any point in the chat."""
        if address:
            return self.capture_email(doc, address)
        if not doc.get("last_ticket"):
            doc["awaiting_email"] = True
            self.save_chat(doc)
            return self.send(doc["chat_id"], "Sure — send me your email address and I'll mail you the confirmation as soon "
                                             "as your ticket is created.")
        if doc.get("contact_email"):
            return self.capture_email(doc, doc["contact_email"])
        doc["awaiting_email"] = True
        self.save_chat(doc)
        return self.send(doc["chat_id"], f"Sure! Which email address should I send the confirmation for {doc['last_ticket']} to?")

    def email(self, doc, ticket, kind, text, message=""):
        """Run the Email agent for a ticket and log it as a turn, so the website shows it."""
        conv = self.store.get("conversations", ticket.get("conversation_id") or "") or {}
        last_reply = next((m["text"] for m in reversed(conv.get("messages", [])) if m["role"] == "assistant"), "")
        ctx = A.customer_context(self.store, ticket.get("customer_id")) if ticket.get("customer_id") else None
        t0 = time.time()
        rec = send_email(self.store, doc["contact_email"], kind, ticket["id"], last_reply, message,
                         images=ticket_images(self.store, ticket.get("intent"), ticket.get("slots")),
                         customer_name=(ctx or {}).get("first_name"))
        step = dict(agent="Email Agent", status="OK", ms=round((time.time() - t0) * 1000, 1),
                    summary=f"{rec['status']} → {rec['to_masked']}")
        self.record(doc, text, dict(conversation_id=ticket.get("conversation_id"), channel="telegram", ticket=ticket,
                                    email=rec, context=ctx, trace=[step], decision="email",
                                    reply=f"Email {rec['status'].replace('_', ' ')} → {rec['to_masked']}"))
        return rec

    def deliver_notifications(self):
        """Send ticket updates made on the website (status changes, SLA escalations) to the customer's chat."""
        chats = {c["conv_id"]: c for c in self.store.list("telegram_chats") if c.get("conv_id")}
        if not chats:
            return
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
                    self.email(chat, t, "update", text=f"Ticket {n['ticket_id']} updated on the website", message=n["message"])

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


def _bot_threads(token):
    """Live bot threads for this token, including ones started by earlier versions of this file.

    Found through threading (not a module global), so they survive Streamlit hot-reloading this module."""
    out = []
    for t in threading.enumerate():
        bot = getattr(t, "bot", None) or getattr(getattr(t, "_target", None), "__self__", None)
        if t.name in OLD_THREAD_NAMES and t.is_alive() and getattr(bot, "token", None) == token and hasattr(bot, "stop"):
            out.append((t, bot))
    return out


def start_in_background(store, token=None, use_llm=True):
    """Start the bot on a daemon thread; returns the bot, or None when no token is configured.

    Exactly one poller per process: a bot running this code is reused; bots running older code (left behind
    when a deploy hot-reloads this module) are asked to stop, and the new bot starts once they have exited.
    """
    token = token or get_token()
    if not token:
        return None
    running = _bot_threads(token)
    for t, bot in running:
        if type(bot) is TelegramBot and not bot._stop.is_set():
            return bot
    for _, bot in running:
        bot.stop()
    bot = TelegramBot(store, token, use_llm)

    def run():
        for t, _ in running:  # let previous pollers finish their last long-poll and hand over
            t.join(POLL_TIMEOUT + 25)
        bot.run()

    thread = threading.Thread(target=run, name=THREAD_NAME, daemon=True)
    thread.bot = bot
    thread.start()
    return bot


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
    b = TelegramBot(A.Store(), tok, use_llm=A.llm_enabled())
    print("SupportPilot Telegram bot running — press Ctrl+C to stop.")
    b.run()
