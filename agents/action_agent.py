"""Agent 5: Action Execution — the system acts instead of merely answering.

The platform side of every action: authorization, idempotency, execution through the company adapter,
fallback chains and the audit trail. The business operation itself runs in the tenant's company system
(``integrations/``; for the demo, the simulated DemoStore). Guardrails:
  * allow-list of registered actions, and only those the company adapter supports
  * ownership — every referenced order / payment / subscription / line must belong to the customer
  * AI refund limit and per-tenant approval modes → queued for a human (``approvals``) instead of run
  * idempotency — the same money/order action on the same entity never runs twice in 30 days
  * fallback chains — e.g. remote line reset fails → schedule technician visit
  * every attempt is stored in ``actions`` and in the ``audit`` log
"""
from __future__ import annotations

import time

from integrations.base import ActionError, AdapterError

from . import approvals, audit, policy, runlog
from .shared_agent import iso, now, parse_ts
from .workspace import as_workspace

__all__ = ["ACTIONS", "ActionError", "catalog", "execute", "notify_customer"]

# action → (domain, label, description)
ACTIONS = {
    "cancel_order": ("E-commerce", "Cancel Order", "Cancel an order that has not shipped; refunds a captured payment"),
    "generate_return_label": ("E-commerce", "Generate Return Label", "Schedule a free return pickup"),
    "initiate_refund": ("E-commerce", "Initiate Refund", "Refund to the original payment method (AI limit applies)"),
    "create_replacement": ("E-commerce", "Create Replacement", "Ship a free replacement order"),
    "expedite_shipment": ("E-commerce", "Expedite Shipment", "Priority delivery for a late order"),
    "issue_coupon": ("E-commerce", "Issue Apology Coupon", "Goodwill coupon"),
    "reconcile_payment": ("E-commerce", "Reconcile Payment", "Mark a captured-but-pending payment as paid"),
    "reset_password": ("SaaS", "Reset Password", "Send a password reset link to the registered email"),
    "unlock_account": ("SaaS", "Unlock Account", "Clear a failed-attempts lock (not a security hold)"),
    "retry_subscription_sync": ("SaaS", "Retry Subscription Sync", "Re-push a paid subscription to the entitlement service"),
    "reactivate_subscription": ("SaaS", "Reactivate Subscription", "Turn a lapsed subscription back on"),
    "upgrade_plan": ("SaaS", "Upgrade Plan", "Move the customer to a higher plan"),
    "cancel_subscription": ("SaaS", "Cancel Subscription", "Stop auto-renewal at period end"),
    "create_service_request": ("Telecom", "Create Service Request", "Log a connectivity request, linked to an outage"),
    "schedule_technician_visit": ("Telecom", "Schedule Technician Visit", "Book a field technician"),
    "remote_line_reset": ("Telecom", "Remote Line Reset", "Reset the broadband line remotely"),
    "freeze_account": ("Security", "Security Hold", "Protective hold: block saved cards and sign out sessions"),
    "notify_customer": ("General", "Notify Customer", "Send the customer a notification on their channel"),
}
IDEMPOTENT_KEYS = ("order_id", "payment_id", "subscription_id", "service_id", "customer_id")
# State-checked actions may repeat; money/order/visit actions are idempotent for 30 days.
REPEATABLE = {"notify_customer", "reset_password", "issue_coupon", "unlock_account", "remote_line_reset", "upgrade_plan",
              "reactivate_subscription", "cancel_subscription"}


def notify_customer(store, p, ctx=None):
    """Platform notification (SupportPilot's own outbox, delivered by the channel integrations)."""
    seq = store.next_seq("notification", 1)
    n = dict(id=f"NTF-{seq:05d}", customer_id=p.get("customer_id"), channel=p.get("channel", "web"), ticket_id=p.get("ticket_id"),
             message=p.get("message", ""), kind=p.get("kind", "update"), created_at=iso())
    store.put("notifications", n["id"], n)
    return n, f"Confirmation sent on {n['channel']}."


def _prior_success(store, action, params):
    key = next(((k, params[k]) for k in IDEMPOTENT_KEYS if params.get(k)), None)
    if not key or action in REPEATABLE:
        return None
    return next((a for a in store.list("actions", action=action, status="success")
                 if a["params"].get(key[0]) == key[1] and (now() - parse_ts(a["ts"])).total_seconds() < 86400 * 30), None)


