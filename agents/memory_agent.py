"""Agent 6: Multi-Turn Conversation Memory — keeps context and tracks each issue's lifecycle.

A conversation holds the full transcript plus an *active issue* with slots
(order_id, service_id, ...). Follow-up messages such as a bare "ORD12345" or
"12345" fill the missing slot of the active issue instead of starting a new
one, so the customer never has to repeat themselves.
"""
from __future__ import annotations

import re

from .shared_agent import CONVERSATIONAL, INTENTS, iso, new_id

ISSUE_STATES = ["new", "investigating", "awaiting_customer", "resolved", "escalated", "closed"]
META_INTENTS = {"ticket_status", "human_agent"} | CONVERSATIONAL
SLOT_ENTITY_KEYS = ["order_id", "transaction_id", "service_id", "subscription_id", "ticket_id", "email", "phone",
                    "customer_id", "plan", "amount"]


def new_conversation(store, customer_id=None, channel="web", conversation_id=None):
    conv = dict(id=conversation_id or new_id("CONV"), customer_id=customer_id, channel=channel, created_at=iso(),
                updated_at=iso(), messages=[], active_issue=None, issues=[], awaiting=None, sentiment_trail=[],
                status="open", clarification_count=0)
    store.put("conversations", conv["id"], conv)
    return conv


def load_conversation(store, conversation_id, customer_id=None, channel="web"):
    conv = store.get("conversations", conversation_id) if conversation_id else None
    if conv is None:
        conv = new_conversation(store, customer_id, channel, conversation_id)
    if customer_id and not conv.get("customer_id"):
        conv["customer_id"] = customer_id
    return conv


def add_message(conv, role, text, meta=None):
    conv["messages"].append(dict(role=role, text=text, ts=iso(), meta=meta or {}))
    conv["updated_at"] = iso()


def save(store, conv):
    store.put("conversations", conv["id"], conv)
    return conv


def _new_issue(understanding):
    return dict(id=new_id("ISS"), intent=understanding["intent"], category=understanding["category"],
                label=understanding["label"], priority=understanding["priority"], slots=dict(understanding["entities"]),
                status="new", opened_at=iso(), ticket_id=None, turns=1, missing_slots=[],
                lifecycle=[dict(status="new", ts=iso(), note=f"Issue detected: {understanding['label']}")],
                first_message=None)


def set_status(issue, status, note=""):
    if issue is None:
        return
    if issue.get("status") != status or note:
        issue["status"] = status
        issue["lifecycle"].append(dict(status=status, ts=iso(), note=note))


def track_issue(conv, understanding, text):
    """Decide whether this message continues the active issue, fills a slot, or opens a new one.

    Returns (issue, mode) with mode in {"new", "slot_fill", "follow_up", "meta"}.
    """
    active = conv.get("active_issue")
    intent, conf, ents = understanding["intent"], understanding["confidence"], understanding["entities"]

    if active and active["status"] == "awaiting_customer":
        # A bare number answers an "Order ID?" question.
        if "order_id" in active.get("missing_slots", []) and "order_id" not in ents:
            m = re.search(r"\b(\d{5,8})\b", str(text))
            if m:
                ents = dict(ents, order_id=f"ORD{m.group(1)}")
        is_meta = intent in ("ticket_status", "human_agent", "greeting", "thanks")
        other_strong = intent not in ("general_inquiry", active["intent"]) and intent in INTENTS and conf >= 0.6
        if not is_meta and not other_strong:
            active["slots"].update({k: v for k, v in ents.items() if k in SLOT_ENTITY_KEYS})
            active["turns"] += 1
            return active, "slot_fill"

    if intent in META_INTENTS:
        if active:
            active["slots"].update({k: v for k, v in ents.items() if k in SLOT_ENTITY_KEYS})
        return active, "meta"

    if active and intent == active["intent"] and active["status"] not in ("closed",):
        active["slots"].update({k: v for k, v in ents.items() if k in SLOT_ENTITY_KEYS})
        active["turns"] += 1
        return active, "follow_up"

    return open_issue(conv, dict(understanding, entities=ents), text), "new"


def open_issue(conv, understanding, text):
    """Archive the active issue (if any) and start tracking a new one."""
    if conv.get("active_issue"):
        conv["issues"].append(conv["active_issue"])
    issue = _new_issue(dict(understanding, entities={k: v for k, v in understanding["entities"].items() if k in SLOT_ENTITY_KEYS}))
    issue.update(first_message=text, confidence=understanding["confidence"], emotion=understanding.get("emotion", "calm"))
    conv["active_issue"] = issue
    conv["clarification_count"] = 0
    return issue


def transcript(conv, limit=None):
    msgs = conv.get("messages", [])
    msgs = msgs[-limit:] if limit else msgs
    return "\n".join(f"{'Customer' if m['role'] == 'user' else 'SupportPilot'}: {m['text']}" for m in msgs)


def summarize(conv, issue=None, investigation=None, actions=None):
    """Deterministic one-paragraph summary a human agent can read instead of the transcript."""
    issue = issue or conv.get("active_issue") or {}
    user_msgs = [m["text"] for m in conv.get("messages", []) if m["role"] == "user"]
    parts = []
    if issue:
        first = issue.get("first_message") or (user_msgs[0] if user_msgs else "")
        parts.append(f"Customer reported: \"{first.strip()[:160]}\" ({issue.get('label', '')}).")
        slots = {k: v for k, v in issue.get("slots", {}).items() if k in ("order_id", "transaction_id", "service_id", "plan")}
        if slots:
            parts.append("Details captured: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in slots.items()) + ".")
    if investigation and investigation.get("diagnosis"):
        parts.append(f"Diagnosis: {investigation['diagnosis']}")
    done = [a for a in (actions or []) if a.get("status") == "success"]
    failed = [a for a in (actions or []) if a.get("status") in ("failed", "blocked")]
    if done:
        parts.append("Actions completed: " + "; ".join(a["label"] for a in done) + ".")
    if failed:
        parts.append("Actions that did not succeed: " + "; ".join(f"{a['label']} ({a['message']})" for a in failed) + ".")
    trail = conv.get("sentiment_trail", [])
    if trail:
        parts.append(f"Customer sentiment: {trail[-1]} across {len(user_msgs)} message(s).")
    return " ".join(parts)
