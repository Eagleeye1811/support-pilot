import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agents import email_agent  # noqa: E402
from telegram_bot import EMAIL_OFFER, TelegramBot  # noqa: E402

CHAT = 777
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000


class FakeBot(TelegramBot):
    """TelegramBot with the Bot API replaced by an in-memory outbox."""

    def __init__(self, store):
        super().__init__(store, "TEST", use_llm=False)
        self.outbox = []

    def call(self, method, http_timeout=15, files=None, **params):
        if method in ("sendMessage", "sendPhoto", "sendMediaGroup"):
            self.outbox.append(dict(params, method=method, files=sorted(files or {})))
        if method == "getFile":
            return {"file_path": "photos/x.jpg"}
        return {}

    def download(self, file_id):
        return JPEG

    def texts(self):
        return [m["text"] for m in self.outbox if m["method"] == "sendMessage"]


def message(text=None, photo=False, caption=None):
    msg = {"chat": {"id": CHAT}, "from": {"first_name": "Asha", "username": "asha"}}
    if photo:
        msg["photo"] = [{"file_id": "small", "width": 90}, {"file_id": "big", "width": 800}]
        if caption:
            msg["caption"] = caption
    else:
        msg["text"] = text
    return {"message": msg}


def button(data):
    return {"callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": CHAT}}, "from": {"first_name": "Asha"}}}


@pytest.fixture()
def bot():
    return FakeBot(A.Store(":memory:"))


@pytest.fixture()
def smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, *a):
            pass

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setenv("SMTP_USER", "desk@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcdabcdabcdabcd")
    monkeypatch.setattr(email_agent.smtplib, "SMTP_SSL", FakeSMTP)
    return sent


def test_start_offers_demo_customers(bot):
    bot.handle_update(message("/start"))
    keyboard = bot.outbox[-1]["reply_markup"]["inline_keyboard"]
    assert any(b[0]["callback_data"] == "login:CUST1001" for b in keyboard)
    assert keyboard[-1][0]["callback_data"] == "login:guest"


def test_wrong_item_reply_has_product_album_stars_and_email_offer(bot):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("My order was delivered but I received the wrong item"))
    reply = next(m for m in bot.outbox if m["method"] == "sendMessage" and "reply_markup" in m and "csat:5" in str(m["reply_markup"]))
    assert EMAIL_OFFER in reply["text"]
    album = bot.outbox[-1]
    assert album["method"] == "sendMediaGroup" and album["files"] == ["EL-110", "EL-200"]
    assert [m["caption"].split(":")[0] for m in album["media"]] == ["Ordered", "Received"]
    turn = A.recent_turns(bot.store, source="telegram")[-1]
    assert turn["result"]["ticket"]["channel"] == "telegram" and turn["chat_id"] == CHAT

    bot.handle_update(button("csat:5"))
    assert "Thank you" in bot.texts()[-1]
    assert bot.store.get("tickets", turn["result"]["ticket"]["id"])["status"] == "Closed"


def test_photo_is_stored_and_attached_to_the_ticket(bot):
    bot.handle_update(button("login:CUST1012"))
    bot.handle_update(message(photo=True, caption="My laptop arrived damaged, the screen is cracked"))
    att = bot.store.list("attachments")
    assert len(att) == 1 and att[0]["size"] == len(JPEG)
    r = A.recent_turns(bot.store)[-1]["result"]
    assert r["attachments"] == [att[0]["id"]]
    assert r["investigation"]["steps"][0]["check"] == "Photo evidence"
    assert att[0]["id"] in bot.store.get("tickets", r["ticket"]["id"])["attachments"]


def test_email_capture_sends_the_ticket_and_later_website_updates(bot, smtp):
    bot.handle_update(button("login:CUST1008"))
    bot.handle_update(message("There's a ₹18,499 transaction I did not make. Is this fraud?"))
    tid = bot.store.get("telegram_chats", CHAT)["last_ticket"]
    bot.handle_update(message("asha.k@gmail.com"))
    assert "Sent the ticket details to a***@gmail.com" in bot.texts()[-1]
    assert len(smtp) == 1 and tid in smtp[0]["Subject"]
    assert A.recent_turns(bot.store)[-1]["result"]["email"]["status"] == "sent"

    bot.deliver_notifications()  # nothing new yet
    A.transition(bot.store, tid, "Resolved", actor="Human Agent", note="Card blocked and money returned")
    bot.deliver_notifications()
    bot.deliver_notifications()
    assert sum(t.startswith("🔔") for t in bot.texts()) == 1
    assert len(smtp) == 2 and "Update: Resolved" in smtp[1]["Subject"]


def test_skip_email_and_new_conversation(bot):
    bot.handle_update(button("login:CUST1003"))
    bot.handle_update(message("I can't login, it says my account is locked"))
    assert bot.store.get("telegram_chats", CHAT)["awaiting_email"]
    bot.handle_update(message("skip"))
    assert "keep you updated here" in bot.texts()[-1]
    first = bot.store.get("telegram_chats", CHAT)["conv_id"]
    bot.handle_update(message("/new"))
    bot.handle_update(message("hi"))
    assert bot.store.get("telegram_chats", CHAT)["conv_id"] not in (None, first)


def test_guest_question(bot):
    bot.handle_update(message("/guest"))
    bot.handle_update(message("What are the delivery charges for cash on delivery?"))
    assert bot.store.get("telegram_chats", CHAT)["conv_id"]
