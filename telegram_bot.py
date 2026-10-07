"""Real Telegram channel: long-polls the Bot API and runs every message through the agents.

Long polling needs no public webhook URL, so it runs inside the Streamlit app (one
background thread per server process) or standalone with `python telegram_bot.py`.
Each Telegram chat maps to one SupportPilot conversation; every turn is stored so the
Streamlit "Telegram live" tab can replay how the agents handled it. Ticket updates made
by a human agent on the website are delivered back to the customer's Telegram chat.
"""
from __future__ import annotations

import os
import threading
import time

import requests

import agents as A
from agents.shared_agent import _load_env_file, iso

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 10  # seconds; also how often website ticket updates are pushed to Telegram
_RUNNING = {}  # token → bot: one poller per process, or Telegram answers 409 Conflict
DEMO_CUSTOMERS = ["CUST1001", "CUST1002", "CUST1003", "CUST1005", "CUST1006", "CUST1008", "CUST1009", "CUST1010", "CUST1012"]
HELP = ("Tell me your problem in your own words — orders, payments, refunds, login, subscriptions or internet.\n\n"
        "/login — act as a demo customer (so I can see orders and accounts)\n"
        "/guest — continue without an account\n"
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


class TelegramBot:
    def __init__(self, store, token, use_llm=True):
        self.store, self.token, self.use_llm = store, token, use_llm
        self.http = requests.Session()
        self.started = iso()
        self.username = None
        self._stop = threading.Event()

    # ---- Bot API ---------------------------------------------------------------------------
    def call(self, method, http_timeout=15, **params):
        r = self.http.post(API.format(token=self.token, method=method), json=params, timeout=http_timeout)
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
        if not msg or not msg.get("text"):
            if msg:
                self.send(msg["chat"]["id"], "I can read text messages only — please type your issue.")
            return
        doc = self.chat(msg["chat"]["id"], msg.get("from"))
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
            doc["conv_id"] = None
            self.save_chat(doc)
            return self.send(doc["chat_id"], "Started a new conversation. What can I help with?")
        if cmd:
            return self.send(doc["chat_id"], HELP)
        self.run_turn(doc, text, msg)

    def handle_callback(self, cq):
        data, chat_id = cq.get("data", ""), cq["message"]["chat"]["id"]
        self.call("answerCallbackQuery", callback_query_id=cq["id"])
        doc = self.chat(chat_id, cq.get("from"))
        if data.startswith("login:"):
            return self.send(chat_id, self.set_customer(doc, data.split(":", 1)[1]))
        if data.startswith("csat:"):
            self.run_turn(doc, data.split(":", 1)[1], None)

    def run_turn(self, doc, text, msg):
        self.call("sendChatAction", chat_id=doc["chat_id"], action="typing")
        payload = dict(message=dict(text=text, chat={"id": doc["chat_id"]},
                                    **{"from": (msg or {}).get("from") or {"username": doc.get("username")}}))
        before = iso()
        r = A.handle_message(self.store, conversation_id=doc["conv_id"], customer_id=doc["customer_id"], channel="telegram",
                             payload=payload, use_llm=self.use_llm)
        conv = r.pop("conversation", {}) or {}
        doc["conv_id"] = r["conversation_id"]
        if conv.get("customer_id") and not doc.get("customer_id"):
            doc["customer_id"] = conv["customer_id"]  # identified from an order id / phone / email in the message
        self.save_chat(doc)
        # notifications raised during this turn are already covered by the reply
        for n in self.store.list("notifications", where=lambda n: n.get("created_at", "") >= before):
            self.store.put("telegram_sent", n["id"], dict(id=n["id"], ts=iso(), suppressed=True))
        seq = self.store.next_seq("telegram_turn", 1)
        self.store.put("telegram_turns", f"{seq:06d}", dict(id=f"{seq:06d}", seq=seq, chat_id=doc["chat_id"], conv_id=doc["conv_id"],
                                                             text=text, ts=iso(), result=r))
        buttons = [[{"text": "⭐" * i, "callback_data": f"csat:{i}"} for i in range(1, 6)]] \
            if r.get("awaiting") == "csat" else None
        self.send(doc["chat_id"], r.get("reply_channel") or r["reply"], buttons)

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
            if chat:
                self.send(chat["chat_id"], f"🔔 Update on {n['ticket_id']}: {n['message']}")
            self.store.put("telegram_sent", n["id"], dict(id=n["id"], ts=iso(), delivered=bool(chat)))

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
                    try:
                        self.handle_update(upd)
                    except Exception as exc:  # one bad message must not stop the bot
                        self.status(error=f"{type(exc).__name__}: {exc}")
                self.deliver_notifications()
                self.status(running=True, last_poll=iso())
            except Exception as exc:
                self.status(running=True, error=f"{type(exc).__name__}: {exc}", last_poll=iso())
                time.sleep(5)

    @staticmethod
    def _poll_params(offset):
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "edited_message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return params

    def stop(self):
        self._stop.set()


def start_in_background(store, token=None, use_llm=True):
    """Start the bot on a daemon thread; returns the bot, or None when no token is configured."""
    token = token or get_token()
    if not token:
        return None
    if token in _RUNNING and _RUNNING[token]["thread"].is_alive():
        return _RUNNING[token]["bot"]
    bot = TelegramBot(store, token, use_llm)
    thread = threading.Thread(target=bot.run, name="telegram-bot", daemon=True)
    thread.start()
    _RUNNING[token] = dict(bot=bot, thread=thread)
    return bot


if __name__ == "__main__":
    tok = get_token()
    if not tok:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
    b = TelegramBot(A.Store(), tok, use_llm=A.llm_enabled())
    print("SupportPilot Telegram bot running — press Ctrl+C to stop.")
    b.run()