def _perform(ws, action, params, ctx):
    if action == "notify_customer":
        return notify_customer(ws, params, ctx)
    t0 = time.time()
    status, summary = "ok", {}
    try:
        data, msg = ws.adapter.perform(action, params, ctx)
        summary = dict(message=msg)
        return data, msg
    except Exception as exc:
        status, summary = "error", dict(error=str(exc)[:200])
        raise
    finally:
        runlog.record_tool(dict(tool=f"{ws.adapter.name}.actions.{action}", kind="action", args=params, status=status,
                                result=summary, ms=round((time.time() - t0) * 1000, 2), simulated=ws.adapter.simulated))


def execute(store, action, params, ctx=None, actor="AI", ticket_id=None, conversation_id=None, approval_id=None):
    """Authorize and execute one action, write it to the audit log, and follow its fallback chain if it fails."""
    ws = as_workspace(store)
    params = dict(params or {})
    fallback = params.pop("fallback", None)
    label = ACTIONS.get(action, (None, action))[1]
    rec = dict(id=None, action=action, label=label, params=params, actor=actor, ts=iso(), status="failed", message="",
               customer_message="", data={}, ticket_id=ticket_id, conversation_id=conversation_id,
               customer_id=(ctx or {}).get("customer_id") or params.get("customer_id"), approval_id=approval_id,
               adapter=ws.adapter.name, simulated=ws.adapter.simulated)
    auth = policy.authorize(ws, action, params, ctx, actor)
    rec["authorization"] = auth
    prior = _prior_success(ws, action, params) if auth["outcome"] != "deny" else None
    if auth["outcome"] == "deny":
        rec.update(status="blocked", message=auth["reason"])
    elif prior:
        rec.update(status="skipped", message=f"Already done ({prior['id']}) — not repeated",
                   customer_message=prior.get("customer_message", ""), data=prior.get("data", {}))
    elif auth["outcome"] == "approval":
        ap = approvals.request(ws, action, label, params, ctx, auth["reason"], conversation_id, ticket_id)
        rec.update(status="pending_approval", message=f"{auth['reason']} — queued for human approval ({ap['id']})",
                   customer_message=f"{label} needs a quick approval from our team; I've requested it (reference {ap['id']}).",
                   data=dict(approval_id=ap["id"]))
    else:
        try:
            data, msg = _perform(ws, action, params, ctx)
            rec.update(status="success", message=msg, customer_message=msg, data=data or {})
        except ActionError as exc:
            rec.update(status="blocked" if len(exc.args) > 1 and exc.args[1] == "blocked" else "failed", message=str(exc.args[0]))
        except AdapterError as exc:
            rec.update(status="failed", message=f"Company system unavailable: {exc}")
        except Exception as exc:  # defensive: an action must never crash the conversation
            rec.update(status="failed", message=f"{type(exc).__name__}: {exc}")
    rec["id"] = f"ACT-{ws.next_seq('action', 1):05d}"
    ws.put("actions", rec["id"], rec)
    if action != "notify_customer":
        audit.log(ws, f"action.{action}", actor, outcome=rec["status"], action_id=rec["id"], authorization=auth["outcome"],
                  rule=auth["rule"], reason=auth["reason"] or None, customer_id=rec["customer_id"], ticket_id=ticket_id,
                  approval_id=approval_id or rec["data"].get("approval_id"), simulated=ws.adapter.simulated)
    results = [rec]
    if rec["status"] == "failed" and fallback:
        fb = execute(ws, fallback["action"], dict(fallback.get("params", {})), ctx, actor, ticket_id, conversation_id)
        fb[0]["fallback_for"] = rec["id"]
        ws.put("actions", fb[0]["id"], fb[0])
        if fb[0]["status"] in ("success", "skipped"):
            rec["fallback_recovered"] = True
            ws.put("actions", rec["id"], rec)
        results += fb
    return results


def catalog(store=None):
    """Registered actions; with a store, also how the tenant's policy treats each one."""
    ws = as_workspace(store) if store is not None else None
    rows = []
    for k, (domain, label, desc) in ACTIONS.items():
        row = dict(action=k, domain=domain, label=label, description=desc, sensitive=k in policy.SENSITIVE_ACTIONS)
        if ws:
            row.update(mode=ws.policy.mode(k), supported=k == "notify_customer" or k in ws.adapter.supported_actions())
        rows.append(row)
    return rows
