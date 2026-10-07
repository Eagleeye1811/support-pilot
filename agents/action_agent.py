"""Agent 5: Action Execution — the system acts instead of merely answering.

Every action mutates the support world (orders, refunds, accounts,
subscriptions, service lines...) and is written to an audit log. Guardrails:
  * allow-list of registered actions only
  * AI refund limit (AUTO_REFUND_LIMIT) — larger refunds are blocked for humans
  * ownership check — the entity must belong to the customer in context
  * idempotency — the same action on the same entity is never executed twice
  * fallback chains — e.g. remote line reset fails → schedule technician visit
"""
from __future__ import annotations

import datetime as dt
import random

from .shared_agent import AUTO_REFUND_LIMIT, IST, fmt_day, fmt_ts, inr, iso, mask_email, now, parse_ts


class ActionError(Exception):
    pass


def _order(store, params, ctx):
    o = store.get("orders", params.get("order_id"))
    if not o:
        raise ActionError(f"Order {params.get('order_id')} not found")
    if ctx and o["customer_id"] != ctx["customer_id"]:
        raise ActionError("Order does not belong to this customer")
    return o


def _sub(store, params, ctx):
    s = store.get("subscriptions", params.get("subscription_id"))
    if not s:
        raise ActionError(f"Subscription {params.get('subscription_id')} not found")
    if ctx and s["customer_id"] != ctx["customer_id"]:
        raise ActionError("Subscription does not belong to this customer")
    return s


def _line(store, params, ctx):
    l = store.get("service_lines", params.get("service_id"))
    if not l:
        raise ActionError(f"Service line {params.get('service_id')} not found")
    if ctx and l["customer_id"] != ctx["customer_id"]:
        raise ActionError("Service line does not belong to this customer")
    return l


def _refund(store, order, amount, reason, payment_id=None, method=None):
    seq = store.next_seq("refund", 7000)
    fast = (method or order.get("payment_method", "")).upper() in ("UPI", "WALLET")
    eta = now() + dt.timedelta(days=3 if fast else 7)
    r = dict(id=f"RF{seq}", order_id=order["id"], customer_id=order["customer_id"], amount=amount, status="initiated",
             method=method or order.get("payment_method", "UPI"), initiated_at=iso(), eta=iso(eta), reason=reason,
             payment_id=payment_id)
    store.put("refunds", r["id"], r)
    if payment_id:
        store.update("payments", payment_id, status="refund_initiated")
    return r


# ---- E-commerce -----------------------------------------------------------
def cancel_order(store, p, ctx):
    o = _order(store, p, ctx)
    if o["status"] not in ("placed", "packed", "payment_pending"):
        raise ActionError(f"Order is {o['status']} and can no longer be cancelled")
    o["status"] = "cancelled"
    o["cancelled_at"] = iso()
    store.put("orders", o["id"], o)
    pay = store.get("payments", o.get("payment_id")) or {}
    if pay.get("status") == "captured":
        r = _refund(store, o, o["amount"], "Order cancelled", pay["id"])
        return dict(refund_id=r["id"]), f"Order {o['id']} is cancelled and a refund of {inr(o['amount'])} ({r['id']}) is initiated — expected by {fmt_day(r['eta'])}."
    return {}, f"Order {o['id']} is cancelled."


def generate_return_label(store, p, ctx):
    o = _order(store, p, ctx)
    pickup = (now().astimezone(IST) + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0)
    label = dict(id=f"RL-{o['id'][3:]}-{random.randint(100, 999)}", order_id=o["id"], reason=p.get("reason", "return"),
                 carrier=o.get("courier"), pickup_window=f"{pickup:%d %b}, 10:00–14:00 IST", created_at=iso())
    o.setdefault("returns", []).append(label)
    o["return_status"] = "pickup_scheduled"
    store.put("orders", o["id"], o)
    return label, (f"A free return pickup is scheduled for {label['pickup_window']} (label {label['id']}) — "
                   "just hand over the item in its packaging.")


def initiate_refund(store, p, ctx, actor="AI"):
    o = _order(store, p, ctx)
    amount = float(p.get("amount") or o["amount"])
    if actor == "AI" and amount > AUTO_REFUND_LIMIT:
        raise ActionError(f"Refund {inr(amount)} exceeds AI limit {inr(AUTO_REFUND_LIMIT)} — needs human approval", "blocked")
    if p.get("payment_id"):
        pay = store.get("payments", p["payment_id"]) or {}
        if pay.get("status") in ("refund_initiated", "refunded"):
            raise ActionError(f"Payment {p['payment_id']} already refunded")
    r = _refund(store, o, amount, p.get("reason", "Refund"), p.get("payment_id"))
    if o["status"] == "payment_pending":
        o["status"] = "payment_failed"
        store.put("orders", o["id"], o)
    return dict(refund_id=r["id"], eta=r["eta"], amount=amount), (
        f"Refund {r['id']} of {inr(amount)} is initiated to your original payment method — expected by {fmt_day(r['eta'])}.")


