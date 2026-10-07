"""Advanced: Omnichannel Support — one brain for Website Chat, WhatsApp, Email, Telegram and Mobile App.

Inbound payloads in each channel's native shape are normalized into a single
message (text + sender identifiers), and outbound replies are formatted for
the channel (email greeting/signature, WhatsApp bold syntax, plain text for
push notifications).
"""
from __future__ import annotations

import re

from .shared_agent import CHANNELS, COMPANY, PRODUCT

SAMPLE_PAYLOADS = {
    "whatsapp": {"from": "+91 98765 00003", "profile": {"name": "Rohan"}, "type": "text",
                 "text": {"body": "Hi, I can't login to my account, it says locked"}},
    "email": {"from": "John Doe <john.doe@example.com>", "subject": "Premium subscription not activated",
              "body": "Hello team,\n\nI paid for Premium yesterday but my subscription is still not activated.\n\n"
                      "Thanks,\nJohn\n\n> On Mon, Support wrote:\n> previous reply"},
    "telegram": {"message": {"from": {"username": "vikram_s", "first_name": "Vikram"}, "chat": {"id": 55120},
                             "text": "My internet is down since morning. Phone 9876500006"}},
    "app": {"user_id": "CUST1009", "device": "Android 15", "app_version": "7.4.1", "text": "Where is my order? It's late"},
    "web": {"session_customer_id": "CUST1001", "text": "My order was delivered but I received the wrong item"},
}


def _strip_email(body):
    lines = []
    for line in str(body).splitlines():
        if line.strip().startswith(">") or re.match(r"^On .+wrote:$", line.strip()):
            break
        if re.match(r"^(--|thanks|regards|best|cheers)[,!]?\s*$", line.strip(), re.I):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def normalize_inbound(channel, payload):
    """Return {channel, text, sender:{email, phone, customer_id, handle}, raw_channel}."""
    channel = channel if channel in CHANNELS else "web"
    payload = payload or {}
    if isinstance(payload, str):
        payload = {"text": payload}
    sender = {}
    if channel == "whatsapp":
        text = (payload.get("text") or {}).get("body") if isinstance(payload.get("text"), dict) else payload.get("text", "")
        sender["phone"] = re.sub(r"\D", "", str(payload.get("from", "")))[-10:]
        sender["handle"] = (payload.get("profile") or {}).get("name")
    elif channel == "email":
        subject, body = payload.get("subject", ""), _strip_email(payload.get("body", payload.get("text", "")))
        text = f"{subject}. {body}" if subject and subject.lower() not in body.lower() else body
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", str(payload.get("from", "")))
        sender["email"] = m.group(0).lower() if m else None
    elif channel == "telegram":
        msg = payload.get("message", payload)
        text = msg.get("text", "")
        sender["handle"] = (msg.get("from") or {}).get("username")
    elif channel == "app":
        text = payload.get("text", "")
        sender["customer_id"] = payload.get("user_id")
    else:
        text = payload.get("text", "")
        sender["customer_id"] = payload.get("session_customer_id")
    return dict(channel=channel, text=str(text or "").strip(), sender={k: v for k, v in sender.items() if v})


def format_outbound(channel, text, name=None, ticket_id=None, subject=None):
    if channel == "email":
        body = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        ref = f" [{ticket_id}]" if ticket_id else ""
        return (f"Subject: Re: {subject or 'Your support request'}{ref}\n\nHi {name or 'there'},\n\n{body}\n\n"
                f"Warm regards,\n{PRODUCT} · {COMPANY} Customer Support")
    if channel in ("whatsapp", "telegram"):
        out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        return out
    if channel == "app":
        return re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text
