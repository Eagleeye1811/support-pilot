"""Human approval queue for actions the policy will not let the AI run on its own.

``request`` parks an action (status ``pending``) with the reason; it is linked to the ticket once the ticket
exists. A human then calls ``decide``: on approval the action runs with the human as the actor (ownership and
allow-list checks still apply), the ticket records the outcome and the customer is notified. Nothing in this
module is reachable from the LLM.
"""
from __future__ import annotations

from . import audit, tracking_agent
from .shared_agent import iso


def request(store, action, label, params, ctx, reason, conversation_id=None, ticket_id=None):
    key = next(((k, params[k]) for k in ("order_id", "payment_id", "subscription_id", "service_id") if params.get(k)), None)
    for ap in store.list("approvals", action=action, status="pending"):
        if key and ap["params"].get(key[0]) == key[1]:
            return ap
    seq = store.next_seq("approval", 1)
    ap = dict(id=f"APR-{seq:04d}", action=action, label=label, params=params, reason=reason, status="pending",
              customer_id=(ctx or {}).get("customer_id"), customer_name=(ctx or {}).get("name"),
              conversation_id=conversation_id, ticket_id=ticket_id, requested_at=iso(), requested_by="AI")
    store.put("approvals", ap["id"], ap)
    return ap


def link_ticket(store, approval_ids, ticket_id):
    for aid in approval_ids:
        ap = store.get("approvals", aid)
        if ap and not ap.get("ticket_id"):
            ap["ticket_id"] = ticket_id
            store.put("approvals", aid, ap)


def pending(store):
    return sorted(store.list("approvals", status="pending"), key=lambda a: a["id"])


def decide(store, approval_id, approve, approver, note=""):
    """Approve (run the action now) or reject a pending request. Returns the updated approval."""
    from . import action_agent, context_agent
    if not approver or not str(approver).strip():
        raise ValueError("An approver name is required")
    ap = store.get("approvals", approval_id)
    if ap is None:
        raise KeyError(f"Approval {approval_id} not found")
    if ap["status"] != "pending":
        raise ValueError(f"Approval {approval_id} is already {ap['status']}")
    actor = f"Human:{approver}"
    ap.update(decided_at=iso(), decided_by=approver, note=note)
    ticket = store.get("tickets", ap["ticket_id"]) if ap.get("ticket_id") else None
    if approve:
        ctx = context_agent.customer_context(store, ap["customer_id"])
        results = action_agent.execute(store, ap["action"], ap["params"], ctx, actor=actor, ticket_id=ap.get("ticket_id"),
                                       conversation_id=ap.get("conversation_id"), approval_id=ap["id"])
        main = results[0]
        ap.update(status="approved" if main["status"] in ("success", "skipped") else "approved_failed",
                  result=dict(action_id=main["id"], status=main["status"], message=main["message"]))
        if ticket:
            ticket.setdefault("actions_taken", []).append(f"{main['label']} — {main['status']} (approved by {approver}): "
                                                          f"{main['message']}")
            store.put("tickets", ticket["id"], ticket)
            if main["status"] in ("success", "skipped"):
                outstanding = [a for a in store.list("approvals", ticket_id=ticket["id"], status="pending") if a["id"] != ap["id"]]
                if not outstanding and ticket["status"] in ("Opened", "Assigned", "Pending Customer"):
                    tracking_agent.transition(store, ticket, "Resolved", actor=actor,
                                              note=f"{main['label']} approved and completed. {main.get('customer_message', '')}".strip())
    else:
        ap.update(status="rejected")
        if ticket:
            ticket.setdefault("events", []).append(dict(ts=iso(), status=ticket["status"], actor=actor,
                                                        note=f"Rejected {ap['label']}: {note or 'no reason given'}"))
            store.put("tickets", ticket["id"], ticket)
    store.put("approvals", ap["id"], ap)
    audit.log(store, "approval." + ("approved" if approve else "rejected"), actor, outcome=ap["status"],
              approval_id=ap["id"], action=ap["action"], ticket_id=ap.get("ticket_id"), note=note or None)
    return ap
