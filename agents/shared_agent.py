"""SupportPilot shared layer: intent taxonomy, SLA policy, time/text helpers and optional Groq LLM access.

Every agent imports from here so the taxonomy, priorities and SLA rules have a
single source of truth. The LLM is optional: all decisions are deterministic,
the LLM may only re-phrase or break ties, and its output is always validated.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
COMPANY = "ShopNova"
PRODUCT = "SupportPilot"

PRIORITIES = ["low", "medium", "high", "critical"]
TIERS = ["Standard", "Plus", "Premium"]
CHANNELS = {"web": "Website Chat", "whatsapp": "WhatsApp", "email": "Email", "telegram": "Telegram", "app": "Mobile App"}

AUTO_REFUND_LIMIT = 10_000        # ₹ — AI may refund up to this amount without human approval
HIGH_VALUE_CLAIM = 25_000         # ₹ — damage/wrong-item claims above this need manual approval
BASE_CONFIDENCE_THRESHOLD = 0.70  # spec: "Confidence < 70% → Escalate"

# ---- Intent taxonomy -------------------------------------------------------
# phrases score 3, keywords score 1. required_slots drive multi-turn slot filling.
INTENTS = {
    "payment_failure": dict(label="Payment failed / money deducted", category="Billing", priority="high", team="Payments",
        domain="E-commerce", required_slots=["order_id"],
        phrases=["payment failed", "payment fail", "payment has failed", "transaction failed", "money deducted", "amount deducted",
                 "money was deducted", "amount was deducted", "money got deducted", "debited but", "deducted but", "charged but",
                 "payment declined", "payment not successful", "payment unsuccessful", "upi failed", "payment pending"],
        keywords=["payment", "deducted", "debited", "declined", "upi", "failed"]),
    "double_charge": dict(label="Charged twice", category="Billing", priority="high", team="Payments", domain="E-commerce",
        required_slots=["order_id"],
        phrases=["charged twice", "double charged", "deducted twice", "debited twice", "duplicate charge", "duplicate payment",
                 "paid twice", "charged two times", "billed twice"],
        keywords=["twice", "duplicate", "double"]),
    "refund_request": dict(label="Refund request", category="Billing", priority="medium", team="Payments", domain="E-commerce",
        required_slots=["order_id"],
        phrases=["want a refund", "need a refund", "refund my", "money back", "give me refund", "request a refund",
                 "initiate refund", "initiate a refund", "get a refund", "full refund", "refund for"],
        keywords=["refund", "reimburse"]),
    "refund_status": dict(label="Refund status", category="Billing", priority="medium", team="Payments", domain="E-commerce",
        required_slots=[],
        phrases=["refund status", "where is my refund", "refund not received", "not received my refund", "not received refund",
                 "havent received my refund", "refund still", "when will i get my refund", "refund pending", "status of my refund",
                 "refund not credited", "refund has not"],
        keywords=[]),
    "wrong_item": dict(label="Wrong item delivered", category="Orders", priority="high", team="Fulfilment", domain="E-commerce",
        required_slots=["order_id"],
        phrases=["wrong item", "wrong product", "different item", "different product", "incorrect item", "not what i ordered",
                 "received the wrong", "sent me the wrong", "sent the wrong", "wrong size", "wrong colour", "wrong color",
                 "wrong order"],
        keywords=["wrong", "incorrect", "mismatch"]),
    "damaged_item": dict(label="Damaged / defective item", category="Orders", priority="high", team="Fulfilment", domain="E-commerce",
        required_slots=["order_id"],
        phrases=["damaged", "broken", "defective", "cracked", "dead on arrival", "stopped working", "screen is broken",
                 "arrived damaged", "torn"],
        keywords=["damage", "faulty", "dent"]),
    "delivery_delay": dict(label="Delivery delayed / order status", category="Orders", priority="medium", team="Fulfilment",
        domain="E-commerce", required_slots=["order_id"],
        phrases=["where is my order", "not delivered", "not yet delivered", "order delayed", "delivery delayed", "late delivery",
                 "still not received", "hasnt arrived", "has not arrived", "track my order", "order status", "when will my order",
                 "delivery date", "not received my order", "order not received"],
        keywords=["delivery", "tracking", "shipment", "courier", "arrive", "delayed", "late"]),
    "cancel_order": dict(label="Cancel order", category="Orders", priority="medium", team="Fulfilment", domain="E-commerce",
        required_slots=["order_id"],
        phrases=["cancel my order", "cancel the order", "cancel order", "cancel this order", "dont want this order",
                 "cancel my purchase"],
        keywords=[]),
    "return_request": dict(label="Return / exchange", category="Orders", priority="medium", team="Fulfilment", domain="E-commerce",
        required_slots=["order_id"],
        phrases=["return the", "return my", "want to return", "return request", "return label", "send it back", "exchange it",
                 "return this"],
        keywords=["return", "exchange"]),
    "login_issue": dict(label="Unable to login", category="Account", priority="medium", team="Tier-1 Support", domain="SaaS",
        required_slots=[],
        phrases=["unable to login", "cant login", "cannot login", "cant log in", "cannot log in", "unable to log in", "login failed",
                 "account locked", "locked out", "cant sign in", "cannot sign in", "unable to sign in", "login not working",
                 "otp not received", "account is locked"],
        keywords=["login", "signin", "locked", "otp"]),
    "password_reset": dict(label="Password reset", category="Account", priority="low", team="Tier-1 Support", domain="SaaS",
        required_slots=[],
        phrases=["reset my password", "reset password", "forgot password", "forgot my password", "change my password",
                 "password reset", "new password"],
        keywords=["password"]),
    "subscription_not_active": dict(label="Subscription not activated", category="Subscription", priority="high", team="Engineering",
        domain="SaaS", required_slots=[],
        phrases=["subscription not activated", "not activated", "premium not", "plan not active", "subscription not active",
                 "subscription not working", "benefits not", "membership not", "paid for premium", "upgrade not reflected",
                 "still showing free", "still shows free", "premium is not"],
        keywords=["subscription", "premium", "membership", "activated", "activate"]),
    "reactivate_subscription": dict(label="Reactivate subscription", category="Subscription", priority="medium",
        team="Customer Success", domain="SaaS", required_slots=[],
        phrases=["reactivate", "renew my subscription", "resume my subscription", "restart my subscription",
                 "subscription expired", "renew my membership"],
        keywords=["renew", "reactivate"]),
    "upgrade_plan": dict(label="Upgrade plan", category="Subscription", priority="low", team="Customer Success", domain="SaaS",
        required_slots=[],
        phrases=["upgrade my plan", "upgrade to", "change my plan", "switch plan", "higher plan", "upgrade plan", "switch to premium"],
        keywords=["upgrade"]),
    "cancel_subscription": dict(label="Cancel subscription", category="Subscription", priority="medium", team="Customer Success",
        domain="SaaS", required_slots=[],
        phrases=["cancel my subscription", "cancel subscription", "stop my subscription", "end my membership", "unsubscribe",
                 "cancel membership", "cancel my membership", "cancel auto renewal", "stop auto renew", "turn off auto renew"],
        keywords=[]),
    "network_issue": dict(label="Internet / network issue", category="Connectivity", priority="high", team="Network Ops",
        domain="Telecom", required_slots=[],
        phrases=["internet not working", "internet is not working", "wifi is not working", "broadband is not working",
                 "router not working", "no internet", "internet is down", "internet down", "slow internet", "no signal",
                 "network issue", "network down", "wifi not working", "broadband not working", "connection dropping",
                 "no network", "fiber down", "speed is slow", "keeps disconnecting", "internet is slow", "net not working"],
        keywords=["internet", "broadband", "wifi", "network", "signal", "router", "fiber", "speed", "connection"]),
    "technician_visit": dict(label="Technician visit", category="Connectivity", priority="medium", team="Network Ops",
        domain="Telecom", required_slots=[],
        phrases=["technician", "engineer visit", "schedule a visit", "send someone", "home visit", "installation"],
        keywords=["technician", "installation"]),
    "fraud_report": dict(label="Fraud / unauthorized transaction", category="Security", priority="critical", team="Trust & Safety",
        domain="Security", required_slots=[],
        phrases=["fraud", "unauthorized", "unauthorised", "i didnt make", "i did not make", "not made by me", "scam",
                 "someone used my", "hacked", "stolen card", "suspicious transaction", "phishing", "dont recognise",
                 "dont recognize", "did not authorize", "didnt authorize"],
        keywords=["fraud", "unauthorized", "scam", "hacked", "suspicious"]),
    "data_breach": dict(label="Data breach / privacy exposure", category="Security", priority="critical",
        team="Security Incident Response", domain="Security", required_slots=[],
        phrases=["data breach", "data leak", "data was leaked", "personal data exposed", "privacy breach", "leaked my",
                 "data exposed", "someone has my details", "my details are leaked", "information leaked"],
        keywords=["breach", "leak", "leaked"]),
    "legal_request": dict(label="Legal / regulatory request", category="Legal", priority="high", team="Legal & Privacy",
        domain="Legal", required_slots=[],
        phrases=["legal action", "lawyer", "consumer court", "legal notice", "sue you", "lawsuit", "court", "gdpr",
                 "delete my data", "data deletion", "right to be forgotten", "advocate", "consumer forum"],
        keywords=["legal", "lawyer", "court", "sue"]),
    "human_agent": dict(label="Wants a human agent", category="General", priority="medium", team="Tier-1 Support",
        domain="General", required_slots=[],
        phrases=["talk to a human", "speak to a human", "real person", "human agent", "talk to someone", "speak to someone",
                 "customer care executive", "live agent", "talk to agent", "speak to an agent", "talk to an agent",
                 "connect me to", "your manager", "a supervisor", "human being"],
        keywords=["human"]),
    "ticket_status": dict(label="Ticket status / follow-up", category="General", priority="low", team="Tier-1 Support",
        domain="General", required_slots=[],
        phrases=["ticket status", "status of my ticket", "update on my ticket", "any update", "complaint status",
                 "status of my complaint", "what happened to my complaint", "following up", "follow up on"],
        keywords=["ticket"]),
    "general_inquiry": dict(label="General question", category="General", priority="low", team="Tier-1 Support",
        domain="General", required_slots=[], phrases=[], keywords=[]),
}
CONVERSATIONAL = {"greeting", "thanks", "affirm", "deny"}
ALWAYS_ESCALATE = {"fraud_report", "data_breach", "legal_request"}

# ---- SLA policy (minutes) — spec: Premium response deadline 15 minutes ------
SLA_RESPONSE = {
    "Premium": {"critical": 15, "high": 15, "medium": 30, "low": 60},
    "Plus": {"critical": 30, "high": 60, "medium": 120, "low": 240},
    "Standard": {"critical": 60, "high": 120, "medium": 240, "low": 480},
}
SLA_RESOLUTION = {
    "Premium": {"critical": 120, "high": 240, "medium": 480, "low": 1440},
    "Plus": {"critical": 240, "high": 480, "medium": 1440, "low": 2880},
    "Standard": {"critical": 480, "high": 1440, "medium": 2880, "low": 4320},
}

TEAMS = {
    "Tier-1 Support": ["Neha Kapoor", "Rahul Desai"],
    "Payments": ["Aditi Rao", "Sameer Joshi"],
    "Fulfilment": ["Imran Shaikh", "Pooja Bhatt"],
    "Engineering": ["Kunal Shah", "Divya Menon"],
    "Network Ops": ["Harish Kumar", "Lakshmi Pillai"],
    "Trust & Safety": ["Zoya Ahmed", "Nikhil Bansal"],
    "Security Incident Response": ["Arvind Nair"],
    "Legal & Privacy": ["Ritu Agarwal"],
    "Customer Success": ["Kavya Menon"],
    "Escalation Desk": ["Sunita Rao (Supervisor)"],
}


# ---- time helpers ---------------------------------------------------------
def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse_ts(value):
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    t = dt.datetime.fromisoformat(str(value))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def fmt_ts(value, with_date=True):
    if not value:
        return "—"
    t = parse_ts(value).astimezone(IST)
    return t.strftime("%d %b %Y, %H:%M IST" if with_date else "%H:%M IST")


def fmt_day(value):
    return parse_ts(value).astimezone(IST).strftime("%d %b %Y") if value else "—"


def minutes_between(start, end):
    return (parse_ts(end) - parse_ts(start)).total_seconds() / 60


def humanize_days(value, ref=None):
    days = (parse_ts(ref or now()).astimezone(IST).date() - parse_ts(value).astimezone(IST).date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 30:
        return f"{days} days ago"
    return f"{days // 30} month{'s' if days >= 60 else ''} ago"


def humanize_minutes(m):
    m = abs(m)
    if m < 60:
        return f"{m:.0f} min"
    if m < 60 * 48:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.1f} days"


# ---- text helpers ---------------------------------------------------------
def normalize_text(text):
    text = str(text or "").lower().replace("’", "'").replace("`", "'")
    text = text.replace("'", "")
    text = text.replace("log-in", "login").replace("sign-in", "signin").replace("e-mail", "email")
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text):
    text = re.sub(r"[^a-z0-9\s]", " ", normalize_text(text))
    return [w for w in text.split() if len(w) > 2 and w not in STOPWORDS]


STOPWORDS = {"the", "and", "for", "you", "your", "with", "that", "this", "are", "was", "but", "have", "has", "not", "can",
             "from", "what", "how", "why", "when", "will", "our", "its", "all", "any", "out", "get", "got", "had", "did",
             "does", "into", "they", "them", "then", "than", "there", "here", "been", "were", "also", "just", "about",
             "please", "pls", "hai", "hello", "hey"}


def bump_priority(priority, steps=1, cap="critical"):
    i = min(PRIORITIES.index(priority) + steps, PRIORITIES.index(cap))
    return PRIORITIES[max(i, 0)]


def mask_email(email):
    if not email or "@" not in email:
        return email or ""
    user, domain = email.split("@", 1)
    return f"{user[:2]}{'*' * max(len(user) - 2, 1)}@{domain}"


def mask_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"******{digits[-4:]}" if len(digits) >= 4 else ""


def inr(v):
    return f"₹{float(v):,.0f}"


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# ---- optional LLM (Groq, OpenAI-compatible) -------------------------------
def _load_env_file():
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=os.path.join(ROOT, ".env"), override=False)
    except Exception:
        pass


PLACEHOLDERS = {"", "your_key_here", "your_token_here", "you@gmail.com", "your_app_password"}


def get_secret(name, default=None):
    """A setting from the environment / .env, else Streamlit secrets; placeholders count as unset."""
    _load_env_file()
    value = (os.getenv(name) or "").strip()
    if value not in PLACEHOLDERS:
        return value
    try:
        import streamlit as st
        value = str(st.secrets.get(name, "") or "").strip()
        if value not in PLACEHOLDERS:
            return value
    except Exception:
        pass
    return default


def get_groq_api_key():
    return get_secret("GROQ_API_KEY")


def llm_enabled():
    return os.getenv("SUPPORTPILOT_DISABLE_LLM", "").lower() not in ("1", "true", "yes") and bool(get_groq_api_key())


def groq_chat(prompt, system_msg=None, model=None, api_key=None, max_tokens=400, json_mode=False):
    if os.getenv("SUPPORTPILOT_DISABLE_LLM", "").lower() in ("1", "true", "yes"):
        return None
    api_key = api_key or get_groq_api_key()
    if not api_key:
        return None
    model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg or "You are a careful customer support assistant. Never invent facts."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        import requests
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def groq_json(prompt, system_msg=None):
    raw = groq_chat(prompt, system_msg=system_msg, json_mode=True, max_tokens=300)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            return json.loads(match.group(0)) if match else None
        except Exception:
            return None
