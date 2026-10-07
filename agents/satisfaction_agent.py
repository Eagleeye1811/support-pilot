"""Agent 10: Customer Satisfaction — collect CSAT, sentiment and resolution quality, and feed it back.

Feedback loop: intents whose *AI-resolved* tickets earn low CSAT get a
stricter escalation threshold (the Escalation Agent reads
`learned_thresholds`), so the system escalates more where automation
disappoints customers and automates more where it delights them.
"""
from __future__ import annotations

from collections import defaultdict

from .intent_agent import analyze_sentiment
from .shared_agent import BASE_CONFIDENCE_THRESHOLD, INTENTS, iso, minutes_between

CSAT_PROMPT = "How would you rate this support experience? Reply with 1–5 ⭐ (5 = excellent)."


def resolution_quality(ticket, rating):
    """0–100 blend of CSAT, first-contact resolution, SLA adherence and no reopen."""
    fcr = 1.0 if not ticket.get("escalated") else 0.4
    sla_ok = 1.0
    if ticket.get("resolved_at") and ticket.get("sla", {}).get("resolution_due"):
        sla_ok = 1.0 if minutes_between(ticket["resolved_at"], ticket["sla"]["resolution_due"]) >= 0 else 0.3
    reopen = 0.0 if ticket.get("reopened") else 1.0
    return round(100 * (0.5 * rating / 5 + 0.2 * fcr + 0.2 * sla_ok + 0.1 * reopen), 1)


def record_csat(store, ticket_id, rating, comment="", conversation_id=None):
    ticket = store.get("tickets", ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")
    rating = int(rating)
    if not 1 <= rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    senti = analyze_sentiment(comment) if comment else dict(sentiment="positive" if rating >= 4 else ("neutral" if rating == 3 else "negative"))
    rec = dict(id=f"CSAT-{ticket_id}-{store.next_seq('csat', 1)}", ticket_id=ticket_id, customer_id=ticket["customer_id"],
               rating=rating, comment=comment, sentiment=senti["sentiment"], intent=ticket["intent"], channel=ticket.get("channel"),
               resolved_by="Human" if ticket.get("escalated") else "AI", resolution_quality=resolution_quality(ticket, rating),
               conversation_id=conversation_id, ts=iso())
    store.put("csat", rec["id"], rec)
    ticket["csat"] = rating
    ticket.setdefault("events", []).append(dict(ts=iso(), status=ticket["status"], actor="Customer", note=f"CSAT {rating}/5"
                                                + (f" — “{comment}”" if comment else "")))
    store.put("tickets", ticket_id, ticket)
    return rec


def csat_summary(store):
    rows = store.list("csat")
    if not rows:
        return dict(count=0, avg=None, csat_pct=None, by_intent={}, by_resolver={}, avg_quality=None)
    by_intent, by_res = defaultdict(list), defaultdict(list)
    for r in rows:
        by_intent[r["intent"]].append(r["rating"])
        by_res[r["resolved_by"]].append(r["rating"])
    avg = lambda xs: round(sum(xs) / len(xs), 2)
    return dict(count=len(rows), avg=avg([r["rating"] for r in rows]),
                csat_pct=round(sum(r["rating"] >= 4 for r in rows) / len(rows), 3),
                avg_quality=round(sum(r.get("resolution_quality", 0) for r in rows) / len(rows), 1),
                by_intent={k: dict(avg=avg(v), n=len(v)) for k, v in by_intent.items()},
                by_resolver={k: dict(avg=avg(v), n=len(v)) for k, v in by_res.items()})


def learned_thresholds(store, min_samples=5):
    """Per-intent escalation thresholds learned from CSAT on AI-resolved tickets."""
    ai = defaultdict(list)
    for r in store.list("csat", resolved_by="AI"):
        ai[r["intent"]].append(r["rating"])
    out = {}
    for intent in INTENTS:
        xs = ai.get(intent, [])
        t, why = BASE_CONFIDENCE_THRESHOLD, "default"
        if len(xs) >= min_samples:
            a = sum(xs) / len(xs)
            if a < 3.0:
                t, why = 0.90, f"AI CSAT {a:.1f}/5 (n={len(xs)}) — escalate more"
            elif a < 3.5:
                t, why = 0.80, f"AI CSAT {a:.1f}/5 (n={len(xs)}) — escalate more"
            elif a >= 4.5:
                t, why = 0.65, f"AI CSAT {a:.1f}/5 (n={len(xs)}) — automate more"
            else:
                why = f"AI CSAT {a:.1f}/5 (n={len(xs)})"
        out[intent] = dict(threshold=t, reason=why)
    return out


def parse_rating(text):
    import re
    t = str(text).strip()
    m = re.fullmatch(r"\s*([1-5])\s*(?:/\s*5)?\s*(?:stars?|⭐+)?[\s.!]*(.*)", t, re.I | re.S)
    if m:
        return int(m.group(1)), m.group(2).strip()
    if re.fullmatch(r"⭐{1,5}", t):
        return len(t), ""
    return None, None
