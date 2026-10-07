import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agents import email_agent  # noqa: E402

SENT = []


class FakeSMTP:
    def __init__(self, host, port, context=None, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        assert user == "desk@gmail.com" and password == "abcdabcdabcdabcd"

    def send_message(self, msg):
        SENT.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    SENT.clear()
    monkeypatch.setenv("SMTP_USER", "desk@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd abcd abcd abcd")
    monkeypatch.setattr(email_agent.smtplib, "SMTP_SSL", FakeSMTP)
    return SENT


def test_ticket_email_is_sent_with_product_images(smtp):
    store = A.Store(":memory:")
    r = A.handle_message(store, "I received the wrong item", customer_id="CUST1001", use_llm=False,
                         contact_email="asha.k@gmail.com")
    assert r["email"]["status"] == "sent" and r["email"]["to_masked"] == "a***@gmail.com"
    assert any(s.get("tool") == "send_email" and s["transport"] == "mcp" for s in r["trace"])
    msg = smtp[-1]
    assert msg["To"] == "asha.k@gmail.com" and r["ticket"]["id"] in msg["Subject"]
    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert {p["Content-ID"] for p in images} == {"<EL-200>", "<EL-110>"}  # ordered vs received
    html = next(p for p in msg.walk() if p.get_content_type() == "text/html").get_content()
    assert "cid:EL-200" in html and "rate this support experience" not in html.lower()


def test_not_configured_is_logged_not_sent(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(email_agent, "get_secret", lambda name, default=None: default)
    store = A.Store(":memory:")
    rec = email_agent.send_email(store, "asha.k@gmail.com", kind="update", message="Your ticket was resolved")
    assert rec["status"] == "not_configured"
    assert store.get("emails", rec["id"])["subject"]


def test_never_emails_seeded_demo_addresses(smtp):
    store = A.Store(":memory:")
    demo = store.get("customers", "CUST1004")["email"]
    rec = email_agent.send_email(store, demo, kind="update", message="hello")
    assert rec["status"] == "blocked" and not smtp


def test_rate_limited_per_address(smtp):
    store = A.Store(":memory:")
    statuses = [email_agent.send_email(store, "asha.k@gmail.com", kind="update", message=f"m{i}")["status"] for i in range(7)]
    assert statuses == ["sent"] * email_agent.RATE_LIMIT + ["rate_limited"] * 2
    assert len(smtp) == email_agent.RATE_LIMIT


def test_invalid_address_is_blocked(smtp):
    rec = email_agent.send_email(A.Store(":memory:"), "not-an-email", kind="update", message="x")
    assert rec["status"] == "blocked" and not smtp


def test_smtp_failure_is_logged(smtp, monkeypatch):
    class Broken(FakeSMTP):
        def login(self, user, password):
            raise OSError("connection refused")
    monkeypatch.setattr(email_agent.smtplib, "SMTP_SSL", Broken)
    rec = email_agent.send_email(A.Store(":memory:"), "asha.k@gmail.com", kind="update", message="x")
    assert rec["status"] == "failed" and "connection refused" in rec["error"]
