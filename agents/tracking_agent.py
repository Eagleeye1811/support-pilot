"""Agent 9: Resolution Tracking — ticket lifecycle, automatic customer updates and SLA monitoring.

Lifecycle: Opened → Assigned → Pending Customer → Resolved → Closed (with
reopen). Every transition notifies the customer on their channel. The SLA
monitor flags tickets at risk (≥75% of the deadline used) or breached and
auto-escalates them to the Escalation Desk — e.g. Premium users have a
15-minute response deadline.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

from .shared_agent import SLA_RESOLUTION, SLA_RESPONSE, TEAMS, bump_priority, fmt_ts, iso, minutes_between, now, parse_ts

STATUS_FLOW = ["Opened", "Assigned", "Pending Customer", "Resolved", "Closed"]
OPEN_STATUSES = {"Opened", "Assigned", "Pending Customer"}
TRANSITIONS = {
    "Opened": {"Assigned", "Resolved", "Closed"},
    "Assigned": {"Assigned", "Pending Customer", "Resolved"},
    "Pending Customer": {"Assigned", "Resolved"},
    "Resolved": {"Closed", "Opened"},
    "Closed": set(),
}
NOTIFY = {
    "Opened": "We've logged your issue as {id} ({title}). A specialist will pick it up shortly.",
    "Assigned": "Your ticket {id} is assigned to {assignee} ({team}). Expected first response by {response_due}.",
    "Pending Customer": "Update on {id}: {note} We need a reply from you to continue.",
    "Resolved": "Good news — ticket {id} is resolved. {note} Reply here if anything is still not right.",
    "Closed": "Ticket {id} is now closed. Thank you for your patience!",
    "Reopened": "Ticket {id} has been reopened and is back with our team.",
}


def sla_targets(tier, priority, created):
    created = parse_ts(created)
    resp, res = SLA_RESPONSE[tier][priority], SLA_RESOLUTION[tier][priority]
    return dict(policy=f"{tier} · {priority}", response_minutes=resp, resolution_minutes=res,
                response_due=iso(created + dt.timedelta(minutes=resp)), resolution_due=iso(created + dt.timedelta(minutes=res)))


def assign(store, team):
    """Least-loaded agent in the team (by open tickets)."""
    roster = TEAMS.get(team) or TEAMS["Tier-1 Support"]
    load = Counter(t.get("assignee") for t in store.list("tickets") if t.get("status") in OPEN_STATUSES)
    return min(roster, key=lambda a: (load[a], roster.index(a)))


def _notify(store, ticket, status, note=""):
    from .action_agent import notify_customer
    msg = NOTIFY.get(status, "Ticket {id} updated: {note}").format(
        id=ticket["id"], title=ticket.get("title", ""), assignee=ticket.get("assignee", "our team"), team=ticket.get("team", ""),
        response_due=fmt_ts(ticket.get("sla", {}).get("response_due")), note=note or "")
    notify_customer(store, dict(customer_id=ticket["customer_id"], channel=ticket.get("channel", "web"),
                                ticket_id=ticket["id"], message=msg.strip(), kind=f"status:{status}"), None)
    return msg


def transition(store, ticket_or_id, to_status, actor="System", note="", notify=True, ref=None):
    t = store.get("tickets", ticket_or_id) if isinstance(ticket_or_id, str) else ticket_or_id
    if t is None:
        raise ValueError(f"Ticket {ticket_or_id} not found")
    cur = t.get("status", "Opened")
    if to_status == "Reopened":
        to_status = "Opened"
        t["reopened"] = True
    if to_status not in TRANSITIONS.get(cur, set()) and not (cur == to_status == "Assigned"):
        raise ValueError(f"Invalid transition {cur} → {to_status}. Allowed: {sorted(TRANSITIONS.get(cur, []))}")
    ts = iso(ref)
    t["status"] = to_status
    t["updated_at"] = ts
    if to_status in ("Pending Customer", "Resolved") and not t.get("first_response_at") and actor != "SupportPilot AI":
        t["first_response_at"] = ts
    if to_status == "Resolved":
        t["resolved_at"] = ts
        if actor != "System" or not t.get("resolved_by"):
            t["resolved_by"] = actor
    if to_status == "Closed":
        t["closed_at"] = ts
    if to_status == "Opened" and t.get("reopened"):
        t.pop("resolved_at", None)
    t.setdefault("events", []).append(dict(ts=ts, status=to_status, actor=actor, note=note))
    store.put("tickets", t["id"], t)
    if notify:
        _notify(store, t, "Reopened" if t.get("reopened") and to_status == "Opened" else to_status, note)
    return t


def sla_status(ticket, ref=None):
    ref = ref or now()
    sla = ticket.get("sla") or {}
    if not sla:
        return dict(state="n/a")
    created = ticket["created_at"]
    if ticket.get("status") not in OPEN_STATUSES:
        if not ticket.get("resolved_at"):
            return dict(state="n/a")
        met = minutes_between(ticket["resolved_at"], sla["resolution_due"]) >= 0
        return dict(state="met" if met else "missed", target="resolution", due=sla["resolution_due"])
    target = "response" if not ticket.get("first_response_at") else "resolution"
    due = sla[f"{target}_due"]
    total = max(minutes_between(created, due), 1)
    used = minutes_between(created, ref)
    left = minutes_between(ref, due)
    pct = used / total
    state = "breached" if left < 0 else ("at_risk" if pct >= 0.75 else "ok")
    return dict(state=state, target=target, due=due, minutes_left=round(left, 1), pct_used=round(pct, 2))


def sla_scan(store, ref=None, auto_escalate=True):
    """SLA Monitoring: find at-risk/breached open tickets and auto-escalate them once."""
    ref = ref or now()
    flagged = []
    for t in store.list("tickets", where=lambda x: x.get("status") in OPEN_STATUSES):
        s = sla_status(t, ref)
        if s["state"] not in ("at_risk", "breached"):
            continue
        row = dict(ticket_id=t["id"], customer=t["customer"]["name"], tier=t["customer"]["tier"], priority=t["priority"],
                   team=t.get("team"), assignee=t.get("assignee"), state=s["state"], target=s["target"], due=s["due"],
                   minutes_left=s["minutes_left"], escalated_now=False)
        if auto_escalate and not t.get("sla_escalated"):
            t["priority"] = bump_priority(t["priority"])
            t["sla_escalated"] = True
            t["previous_assignee"] = t.get("assignee")
            t["assignee"] = TEAMS["Escalation Desk"][0]
            note = (f"SLA {s['target']} {'breached' if s['state'] == 'breached' else 'at risk'} "
                    f"({s['minutes_left']:+.0f} min) — auto-escalated to Escalation Desk, priority → {t['priority']}")
            t.setdefault("events", []).append(dict(ts=iso(ref), status=t["status"], actor="SLA Monitor", note=note))
            store.put("tickets", t["id"], t)
            _notify(store, t, "SLA", "We've prioritised your ticket with a senior specialist.")
            row.update(escalated_now=True, priority=t["priority"], assignee=t["assignee"])
        flagged.append(row)
    return sorted(flagged, key=lambda r: r["minutes_left"])


def auto_close(store, ref=None, hours=48):
    ref = ref or now()
    closed = []
    for t in store.list("tickets", status="Resolved"):
        if t.get("resolved_at") and (ref - parse_ts(t["resolved_at"])).total_seconds() > hours * 3600:
            transition(store, t, "Closed", actor="Resolution Tracker", note=f"Auto-closed {hours}h after resolution", ref=ref)
            closed.append(t["id"])
    return closed


def status_reply(store, customer_id, ticket_id=None):
    """Answer "any update on my ticket?" from the tracker."""
    tickets = store.list("tickets", customer_id=customer_id) if customer_id else []
    if ticket_id:
        t = store.get("tickets", ticket_id)
        if not t or (customer_id and t["customer_id"] != customer_id):
            return None, f"I couldn't find ticket {ticket_id} on your account."
        tickets = [t]
    live = sorted([t for t in tickets if t.get("status") in OPEN_STATUSES or ticket_id], key=lambda t: t["created_at"], reverse=True)
    if not live:
        recent = sorted([t for t in tickets if (now() - parse_ts(t["created_at"])).days < 7], key=lambda t: t["created_at"], reverse=True)
        if recent:
            t = recent[0]
            return t, (f"You have no open tickets. Your latest ticket **{t['id']}** — {t['title']} was **{t['status']}** "
                       f"on {fmt_ts(t.get('resolved_at') or t['updated_at'])}: {t.get('resolution') or 'resolved'}")
        return None, "You have no open tickets right now. Is there anything new I can help with?"
    t = live[0]
    s = sla_status(t)
    last = (t.get("events") or [{}])[-1]
    msg = (f"Ticket **{t['id']}** — {t['title']} is **{t['status']}** with {t.get('assignee', 'our team')} "
           f"({t.get('team')}). Latest update: {last.get('note') or last.get('status', '')}.")
    if s.get("due"):
        msg += f" Target {s['target']} time: {fmt_ts(s['due'])}."
    return t, msg


def supervisor_metrics(store, ref=None):
    ref = ref or now()
    ts = store.list("tickets")
    total = len(ts) or 1
    open_ = [t for t in ts if t.get("status") in OPEN_STATUSES]
    resolved = [t for t in ts if t.get("resolved_at")]
    ai = [t for t in resolved if not t.get("escalated")]
    human = [t for t in resolved if t.get("escalated")]
    mins = lambda xs: round(sum(minutes_between(t["created_at"], t["resolved_at"]) for t in xs) / len(xs), 1) if xs else None
    sla_states = Counter(sla_status(t, ref)["state"] for t in ts)
    closed_sla = sla_states["met"] + sla_states["missed"]
    return dict(
        total=len(ts), open=len(open_), resolved=len(resolved), resolution_rate=round(len(resolved) / total, 3),
        automation_rate=round(len(ai) / total, 3), escalation_rate=round(sum(1 for t in ts if t.get("escalated")) / total, 3),
        avg_resolution_min=mins(resolved), avg_resolution_ai_min=mins(ai), avg_resolution_human_min=mins(human),
        sla_at_risk=sla_states["at_risk"], sla_breached=sla_states["breached"],
        sla_compliance=round(sla_states["met"] / closed_sla, 3) if closed_sla else None,
        by_status=dict(Counter(t["status"] for t in ts)), by_channel=dict(Counter(t.get("channel", "web") for t in ts)),
        by_team_open=dict(Counter(t.get("team") for t in open_)),
    )
