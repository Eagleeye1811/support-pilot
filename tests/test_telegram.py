import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from telegram_bot import TelegramBot  # noqa: E402

CHAT = 777
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000


class FakeBot(TelegramBot):
    """TelegramBot with the Bot API replaced by an in-memory outbox."""

    def __init__(self, store):
        super().__init__(store, "TEST", use_llm=False)
        self.outbox = []

    def call(self, method, http_timeout=15, files=None, **params):
        if method in ("sendMessage", "sendPhoto"):
            self.outbox.append(dict(params, method=method, files=sorted(files or {}),
                                    text=params.get("text") or params.get("caption", "")))
        if method == "getFile":
            return {"file_path": "photos/x.jpg"}
        return {}

    def download(self, file_id):
        return JPEG


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


# ---- email --------------------------------------------------------------------------------------
@pytest.fixture()
def smtp(monkeypatch):
    from agents import email_agent
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


def texts(bot):
    return [m["text"] for m in bot.outbox]


def test_email_capture_sends_the_ticket_and_later_website_updates(bot, smtp):
    from telegram_bot import EMAIL_OFFER
    bot.handle_update(button("login:CUST1008"))
    bot.handle_update(message("There's a ₹18,499 transaction I did not make. Is this fraud?"))
    assert EMAIL_OFFER in texts(bot)[-1]
    tid = bot.store.get("telegram_chats", CHAT)["last_ticket"]
    bot.handle_update(message("asha.k@gmail.com"))
    assert "on its way to a***@gmail.com" in texts(bot)[-1]
    assert len(smtp) == 1 and tid in smtp[0]["Subject"]

    A.transition(bot.store, tid, "Resolved", actor="Human Agent", note="Card blocked and money returned")
    bot.deliver_notifications()
    bot.deliver_notifications()
    assert sum(t.startswith("🔔") for t in texts(bot)) == 1
    assert len(smtp) == 2 and "Update: Resolved" in smtp[1]["Subject"]


def test_email_offer_once_per_chat_and_skip(bot):
    from telegram_bot import EMAIL_OFFER
    bot.handle_update(button("login:CUST1003"))
    bot.handle_update(message("I can't login, it says my account is locked"))
    bot.handle_update(message("skip"))
    assert "keep you updated here" in texts(bot)[-1]
    bot.handle_update(message("/new"))
    bot.handle_update(message("I can't login, it says my account is locked"))
    assert sum(EMAIL_OFFER in t for t in texts(bot)) == 1


# ---- no duplicate pollers, no duplicate replies --------------------------------------------------
def test_the_same_update_is_handled_once(bot):
    upd = {"update_id": 501, **message("/start")}
    bot.process(upd)
    bot.process(upd)  # Telegram re-delivers after a restart / a second poller sees it
    assert len(bot.outbox) == 1


def test_two_bots_sharing_a_store_reply_once(bot):
    twin = FakeBot(bot.store)
    upd = {"update_id": 502, **message("/start")}
    bot.process(upd)
    twin.process(upd)
    assert len(bot.outbox) + len(twin.outbox) == 1


def _parked_run(self):
    self._stop.wait(5)


def test_one_poller_per_process_even_after_a_hot_reload(monkeypatch):
    import importlib
    import telegram_bot as tb
    monkeypatch.setattr(tb.TelegramBot, "run", _parked_run)
    store = A.Store(":memory:")
    first = tb.start_in_background(store, token="T1")
    assert tb.start_in_background(store, token="T1") is first  # reused, not duplicated
    reloaded = importlib.reload(tb)  # what Streamlit does when new code is deployed
    monkeypatch.setattr(reloaded.TelegramBot, "run", _parked_run)
    second = reloaded.start_in_background(store, token="T1")
    assert second is not first and first._stop.is_set()  # the old-code poller was told to stop
    second.stop()


def test_bots_from_earlier_versions_are_retired(monkeypatch):
    import threading
    import telegram_bot as tb
    monkeypatch.setattr(tb.TelegramBot, "run", _parked_run)

    class OldBot:  # what the previous deployment left running
        token = "T2"

        def __init__(self):
            self._stop = threading.Event()

        def run(self):
            self._stop.wait(10)

        def stop(self):
            self._stop.set()

    old = OldBot()
    legacy = threading.Thread(target=old.run, name="telegram-bot", daemon=True)
    legacy.start()
    new = tb.start_in_background(A.Store(":memory:"), token="T2")
    assert old._stop.is_set()
    legacy.join(2)
    assert not legacy.is_alive()
    new.stop()


# ---- guided wrong-item conversation ------------------------------------------------------------
def photo(caption=""):
    msg = {"chat": {"id": CHAT}, "from": {"first_name": "Asha"},
           "photo": [{"file_id": "small", "width": 90}, {"file_id": "big", "width": 800}]}
    if caption:
        msg["caption"] = caption
    return {"message": msg}


def flow_step(bot):
    return (bot.store.get("telegram_chats", CHAT).get("flow") or {}).get("step")


