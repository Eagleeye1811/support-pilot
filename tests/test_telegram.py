import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agent_flow import build_flow  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402

CHAT = 777


class FakeBot(TelegramBot):
    """TelegramBot with the Bot API replaced by an in-memory outbox."""

    def __init__(self, store):
        super().__init__(store, "TEST", use_llm=False)
        self.outbox = []

    def call(self, method, http_timeout=15, **params):
        if method == "sendMessage":
            self.outbox.append(params)
        return {}


def message(text):
    return {"message": {"chat": {"id": CHAT}, "from": {"first_name": "Asha", "username": "asha"}, "text": text}}


def button(data):
    return {"callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": CHAT}}, "from": {"first_name": "Asha"}}}


@pytest.fixture()
def bot():
    return FakeBot(A.Store(":memory:"))


def test_start_offers_demo_customers(bot):
    bot.handle_update(message("/start"))
    keyboard = bot.outbox[-1]["reply_markup"]["inline_keyboard"]
    assert any(b[0]["callback_data"] == "login:CUST1001" for b in keyboard)
    assert keyboard[-1][0]["callback_data"] == "login:guest"


def test_issue_is_resolved_rated_and_recorded_for_the_website(bot):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("My order was delivered but I received the wrong item"))
    reply = bot.outbox[-1]
    assert "csat:5" in str(reply["reply_markup"])  # star buttons offered after auto-resolution
    chat = bot.store.get("telegram_chats", CHAT)
    assert chat["customer_id"] == "CUST1001" and chat["conv_id"]
    turn = bot.store.list("telegram_turns", chat_id=CHAT)[0]
    flow = build_flow(turn["text"], turn["result"])
    assert flow["outcome"] == "Resolved automatically" and flow["ticket"]
    ticket = bot.store.get("tickets", flow["ticket"])
    assert ticket["channel"] == "telegram"

    bot.handle_update(button("csat:5"))
    assert "Thank you" in bot.outbox[-1]["text"]
    assert bot.store.get("tickets", ticket["id"])["status"] == "Closed"


def test_website_ticket_update_reaches_the_chat_once(bot):
    bot.handle_update(button("login:CUST1008"))
    bot.handle_update(message("There's a ₹18,499 transaction I did not make. Is this fraud?"))
    tid = bot.store.list("telegram_turns", chat_id=CHAT)[0]["result"]["ticket"]["id"]
    bot.deliver_notifications()  # notifications raised during the turn are covered by the reply
    assert not any("🔔" in m["text"] for m in bot.outbox)

    A.transition(bot.store, tid, "Resolved", actor="Human Agent", note="Card blocked and money returned")
    bot.deliver_notifications()
    bot.deliver_notifications()
    updates = [m for m in bot.outbox if m["text"].startswith("🔔")]
    assert len(updates) == 1 and tid in updates[0]["text"]


def test_guest_and_new_conversation_commands(bot):
    bot.handle_update(message("/guest"))
    bot.handle_update(message("What are the delivery charges for cash on delivery?"))
    first = bot.store.get("telegram_chats", CHAT)["conv_id"]
    bot.handle_update(message("/new"))
    assert bot.store.get("telegram_chats", CHAT)["conv_id"] is None
    bot.handle_update(message("hi"))
    assert bot.store.get("telegram_chats", CHAT)["conv_id"] not in (None, first)
