"""Agent 8: Ticket Generation — a complete, structured ticket so humans never reread the chat.

Every handled issue becomes a ticket: auto-resolved ones are recorded as
Resolved by SupportPilot AI (for metrics and audit), escalated ones carry the
investigation, actions taken, escalation reasons, SLA, a conversation summary
and a recommended next step for the human agent.
"""
from __future__ import annotations

from . import tracking_agent
from .shared_agent import INTENTS, fmt_ts, iso, mask_email, mask_phone

TITLES = {
    "subscription_not_active": "{plan} subscription not activated",
    "wrong_item": "Wrong item delivered for {order_id}",
    "damaged_item": "Damaged item received — {order_id}",
    "payment_failure": "Payment failed but amount deducted — {order_id}",
    "double_charge": "Charged twice for {order_id}",
    "refund_request": "Refund request for {order_id}",
    "refund_status": "Refund status enquiry",
    "delivery_delay": "Delivery delayed — {order_id}",
    "cancel_order": "Cancel order {order_id}",
    "return_request": "Return request for {order_id}",
    "network_issue": "NovaFiber connectivity issue — {service_id}",
    "technician_visit": "Technician visit for {service_id}",
    "login_issue": "Unable to login",
    "password_reset": "Password reset",
    "fraud_report": "Unauthorized transaction reported",
    "data_breach": "Possible personal data exposure",
    "legal_request": "Legal / privacy request",
}
NEXT_STEP = {
    "Engineering": "Inspect entitlement service logs (ENT-503), force-activate the subscription and confirm with the customer.",
    "Payments": "Validate amount with gateway/bank, approve or reject the refund and share the bank reference (ARN).",
    "Fulfilment": "Verify with photo evidence / QC, then approve replacement or refund.",
    "Network Ops": "Check node/OLT health for the line and confirm restoration with the customer.",
    "Trust & Safety": "Contact customer by verified phone, review flagged transactions, decide chargeback and keep the hold until cleared.",
    "Security Incident Response": "Open a security incident, assess exposure scope and inform the DPO per DPDP Act timelines.",
    "Legal & Privacy": "Acknowledge formally within 24h; route to counsel / execute data-erasure workflow.",
    "Tier-1 Support": "Call the customer, confirm the issue and complete the missing details.",
    "Customer Success": "Contact the customer about their plan and confirm the change.",
}


def build_title(intent, slots, ctx, facts=None):
    slots = dict(slots or {})
    if intent == "subscription_not_active":
        slots.setdefault("plan", ((ctx or {}).get("subscription") or {}).get("plan", "Premium"))
    template = TITLES.get(intent, INTENTS.get(intent, INTENTS["general_inquiry"])["label"])
    try:
        title = template.format(**{k: v for k, v in slots.items() if isinstance(v, str)})
    except KeyError:
        title = INTENTS.get(intent, INTENTS["general_inquiry"])["label"]
    return title


def _investigation_lines(investigation):
    return [f"{s['check']}: {s['detail']}" for s in investigation.get("steps", [])]


def _action_lines(actions):
    return [f"{a['label']} — {a['status']}: {a['message']}" for a in actions if a["action"] != "notify_customer"]