def test_wrong_item_full_guided_flow(bot, smtp):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("Hi, I got the wrong product in my delivery"))
    # 1 · the agent finds the order and shows the product it detected
    shown = bot.outbox[-1]
    assert shown["method"] == "sendPhoto" and shown["files"] == ["photo"]
    assert "ORD12345" in shown["text"] and "Headphones" in shown["text"]
    assert "wi:yes" in str(shown["reply_markup"]) and flow_step(bot) == "confirm"
    assert not bot.store.list("tickets", customer_id="CUST1001", where=lambda t: not t.get("historical"))

    # 2 · confirmed → asks for a photo of what arrived
    bot.handle_update(button("wi:yes"))
    assert "photo of the item you received" in texts(bot)[-1] and flow_step(bot) == "photo"

    # 3 · photo + description → the full pipeline runs: ticket, replacement, no rating yet
    bot.handle_update(photo("Got blue earbuds instead of the headphones"))
    assert flow_step(bot) is None
    chat = bot.store.get("telegram_chats", CHAT)
    ticket = bot.store.get("tickets", chat["last_ticket"])
    assert ticket["status"] == "Resolved" and ticket["attachments"] == [bot.store.list("attachments")[0]["id"]]
    assert "Create Replacement" in " ".join(ticket["actions_taken"])
    resolution = next(m for m in bot.outbox if ticket["id"] in m["text"] and m["method"] == "sendMessage")
    assert "reply_markup" not in resolution and "rate" not in resolution["text"].lower()
    assert "confirmation email" in texts(bot)[-1]  # 4 · email is offered before any rating
    turn = bot.store.list("telegram_turns", chat_id=CHAT)
    assert any(t["result"].get("investigation", {}).get("steps", [{}])[0].get("check") == "Photo evidence" for t in turn)

    # 4 · email → sent with ordered vs received images, then 5 · the rating
    bot.handle_update(message("my email is asha.k@gmail.com"))
    assert len(smtp) == 1 and ticket["id"] in smtp[0]["Subject"]
    assert "on its way to a***@gmail.com" in texts(bot)[-2]
    assert "csat:5" in str(bot.outbox[-1]["reply_markup"])
    bot.handle_update(button("csat:5"))
    assert "Thank you" in texts(bot)[-1]
    assert bot.store.get("tickets", ticket["id"])["status"] == "Closed"

    # the conversation transcript holds the whole dialog for the human agent
    conv = bot.store.get("conversations", chat["conv_id"])
    assert any("is this the order" in m["text"].lower() for m in conv["messages"])


def test_guest_is_asked_for_the_order_first(bot):
    bot.handle_update(message("/guest"))
    bot.handle_update(message("I received the wrong item"))
    assert "order ID" in texts(bot)[-1] and flow_step(bot) == "identify"
    bot.handle_update(message("it's ORD12345"))
    assert bot.outbox[-1]["method"] == "sendPhoto" and "ORD12345" in bot.outbox[-1]["text"]
    assert bot.store.get("telegram_chats", CHAT)["customer_id"] == "CUST1001"


def test_known_email_is_used_and_rating_follows(bot, smtp):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("/email asha.k@gmail.com"))
    bot.handle_update(message("I received the wrong item"))
    bot.handle_update(button("wi:yes"))
    bot.handle_update(message("skip"))  # no photo
    assert len(smtp) == 1  # confirmation sent automatically with the ticket
    assert "Confirmation email sent to a***@gmail.com" in texts(bot)[-2]
    assert "csat:5" in str(bot.outbox[-1]["reply_markup"])


def test_skip_email_then_rating(bot):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("I received the wrong item"))
    bot.handle_update(button("wi:yes"))
    bot.handle_update(photo())
    bot.handle_update(button("email:skip"))
    assert "keep you updated here" in texts(bot)[-2] and "csat:5" in str(bot.outbox[-1]["reply_markup"])


def test_description_without_photo_asks_once_more(bot):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("I received the wrong item"))
    bot.handle_update(message("yes"))
    bot.handle_update(message("I got wireless earbuds instead"))
    assert "attach a *photo*" in texts(bot)[-1] and flow_step(bot) == "photo"
    bot.handle_update(message("don't have one"))
    assert flow_step(bot) is None and bot.store.get("telegram_chats", CHAT)["last_ticket"]


def test_photo_first_then_complaint(bot):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(photo())
    assert "What went wrong" in texts(bot)[-1]
    bot.handle_update(message("wrong item delivered"))
    bot.handle_update(button("wi:yes"))  # the earlier photo is used — no second request
    ticket = bot.store.get("tickets", bot.store.get("telegram_chats", CHAT)["last_ticket"])
    assert ticket["attachments"]


def test_different_order_and_changing_topic(bot):
    bot.handle_update(button("login:CUST1001"))
    bot.handle_update(message("I received the wrong item"))
    bot.handle_update(button("wi:other"))
    assert "order ID" in texts(bot)[-1]
    bot.handle_update(message("Actually my account is locked, I can't login"))
    assert flow_step(bot) is None  # moved on: handled by the normal flow
    assert bot.store.get("telegram_chats", CHAT)["last_ticket"]


def test_asking_for_a_mail_in_own_words(bot, smtp):
    bot.handle_update(button("login:CUST1003"))
    bot.handle_update(message("I can't login, it says my account is locked"))
    bot.handle_update(message("skip"))
    bot.handle_update(message("can you send me a confirmation mail?"))
    assert "Which email address" in texts(bot)[-1]
    bot.handle_update(message("asha.k@gmail.com"))
    assert len(smtp) == 1