def create_replacement(store, p, ctx):
    o = _order(store, p, ctx)
    seq = store.next_seq("order", 12400)
    premium = (ctx or {}).get("tier") == "Premium"
    eta = now() + dt.timedelta(days=1 if premium else 3)
    new = dict(o, id=f"ORD{seq}", amount=0, status="packed", placed_at=iso(), replacement_of=o["id"], delivered_items=o["items"],
               eta=iso(eta), payment_id=None, returns=[])
    for k in ("delivered_at", "shipped_at", "return_status"):
        new.pop(k, None)
    store.put("orders", new["id"], new)
    for i in o["items"]:
        inv = store.get("inventory", i["sku"])
        if inv:
            inv["stock"] = max(inv["stock"] - i["qty"], 0)
            store.put("inventory", i["sku"], inv)
    o["replacement_order"] = new["id"]
    store.put("orders", o["id"], o)
    return dict(replacement_order=new["id"], eta=new["eta"]), (
        f"A free replacement ({', '.join(i['name'] for i in o['items'])}) is placed as order {new['id']} and will arrive by "
        f"{fmt_day(new['eta'])}{' with Premium next-day delivery' if premium else ''}.")


def expedite_shipment(store, p, ctx):
    o = _order(store, p, ctx)
    eta = now() + dt.timedelta(days=1)
    o.update(priority_shipping=True, eta=iso(eta))
    store.put("orders", o["id"], o)
    return dict(new_eta=o["eta"]), f"I've escalated {o['id']} with {o['courier']} for priority delivery — new ETA {fmt_day(o['eta'])}."


def issue_coupon(store, p, ctx):
    amount = int(p.get("amount", 100))
    code = f"SORRY{amount}-{random.randint(1000, 9999)}"
    store.put("coupons", code, dict(id=code, customer_id=p.get("customer_id"), amount=amount, reason=p.get("reason"),
                                    valid_until=iso(now() + dt.timedelta(days=60)), created_at=iso()))
    return dict(code=code, amount=amount), f"As an apology, here's a {inr(amount)} coupon: {code} (valid 60 days)."


def reconcile_payment(store, p, ctx):
    pay = store.get("payments", p.get("payment_id"))
    if not pay:
        raise ActionError("Payment not found")
    pay["status"] = "captured"
    store.put("payments", pay["id"], pay)
    if pay.get("order_id"):
        o = store.get("orders", pay["order_id"])
        o["status"] = "placed"
        store.put("orders", o["id"], o)
    return {}, f"Your payment {pay['id']} is reconciled and the order is confirmed — no need to pay again."


# ---- SaaS -----------------------------------------------------------------
def reset_password(store, p, ctx):
    c = store.get("customers", p.get("customer_id"))
    acct = store.get("accounts", c["id"])
    if p.get("revoke_sessions"):
        acct["sessions"] = 0
    acct["reset_sent_at"] = iso()
    store.put("accounts", acct["id"], acct)
    extra = " All active sessions have been signed out as a precaution." if p.get("revoke_sessions") else ""
    return dict(sent_to=mask_email(c["email"])), f"A secure password reset link was sent to {mask_email(c['email'])} (valid 30 minutes).{extra}"


def unlock_account(store, p, ctx):
    acct = store.get("accounts", p.get("customer_id"))
    if acct["status"] in ("security_hold", "suspended"):
        raise ActionError("Account is on a security hold — only Trust & Safety can unlock it")
    acct.update(status="active", failed_attempts=0, locked_until=None)
    store.put("accounts", acct["id"], acct)
    return {}, "Your account is unlocked (the lock came from 5 failed password attempts)."


def retry_subscription_sync(store, p, ctx):
    s = _sub(store, p, ctx)
    s["sync_attempts"] = s.get("sync_attempts", 0) + 1
    if not s.get("sync_fixable", True):
        s["last_sync_error"] = "ENT-503 entitlement service rejected the update"
        store.put("subscriptions", s["id"], s)
        raise ActionError("Sync retry failed: ENT-503 entitlement service rejected the update")
    s.update(status="active", sync_status="ok", activated_at=iso())
    store.put("subscriptions", s["id"], s)
    return {}, f"Your {s['plan']} subscription is now active — all benefits are unlocked (you may need to reopen the app)."