def create_or_update(store, conv, issue, understanding, ctx, investigation, actions, decision, summary, articles=None):
    intent = issue["intent"]
    escalate = decision["decision"] == "escalate"
    existing = store.get("tickets", issue["ticket_id"]) if issue.get("ticket_id") else None
    ts = iso()
    if existing:
        t = existing
        t["investigation"] = list(dict.fromkeys(t.get("investigation", []) + _investigation_lines(investigation)))
        t["actions_taken"] = t.get("actions_taken", []) + _action_lines(actions)
        t["conversation_summary"] = summary
        t["transcript"] = conv["messages"]
        t["updated_at"] = ts
        if escalate and not t.get("escalated"):
            t.update(escalated=True, team=decision["team"], priority=decision["priority"],
                     escalation=dict(reasons=decision["reasons"], confidence=decision["confidence"], threshold=decision["threshold"]),
                     recommended_next_step=NEXT_STEP.get(decision["team"], NEXT_STEP["Tier-1 Support"]),
                     human_status=f"Needs {decision['team']} Review")
            t["assignee"] = tracking_agent.assign(store, decision["team"])
            t.pop("resolved_at", None)
            t.pop("resolved_by", None)
            t["status"] = "Opened"
            t.setdefault("events", []).append(dict(ts=ts, status="Opened", actor="Escalation Agent",
                                                    note="Escalated: " + "; ".join(decision["reasons"][:2])))
            store.put("tickets", t["id"], t)
            return tracking_agent.transition(store, t, "Assigned", actor="Router", note=f"Assigned to {t['assignee']}")
        store.put("tickets", t["id"], t)
        return t

    seq = store.next_seq("ticket", 1001)
    tier = ctx["tier"] if ctx else "Standard"
    priority = decision["priority"] if escalate else (issue.get("priority") or understanding["priority"])
    title = build_title(intent, issue.get("slots"), ctx, investigation.get("facts"))
    t = dict(
        id=f"TKT-{seq}", customer_id=ctx["customer_id"] if ctx else None,
        customer=dict(id=ctx["customer_id"], name=ctx["name"], tier=tier, email=mask_email(ctx["email"]),
                      phone=mask_phone(ctx["phone"]), headline=ctx["headline"], flags=ctx["flags"]) if ctx else
        dict(id=None, name="Unidentified customer", tier="Standard"),
        channel=conv.get("channel", "web"), conversation_id=conv["id"], issue_id=issue["id"], intent=intent,
        category=INTENTS.get(intent, INTENTS["general_inquiry"])["category"], title=title,
        description=issue.get("first_message") or "", priority=priority, sentiment=understanding.get("sentiment"),
        emotion=understanding.get("emotion"), escalated=escalate, team=decision["team"],
        assignee=tracking_agent.assign(store, decision["team"]) if escalate else "SupportPilot AI",
        human_status=f"Needs {decision['team']} Review" if escalate else "Resolved by AI",
        investigation=_investigation_lines(investigation), diagnosis=investigation.get("diagnosis"),
        actions_taken=_action_lines(actions),
        escalation=dict(reasons=decision["reasons"], confidence=decision["confidence"], threshold=decision["threshold"]) if escalate else None,
        recommended_next_step=NEXT_STEP.get(decision["team"], NEXT_STEP["Tier-1 Support"]) if escalate else None,
        resolution=None if escalate else investigation.get("diagnosis"),
        kb_articles=[a["id"] for a in (articles or [])[:3]], slots=issue.get("slots", {}),
        meta={k: v for k, v in (investigation.get("facts") or {}).items() if isinstance(v, (str, int, float))},
        conversation_summary=summary, transcript=conv["messages"], created_at=ts, updated_at=ts,
        sla=tracking_agent.sla_targets(tier, priority, ts),
        events=[dict(ts=ts, status="Opened", actor="Ticket Generation Agent", note="Ticket created from conversation")],
        status="Opened",
    )
    if escalate:
        store.put("tickets", t["id"], t)
        t = tracking_agent.transition(store, t, "Assigned", actor="Router", note=f"Assigned to {t['assignee']} ({t['team']})")
    else:
        t.update(first_response_at=ts, resolved_at=ts, resolved_by="SupportPilot AI", status="Resolved")
        t["events"].append(dict(ts=ts, status="Resolved", actor="SupportPilot AI", note=investigation.get("diagnosis", "")))
        store.put("tickets", t["id"], t)
    issue["ticket_id"] = t["id"]
    return t


def render_markdown(t):
    """Human-agent view of a ticket (also returned by the MCP server)."""
    lines = [f"### Ticket {t['id']} — {t['title']}",
             f"**Customer:** {t['customer']['name']} ({t['customer'].get('tier', '')}) · **Channel:** {t.get('channel')} · "
             f"**Priority:** {t['priority'].upper()} · **Status:** {t['status']} · **Owner:** {t.get('assignee')} ({t.get('team')})"]
    if t["customer"].get("headline"):
        lines.append(f"**Context:** {t['customer']['headline']}")
    lines.append(f"**Issue:** {t.get('description') or t['title']}")
    if t.get("investigation"):
        lines.append("**Investigation:**\n" + "\n".join(f"- {x}" for x in t["investigation"]))
    if t.get("actions_taken"):
        lines.append("**Actions taken:**\n" + "\n".join(f"- {x}" for x in t["actions_taken"]))
    if t.get("escalation"):
        lines.append("**Why escalated:**\n" + "\n".join(f"- {x}" for x in t["escalation"]["reasons"]))
    lines.append(f"**Status for agent:** {t.get('human_status', t['status'])}")
    if t.get("recommended_next_step"):
        lines.append(f"**Recommended next step:** {t['recommended_next_step']}")
    if t.get("sla"):
        lines.append(f"**SLA ({t['sla'].get('policy', '')}):** respond by {fmt_ts(t['sla']['response_due'])}, "
                     f"resolve by {fmt_ts(t['sla']['resolution_due'])}")
    lines.append(f"**Conversation summary:** {t.get('conversation_summary', '')}")
    return "\n\n".join(lines)
