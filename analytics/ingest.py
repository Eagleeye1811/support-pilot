"""Extract → normalize → clean. Reads support data read-only; never writes to the live store.

Sources per tenant:
  * live SupportPilot records from the tenant Workspace: tickets created by the Phase 1 workflow (seeded demo
    tickets marked ``historical`` are excluded — the analytics history replaces them, so nothing is counted twice),
    workflow runs and actions;
  * the synthetic helpdesk history + event log + gateway stats (simulated tenants only — see ``synthetic.py``);
  * social posts from the configured ``SocialSource``s.

Every cleaning decision is counted in a data-quality report: exact duplicates, re-exported rows of the same ticket
(the most complete version wins), unparseable or impossible timestamps, unknown/missing channel, tier and intent
values, and out-of-range CSAT ratings.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re

import numpy as np
import pandas as pd

from agents.intent_agent import analyze_sentiment
from agents.shared_agent import INTENTS, SLA_RESOLUTION

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CHANNELS = {"web": "web", "website": "web", "web chat": "web", "webchat": "web", "whatsapp": "whatsapp", "wa": "whatsapp",
            "email": "email", "e-mail": "email", "mail": "email", "app": "app", "mobile app": "app", "telegram": "telegram"}
TIERS = {"standard": "Standard", "plus": "Plus", "premium": "Premium"}
TICKET_COLUMNS = ["id", "source", "created_at", "customer_id", "tier", "channel", "intent", "category", "priority",
                  "first_message", "escalated", "team", "status", "first_response_at", "resolved_at", "csat", "reopened",
                  "gateway", "courier", "area", "sku"]
DERIVED_COLUMNS = ["sentiment_score", "emotion", "first_response_min", "resolution_min", "sla_resolution_min", "sla_breached",
                   "resolved", "date"]
SOCIAL_TOPICS = {
    "wallet": ["wallet", "top up", "top-up", "topup", "novapay", "add money"],
    "payment": ["payment", "checkout", "charged", "debited", "upi", "payfast", "money gone", "deducted"],
    "broadband": ["novafiber", "internet", "wifi", "broadband", "speeds", "speed"],
    "app_login": ["login", "log in", "logs me out", "otp", "sign in"],
    "delivery": ["parcel", "delivery", "tracking", "late", "courier", "order 🙄", "waiting for my", "quick"],
    "product_quality": ["broke", "defect", "smells", "damaged", "faulty", "loving my", "air fryer"],
    "refunds": ["refund"],
    "customer_service": ["support", "customer care", "bot", "agent"],
    "pricing": ["price", "prices", "deals", "discount"],
}
TOPIC_INTENTS = {"payment": ["payment_failure", "double_charge"], "wallet": ["payment_failure"], "broadband": ["network_issue", "technician_visit"],
                 "app_login": ["login_issue", "password_reset"], "delivery": ["delivery_delay"], "product_quality": ["damaged_item"],
                 "refunds": ["refund_request", "refund_status"], "customer_service": ["human_agent"], "pricing": []}
COMPLAINT_WORDS = ["fail", "late", "never", "can't", "cant", "broke", "down", "dead", "twice", "crash", "waiting", "defect",
                   "smells", "unable", "not ", "logs me out", "went up", "🙄", "😡"]


def parse_time(value):
    """UTC timestamp from ISO 8601 (with or without offset/Z) or a 'dd/mm/yyyy HH:MM' IST export; NaT when invalid."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return pd.NaT
    s = str(value).strip()
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return pd.Timestamp(t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).tz_convert("UTC")
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2})", s)
    if m:
        d, mo, y, h, mi = map(int, m.groups())
        try:
            return pd.Timestamp(dt.datetime(y, mo, d, h, mi, tzinfo=IST)).tz_convert("UTC")
        except ValueError:
            return pd.NaT
    return pd.NaT


def norm_channel(v):
    return CHANNELS.get(str(v).strip().lower(), "unknown") if v not in (None, "") else "unknown"


def norm_intent(v):
    key = re.sub(r"[\s-]+", "_", str(v or "").strip().lower())
    return key if key in INTENTS else "unknown"