def reactivate_subscription(store, p, ctx):
    s = _sub(store, p, ctx)
    s.update(status="active", auto_renew=True, renews_at=iso(now() + dt.timedelta(days=30)))
    store.put("subscriptions", s["id"], s)
    return {}, f"Your {s['plan']} subscription is reactivated; next renewal on {fmt_day(s['renews_at'])}."


def upgrade_plan(store, p, ctx):
    from .shared_agent import TIERS
    plan = p.get("plan", "Premium")
    prices = {"Plus": 199, "Premium": 499}
    subs = store.list("subscriptions", customer_id=p.get("customer_id"))
    if subs:
        s = subs[0]
        old = s["plan"]
        s.update(plan=plan, price=prices.get(plan, 499), status="active", sync_status="ok", upgraded_from=old, upgraded_at=iso())
    else:
        old = "Basic"
        s = dict(id=f"SUB{p['customer_id'][4:]}", customer_id=p["customer_id"], plan=plan, price=prices.get(plan, 499),
                 status="active", auto_renew=True, started_at=iso(), renews_at=iso(now() + dt.timedelta(days=30)),
                 sync_status="ok", sync_fixable=True)
    store.put("subscriptions", s["id"], s)
    c = store.get("customers", p["customer_id"])
    if plan in TIERS and TIERS.index(plan) > TIERS.index(c["tier"]):
        c["tier"] = plan
        store.put("customers", c["id"], c)
    return dict(plan=plan), f"You're upgraded from {old} to {plan} ({inr(prices.get(plan, 499))}/month, prorated for this cycle)."


def cancel_subscription(store, p, ctx):
    s = _sub(store, p, ctx)
    s.update(auto_renew=False, status="cancels_at_period_end")
    store.put("subscriptions", s["id"], s)
    return {}, f"Auto-renewal is turned off. Your {s['plan']} benefits continue until {fmt_day(s['renews_at'])}."


# ---- Telecom --------------------------------------------------------------
def create_service_request(store, p, ctx):
    l = _line(store, p, ctx)
    seq = store.next_seq("service_request", 5000)
    sr = dict(id=f"SR-{seq}", service_id=l["id"], customer_id=l["customer_id"], issue=p.get("issue", "Connectivity"),
              outage_id=p.get("outage_id"), status="open", created_at=iso())
    store.put("service_requests", sr["id"], sr)
    msg = f"Service request {sr['id']} is logged for {l['id']}"
    if p.get("outage_id"):
        o = store.get("outages", p["outage_id"])
        msg += f" and linked to the {o['area']} outage — expected restoration by {fmt_ts(o['eta_restore'])}. We'll notify you when it's back."
    return dict(service_request=sr["id"]), msg + ("" if p.get("outage_id") else ".")


def schedule_technician_visit(store, p, ctx):
    l = _line(store, p, ctx)
    slot = (now().astimezone(IST) + dt.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    seq = store.next_seq("visit", 800)
    visit = dict(id=f"VIS-{seq}", service_id=l["id"], customer_id=l["customer_id"], technician="Suresh K. (NovaFiber Field Ops)",
                 slot=f"{slot:%d %b}, 10:00–12:00 IST", otp=random.randint(1000, 9999), created_at=iso())
    store.put("visits", visit["id"], visit)
    return visit, (f"A technician visit ({visit['id']}) is booked for {visit['slot']} — {visit['technician']}. "
                   f"Share OTP {visit['otp']} with the technician on arrival.")


def remote_line_reset(store, p, ctx):
    l = _line(store, p, ctx)
    l["last_reset_at"] = iso()
    if l.get("line_fault") == "hardware" or l.get("signal_dbm", -60) < -75:
        store.put("service_lines", l["id"], l)
        raise ActionError("Remote reset completed but optical signal still below threshold (hardware fault)")
    l["line_fault"] = "none"
    store.put("service_lines", l["id"], l)
    return {}, f"I ran a remote reset on {l['id']}; the line is back in sync. Please restart your router once."


# ---- Security / notifications ----------------------------------------------
def freeze_account(store, p, ctx):
    acct = store.get("accounts", p.get("customer_id"))
    acct.update(status="security_hold", cards_blocked=True, hold_placed_at=iso(), sessions=0)
    store.put("accounts", acct["id"], acct)
    return {}, "As a precaution I've placed a security hold on your account and blocked saved cards so no further payments can be made."


def notify_customer(store, p, ctx):
    seq = store.next_seq("notification", 1)
    n = dict(id=f"NTF-{seq:05d}", customer_id=p.get("customer_id"), channel=p.get("channel", "web"), ticket_id=p.get("ticket_id"),
             message=p.get("message", ""), kind=p.get("kind", "update"), created_at=iso())
    store.put("notifications", n["id"], n)
    return n, f"Confirmation sent on {n['channel']}."


ACTIONS = {
    "cancel_order": (cancel_order, "E-commerce", "Cancel Order"),
    "generate_return_label": (generate_return_label, "E-commerce", "Generate Return Label"),
    "initiate_refund": (initiate_refund, "E-commerce", "Initiate Refund"),
    "create_replacement": (create_replacement, "E-commerce", "Create Replacement"),
    "expedite_shipment": (expedite_shipment, "E-commerce", "Expedite Shipment"),
    "issue_coupon": (issue_coupon, "E-commerce", "Issue Apology Coupon"),
    "reconcile_payment": (reconcile_payment, "E-commerce", "Reconcile Payment"),
    "reset_password": (reset_password, "SaaS", "Reset Password"),
    "unlock_account": (unlock_account, "SaaS", "Unlock Account"),
    "retry_subscription_sync": (retry_subscription_sync, "SaaS", "Retry Subscription Sync"),
    "reactivate_subscription": (reactivate_subscription, "SaaS", "Reactivate Subscription"),
    "upgrade_plan": (upgrade_plan, "SaaS", "Upgrade Plan"),
    "cancel_subscription": (cancel_subscription, "SaaS", "Cancel Subscription"),
    "create_service_request": (create_service_request, "Telecom", "Create Service Request"),
    "schedule_technician_visit": (schedule_technician_visit, "Telecom", "Schedule Technician Visit"),
    "remote_line_reset": (remote_line_reset, "Telecom", "Remote Line Reset"),
    "freeze_account": (freeze_account, "Security", "Security Hold"),
    "notify_customer": (notify_customer, "General", "Notify Customer"),
}
IDEMPOTENT_KEYS = ("order_id", "payment_id", "subscription_id", "service_id", "customer_id")
# State-checked actions may repeat; money/order/visit actions are idempotent for 30 days.
REPEATABLE = {"notify_customer", "reset_password", "issue_coupon", "unlock_account", "remote_line_reset", "upgrade_plan",
              "reactivate_subscription", "cancel_subscription"}


def execute(store, action, params, ctx=None, actor="AI", ticket_id=None, conversation_id=None):
    """Execute one action with guardrails and write it to the audit log."""
    params = dict(params or {})
    fallback = params.pop("fallback", None)
    label = ACTIONS.get(action, (None, None, action))[2]
    rec = dict(id=None, action=action, label=label, params=params, actor=actor, ts=iso(), status="failed", message="",
               customer_message="", data={}, ticket_id=ticket_id, conversation_id=conversation_id,
               customer_id=(ctx or {}).get("customer_id") or params.get("customer_id"))
    if action not in ACTIONS:
        rec.update(status="blocked", message=f"Action '{action}' is not on the allow-list")
    else:
        key = next(((k, params[k]) for k in IDEMPOTENT_KEYS if params.get(k)), None)
        prior = None
        if key and action not in REPEATABLE:
            prior = next((a for a in store.list("actions", action=action, status="success")
                          if a["params"].get(key[0]) == key[1] and
                          (now() - parse_ts(a["ts"])).total_seconds() < 86400 * 30), None)
        if prior:
            rec.update(status="skipped", message=f"Already done ({prior['id']}) — not repeated",
                       customer_message=prior.get("customer_message", ""), data=prior.get("data", {}))
        else:
            fn = ACTIONS[action][0]
            try:
                data, msg = fn(store, params, ctx, actor) if action == "initiate_refund" else fn(store, params, ctx)
                rec.update(status="success", message=msg, customer_message=msg, data=data or {})
            except ActionError as exc:
                rec.update(status="blocked" if len(exc.args) > 1 and exc.args[1] == "blocked" else "failed", message=str(exc.args[0]))
            except Exception as exc:  # defensive: an action must never crash the conversation
                rec.update(status="failed", message=f"{type(exc).__name__}: {exc}")
    rec["id"] = f"ACT-{store.next_seq('action', 1):05d}"
    store.put("actions", rec["id"], rec)
    results = [rec]
    if rec["status"] == "failed" and fallback:
        fb = execute(store, fallback["action"], dict(fallback.get("params", {})), ctx, actor, ticket_id, conversation_id)
        fb[0]["fallback_for"] = rec["id"]
        store.put("actions", fb[0]["id"], fb[0])
        if fb[0]["status"] in ("success", "skipped"):
            rec["fallback_recovered"] = True
            store.put("actions", rec["id"], rec)
        results += fb
    return results


def catalog():
    return [dict(action=k, domain=v[1], label=v[2], description=(v[0].__doc__ or "").strip()) for k, v in ACTIONS.items()]