def norm_csat(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return np.nan, False
    m = re.fullmatch(r"\s*(\d+)\s*(?:/\s*5)?\s*", str(v))
    if not m:
        return np.nan, True
    r = int(m.group(1))
    return (float(r), False) if 1 <= r <= 5 else (np.nan, True)


def _completeness(t):
    return (t.get("resolved_at") is not None, t.get("csat") is not None, t.get("export_version", 1))


def clean_tickets(raw):
    """Raw ticket dicts (any source) → (clean DataFrame, quality report)."""
    rep = dict(raw_rows=len(raw))
    seen, unique = set(), []
    for t in raw:
        key = json.dumps(t, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(t)
    rep["exact_duplicates_removed"] = len(raw) - len(unique)
    by_id = {}
    for t in unique:
        if t["id"] not in by_id or _completeness(t) > _completeness(by_id[t["id"]]):
            by_id[t["id"]] = t
    rep["stale_versions_removed"] = len(unique) - len(by_id)
    rows, bad_time, impossible, bad_csat = [], 0, 0, 0
    counts = dict(channel_fixed=0, channel_unknown=0, tier_unknown=0, intent_fixed=0, intent_unknown=0)
    for t in by_id.values():
        created = parse_time(t.get("created_at"))
        if pd.isna(created):
            bad_time += 1
            continue
        resolved = parse_time(t.get("resolved_at"))
        if not pd.isna(resolved) and resolved < created:
            impossible += 1
            resolved = pd.NaT
        ch = norm_channel(t.get("channel"))
        counts["channel_unknown"] += ch == "unknown"
        counts["channel_fixed"] += ch != "unknown" and t.get("channel") != ch
        tier = TIERS.get(str(t.get("tier") or "").lower(), "unknown")
        counts["tier_unknown"] += tier == "unknown"
        intent = norm_intent(t.get("intent"))
        counts["intent_unknown"] += intent == "unknown"
        counts["intent_fixed"] += intent != "unknown" and t.get("intent") != intent
        csat, invalid = norm_csat(t.get("csat"))
        bad_csat += invalid
        meta = t.get("meta") or {}
        rows.append(dict(
            id=t["id"], source=t.get("source", "unknown"), created_at=created, customer_id=t.get("customer_id"), tier=tier,
            channel=ch, intent=intent, category=INTENTS.get(intent, {}).get("category", "Unknown"),
            priority=t.get("priority") or INTENTS.get(intent, {}).get("priority", "medium"),
            first_message=t.get("first_message") or "", escalated=bool(t.get("escalated")), team=t.get("team"),
            status=t.get("status"), first_response_at=parse_time(t.get("first_response_at")), resolved_at=resolved,
            csat=csat, reopened=bool(t.get("reopened")), gateway=meta.get("gateway"), courier=meta.get("courier"),
            area=meta.get("area"), sku=meta.get("sku")))
    rep.update(unparseable_timestamps_dropped=bad_time, resolved_before_created_cleared=impossible,
               invalid_csat_nulled=bad_csat, **counts)
    df = pd.DataFrame(rows, columns=TICKET_COLUMNS)
    if df.empty:
        rep["clean_rows"] = 0
        for c in DERIVED_COLUMNS:
            df[c] = pd.Series(dtype=bool if c in ("sla_breached", "resolved") else object)
        return df, rep
    df = df.sort_values("created_at").reset_index(drop=True)
    senti = df["first_message"].map(analyze_sentiment)
    df["sentiment_score"] = senti.map(lambda s: s["sentiment_score"])
    df["emotion"] = senti.map(lambda s: s["emotion"])
    df["first_response_min"] = (df["first_response_at"] - df["created_at"]).dt.total_seconds() / 60
    df["resolution_min"] = (df["resolved_at"] - df["created_at"]).dt.total_seconds() / 60
    sla = [SLA_RESOLUTION.get(t if t != "unknown" else "Standard", SLA_RESOLUTION["Standard"]).get(p, 1440)
           for t, p in zip(df["tier"], df["priority"])]
    df["sla_resolution_min"] = sla
    df["sla_breached"] = df["resolution_min"] > df["sla_resolution_min"]
    df["resolved"] = df["resolved_at"].notna()
    df["date"] = df["created_at"].dt.tz_convert(IST).dt.date
    rep["clean_rows"] = len(df)
    return df, rep


def live_tickets(ws):
    """Tickets created by the live Phase 1 workflow, in the raw ticket schema."""
    out = []
    for t in ws.list("tickets"):
        if t.get("historical"):
            continue
        out.append(dict(id=t["id"], source="supportpilot", created_at=t["created_at"], customer_id=t.get("customer_id"),
                        tier=(t.get("customer") or {}).get("tier"), channel=t.get("channel"), intent=t.get("intent"),
                        priority=t.get("priority"), first_message=t.get("description") or t.get("title", ""),
                        escalated=t.get("escalated"), team=t.get("team"), status=t.get("status"),
                        first_response_at=t.get("first_response_at"), resolved_at=t.get("resolved_at"), csat=t.get("csat"),
                        reopened=t.get("reopened", False), meta=t.get("meta") or {}))
    return out


def live_runs(ws):
    rows = []
    for r in ws.list("workflow_runs"):
        rows.append(dict(id=r["id"], started_at=parse_time(r.get("started_at")), status=r.get("status"), channel=r.get("channel"),
                         intent=r.get("intent"), intent_source=r.get("intent_source"), decision=r.get("decision"),
                         ticket_id=r.get("ticket_id"), ms=r.get("ms"), llm_mode=(r.get("llm") or {}).get("mode"),
                         tool_calls=len(r.get("tool_calls") or []), simulated=bool(r.get("simulated"))))
    return pd.DataFrame(rows, columns=["id", "started_at", "status", "channel", "intent", "intent_source", "decision",
                                       "ticket_id", "ms", "llm_mode", "tool_calls", "simulated"])


def live_actions(ws):
    rows = [dict(id=a["id"], ts=parse_time(a.get("ts")), action=a["action"], status=a["status"], actor=a.get("actor"),
                 authorization=(a.get("authorization") or {}).get("outcome"), simulated=bool(a.get("simulated")))
            for a in ws.list("actions")]
    return pd.DataFrame(rows, columns=["id", "ts", "action", "status", "actor", "authorization", "simulated"])


def topic_of(text):
    t = str(text).lower()
    for topic, words in SOCIAL_TOPICS.items():
        if any(w in t for w in words):
            return topic
    return "other"


def clean_social(raw, brand_terms):
    """Raw posts → (clean DataFrame, report). Off-brand posts are dropped unless the source was brand-targeted."""
    rep = dict(raw_posts=len(raw))
    by_id = {}
    for p in raw:
        by_id.setdefault(p["id"], p)
    rep["duplicate_posts_removed"] = len(raw) - len(by_id)
    rows, bad_time, offbrand, empty, dup_text = [], 0, 0, 0, 0
    seen_text = set()
    terms = [b.lower() for b in brand_terms]
    for p in by_id.values():
        text = str(p.get("text") or "").strip()
        if not text:
            empty += 1
            continue
        ts = parse_time(p.get("created_at"))
        if pd.isna(ts):
            bad_time += 1
            continue
        if p.get("source") == "synthetic" and not any(b in text.lower() for b in terms):
            offbrand += 1
            continue
        key = (p.get("author"), text.lower(), ts.floor("h"))
        if key in seen_text:  # same author, same text, same hour: spam / cross-post
            dup_text += 1
            continue
        seen_text.add(key)
        s = analyze_sentiment(text)
        lower = text.lower()
        complaint = s["sentiment"] == "negative" or any(w in lower for w in COMPLAINT_WORDS)
        rows.append(dict(id=p["id"], source=p.get("source"), platform=p.get("platform"), created_at=ts, text=text,
                         author=p.get("author"), likes=int(p.get("likes") or 0), reposts=int(p.get("reposts") or 0),
                         lang=p.get("lang"), url=p.get("url"), topic=topic_of(text), sentiment=s["sentiment"],
                         sentiment_score=s["sentiment_score"], complaint=bool(complaint)))
    rep.update(empty_posts_dropped=empty, unparseable_timestamps_dropped=bad_time, off_brand_dropped=offbrand,
               repeated_text_dropped=dup_text, clean_posts=len(rows))
    df = pd.DataFrame(rows, columns=["id", "source", "platform", "created_at", "text", "author", "likes", "reposts", "lang",
                                     "url", "topic", "sentiment", "sentiment_score", "complaint"])
    if not df.empty:
        df = df.sort_values("created_at").reset_index(drop=True)
        df["date"] = df["created_at"].dt.tz_convert(IST).dt.date
    else:
        df["date"] = []
    return df, rep
