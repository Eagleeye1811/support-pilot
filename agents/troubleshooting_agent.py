"""Agent 4: Troubleshooting — investigate like a support engineer before answering.

Each intent has a diagnostic workflow that runs real checks against account
data (shipment scans, gateway status, lock counters, entitlement sync, line
diagnostics, outage map). The result is a list of evidence steps, a
diagnosis, a confidence, the team that owns it and recommended actions for the
Action Execution Agent. Nothing is answered from the LLM's imagination.
"""
from __future__ import annotations

from . import insights_agent
from .shared_agent import AUTO_REFUND_LIMIT, HIGH_VALUE_CLAIM, INTENTS, fmt_ts, inr, now, parse_ts

RETURN_WINDOW = {"Electronics": 10, "Fashion": 30, "Home": 15, "Books": 10, "Beauty": 7, "Grocery": 3}
SELLER_ERROR_WINDOW = 30
PLAN_RANK = {"Basic": 0, "Plus": 1, "Premium": 2}
NEEDS_IDENTITY = set(INTENTS) - {"general_inquiry", "legal_request", "human_agent", "ticket_status"}


class _Run:
    def __init__(self, intent, store, ctx, slots, ref):
        self.store, self.ctx, self.slots, self.ref = store, ctx, slots, ref
        self.res = dict(workflow=intent, steps=[], diagnosis="", resolvable=True, confidence=0.9, recommended_actions=[],
                        missing_slots=[], team=INTENTS[intent]["team"], facts={})

    def step(self, check, status, detail):
        self.res["steps"].append(dict(check=check, status=status, detail=detail))

    def act(self, action, why, protective=False, **params):
        self.res["recommended_actions"].append(dict(action=action, params=params, reason=why, protective=protective))

    def done(self, diagnosis, confidence, resolvable=True, team=None):
        self.res.update(diagnosis=diagnosis, confidence=round(confidence, 2), resolvable=resolvable)
        if team:
            self.res["team"] = team
        return self.res

    def ask(self, slot, note=None):
        self.res["missing_slots"].append(slot)
        if note:
            self.step("Collect details", "info", note)
        return self.done(note or f"Waiting for {slot.replace('_', ' ')}", 0.5)

    def days_since(self, ts):
        return (self.ref - parse_ts(ts)).total_seconds() / 86400

    def order(self, infer=None, describe="matching"):
        """Locate the order from the slot, or infer it when exactly one order fits."""
        cid = self.ctx["customer_id"]
        oid = self.slots.get("order_id")
        if oid:
            o = self.store.get("orders", oid)
            if not o:
                self.slots.pop("order_id", None)
                self.ask("order_id", f"Order {oid} was not found — asked the customer to re-check the Order ID.")
                return None
            if o["customer_id"] != cid:
                self.slots.pop("order_id", None)
                self.res["ownership_mismatch"] = True
                self.ask("order_id", f"Order {oid} is not linked to this account — no details disclosed.")
                return None
            self.step("Verify order ownership", "pass", f"{o['id']} belongs to {self.ctx['name']} · {inr(o['amount'])} · status {o['status']}")
            return o
        if infer:
            candidates = [o for o in self.store.list("orders", customer_id=cid) if infer(o)]
            if len(candidates) == 1:
                o = candidates[0]
                self.slots["order_id"] = o["id"]
                self.step("Identify order", "info", f"Inferred {o['id']} — the only {describe} order on the account")
                return o
        self.ask("order_id", "Need the Order ID to investigate the right order.")
        return None


def _names(items):
    return ", ".join(i["name"] for i in items)


def _in_stock(store, order):
    return all((store.get("inventory", i["sku"]) or {}).get("stock", 0) > 0 for i in order["items"])


# ---- E-commerce workflows -------------------------------------------------
def _wrong_item(r):
    o = r.order(lambda o: o["status"] == "delivered" and r.days_since(o.get("delivered_at") or o["placed_at"]) <= 7,
                "recently delivered")
    if not o:
        return r.res
    r.res["facts"]["courier"] = o["courier"]
    if o["status"] != "delivered":
        r.step("Verify shipment", "fail", f"Order is '{o['status']}', not delivered yet")
        return r.done("Customer reports a wrong item but the order is not delivered yet.", 0.5, resolvable=False)
    r.step("Verify shipment", "pass", f"Delivered {fmt_ts(o['delivered_at'])} by {o['courier']} (AWB {o['awb']})")
    ordered, delivered = {i["sku"] for i in o["items"]}, {i["sku"] for i in o["delivered_items"]}
    if ordered != delivered:
        r.step("Compare purchased vs delivered item", "fail",
               f"Ordered: {_names(o['items'])} · Dispatch scan: {_names(o['delivered_items'])}")
    else:
        r.step("Compare purchased vs delivered item", "warn", "Dispatch scan matches the order — mismatch not confirmed by records")
        return r.done("Customer reports a wrong item but the warehouse dispatch scan matches the order; needs photo "
                      "verification by Fulfilment.", 0.55, resolvable=False)
    days = r.days_since(o["delivered_at"])
    if days > SELLER_ERROR_WINDOW:
        r.step("Check return policy", "fail", f"Delivered {days:.0f} days ago — beyond the {SELLER_ERROR_WINDOW}-day seller-error window")
        return r.done("Wrong item confirmed but reported outside the policy window.", 0.6, resolvable=False)
    r.step("Check return policy", "pass", f"Delivered {days:.0f} day(s) ago — within {SELLER_ERROR_WINDOW}-day seller-error window (KB-001)")
    if o["amount"] > HIGH_VALUE_CLAIM:
        r.step("Check claim value", "warn", f"{inr(o['amount'])} exceeds the {inr(HIGH_VALUE_CLAIM)} auto-approval limit")
        r.act("generate_return_label", "Collect the wrong item while approval is pending", protective=True, order_id=o["id"], reason="wrong_item")
        return r.done("Wrong item confirmed; high-value replacement needs Fulfilment approval.", 0.65, resolvable=False)
    if _in_stock(r.store, o):
        r.step("Check replacement stock", "pass", f"{_names(o['items'])} in stock")
        r.act("create_replacement", "Seller error confirmed by dispatch scan", order_id=o["id"])
    else:
        r.step("Check replacement stock", "warn", "Ordered item out of stock — full refund instead")
        r.act("initiate_refund", "Replacement unavailable", order_id=o["id"], amount=o["amount"], reason="Wrong item, out of stock")
    r.act("generate_return_label", "Free pickup of the wrong item", order_id=o["id"], reason="wrong_item")
    return r.done(f"Warehouse dispatched {_names(o['delivered_items'])} instead of {_names(o['items'])}. Eligible for a free "
                  "replacement and pickup.", 0.93)


def _damaged_item(r):
    o = r.order(lambda o: o["status"] == "delivered" and r.days_since(o.get("delivered_at") or o["placed_at"]) <= SELLER_ERROR_WINDOW,
                "recently delivered")
    if not o:
        return r.res
    r.res["facts"]["courier"] = o["courier"]
    if o["status"] != "delivered":
        r.step("Verify delivery", "fail", f"Order is '{o['status']}'")
        return r.done("Damage reported for an order that is not delivered.", 0.5, resolvable=False)
    days = r.days_since(o["delivered_at"])
    r.step("Verify delivery", "pass", f"Delivered {fmt_ts(o['delivered_at'])} ({days:.0f} day(s) ago)")
    if days > SELLER_ERROR_WINDOW:
        r.step("Check policy window", "fail", f"Beyond {SELLER_ERROR_WINDOW}-day damage window — warranty route applies (KB-027)")
        return r.done("Outside replacement window; brand warranty applies.", 0.82)
    r.step("Check policy window", "pass", f"Within {SELLER_ERROR_WINDOW}-day damage window (KB-007)")
    if o["amount"] > HIGH_VALUE_CLAIM:
        r.step("Check claim value", "warn", f"{inr(o['amount'])} exceeds {inr(HIGH_VALUE_CLAIM)} — quality check & approval required")
        r.act("generate_return_label", "Pick up the item for quality inspection", protective=True, order_id=o["id"], reason="damaged_qc")
        return r.done(f"High-value damage claim ({_names(o['items'])}, {inr(o['amount'])}) needs Fulfilment QC approval before "
                      "replacement.", 0.62, resolvable=False)
    r.step("Check claim value", "pass", f"{inr(o['amount'])} within auto-approval limit")
    if _in_stock(r.store, o):
        r.act("create_replacement", "Damaged on arrival", order_id=o["id"])
    else:
        r.act("initiate_refund", "Replacement unavailable", order_id=o["id"], amount=o["amount"], reason="Damaged item, out of stock")
    r.act("generate_return_label", "Free pickup of the damaged item", order_id=o["id"], reason="damaged_item")
    return r.done("Damaged item within policy; replacement and free pickup approved.", 0.9)


def _payment_failure(r, o=None):
    o = o or r.order()
    if not o:
        return r.res
    pays = [p for p in r.store.list("payments", order_id=o["id"])]
    if not pays:
        r.step("Locate payment", "fail", "No payment attempt recorded for this order")
        return r.done("No payment found for the order.", 0.55, resolvable=False)
    p = sorted(pays, key=lambda x: x["created_at"])[-1]
    r.res["facts"].update(gateway=p["gateway"], payment_id=p["id"])
    r.step("Locate payment", "pass", f"{p['id']} · {inr(p['amount'])} · {p['method']} via {p['gateway']} at {fmt_ts(p['created_at'])}")
    if p["status"] == "captured":
        r.step("Check gateway status", "pass", "Payment captured successfully")
        if o["status"] == "payment_pending":
            r.act("reconcile_payment", "Captured payment not reflected on order", payment_id=p["id"])
        return r.done("Payment actually succeeded; order confirmation needed only.", 0.9)
    if p["status"] in ("refund_initiated", "refunded"):
        r.step("Check gateway status", "info", f"Payment already {p['status'].replace('_', ' ')}")
        return r.done("Refund for this failed payment is already in progress.", 0.9)
    r.step("Check gateway status", "fail", f"FAILED — {p.get('failure_reason', 'declined by gateway')}")
    if not p.get("bank_debited"):
        r.step("Confirm bank debit", "pass", "No debit at the bank — no money lost")
        return r.done("Payment failed before any debit; it is safe to retry.", 0.85)
    r.step("Confirm bank debit", "warn", f"Bank debit confirmed: {inr(p['amount'])} debited")
    inc = insights_agent.incident_for(r.store, "payment_failure", p["gateway"], r.ref)
    if inc:
        r.step("Check gateway health", "warn", f"{inc['hypothesis']} — {inc['recent_count']} failures in 24h ({inc['spike_ratio']}× normal)")
        r.res["facts"]["incident"] = inc["id"]
    else:
        r.step("Check gateway health", "pass", f"No active incident on {p['gateway']}")
    if p.get("late_capture"):
        r.act("reconcile_payment", "Gateway captured late — confirm the order", payment_id=p["id"])
        return r.done("Payment was captured late by the gateway; confirming the order.", 0.9)
    r.act("initiate_refund", "Auto-reversal of a debit for a failed payment", order_id=o["id"], amount=p["amount"],
          payment_id=p["id"], reason="Auto-reversal: payment failed after bank debit")
    return r.done(f"Gateway timed out after the bank debit; payment was never captured, so the {inr(p['amount'])} debit is "
                  "reversed (auto-reversal).", 0.92)


def _double_charge(r):
    def dup(o):
        return len([p for p in r.store.list("payments", order_id=o["id"]) if p["status"] == "captured"]) >= 2
    o = r.order(dup, "double-charged")
    if not o:
        return r.res
    caps = sorted([p for p in r.store.list("payments", order_id=o["id"]) if p["status"] == "captured"], key=lambda p: p["created_at"])
    if len(caps) >= 2:
        r.res["facts"]["gateway"] = caps[-1]["gateway"]
        r.step("Check captures for the order", "fail", f"{len(caps)} captures of {inr(caps[-1]['amount'])}: "
               + ", ".join(p["id"] for p in caps))
        r.step("Check refund policy", "pass", "Duplicate charges are always refunded in full (KB-002)")
        r.act("initiate_refund", "Duplicate capture", order_id=o["id"], amount=caps[-1]["amount"], payment_id=caps[-1]["id"],
              reason="Duplicate charge")
        return r.done(f"Duplicate capture confirmed for {o['id']}; refunding the extra {inr(caps[-1]['amount'])}.", 0.95)
    r.step("Check captures for the order", "pass", "Only one successful capture")
    return r.done("No duplicate capture in our records — the second entry is likely a temporary bank authorisation hold "
                  "that auto-releases within 7 days.", 0.72)


def _refund_request(r):
    o = r.order(lambda o: o["status"] in ("delivered", "placed", "packed") and r.days_since(o["placed_at"]) <= 45, "eligible")
    if not o:
        return r.res
    existing = [x for x in r.store.list("refunds", order_id=o["id"])]
    if existing:
        x = existing[-1]
        r.step("Check existing refunds", "info", f"Refund {x['id']} of {inr(x['amount'])} already {x['status']}")
        return r.done("A refund is already in progress for this order.", 0.9)
    if o["status"] == "payment_pending":
        return _payment_failure(r, o)
    if o["status"] in ("placed", "packed"):
        r.step("Check order status", "pass", f"Not shipped yet ({o['status']}) — cancellation gives an automatic refund (KB-009)")
        r.act("cancel_order", "Customer wants money back before dispatch", order_id=o["id"])
        return r.done("Order not shipped; cancel and refund.", 0.92)
    if o["status"] in ("shipped", "out_for_delivery"):
        r.step("Check order status", "info", "In transit — refund possible after delivery or by refusing the delivery")
        return r.done("Order in transit; customer advised to refuse delivery for an automatic refund.", 0.85)
    cat = o["items"][0]["category"]
    window = RETURN_WINDOW.get(cat, 15)
    days = r.days_since(o["delivered_at"])
    if days > window:
        r.step("Check return window", "fail", f"{cat} return window is {window} days; delivered {days:.0f} days ago")
        return r.done("Outside the return window; not eligible for refund (warranty route explained).", 0.8)
    r.step("Check return window", "pass", f"{cat}: {window}-day window, delivered {days:.0f} day(s) ago")
    if o["amount"] > AUTO_REFUND_LIMIT:
        r.step("Check refund limit", "warn", f"{inr(o['amount'])} exceeds AI refund limit {inr(AUTO_REFUND_LIMIT)}")
        r.act("generate_return_label", "Collect the item while Payments approves", protective=True, order_id=o["id"], reason="refund")
        return r.done("Eligible return, but refund amount needs Payments approval.", 0.66, resolvable=False)
    r.step("Check refund limit", "pass", f"{inr(o['amount'])} within AI refund limit")
    r.act("generate_return_label", "Return pickup", order_id=o["id"], reason="refund")
    r.act("initiate_refund", "Eligible return within window", order_id=o["id"], amount=o["amount"], reason="Customer return")
    return r.done("Eligible for return and refund within policy.", 0.88)


def _refund_status(r):
    refunds = r.store.list("refunds", customer_id=r.ctx["customer_id"])
    if r.slots.get("order_id"):
        refunds = [x for x in refunds if x["order_id"] == r.slots["order_id"]] or refunds
    if not refunds:
        r.step("Find refunds", "fail", "No refund exists on this account")
        return r.done("Customer expects a refund but none has been initiated.", 0.55, resolvable=False)
    x = sorted(refunds, key=lambda z: z["initiated_at"])[-1]
    r.step("Find refunds", "pass", f"{x['id']} · {inr(x['amount'])} for {x['order_id']} via {x['method']} · {x['status']}")
    late = (r.ref - parse_ts(x["eta"])).total_seconds() / 86400
    if x["status"] != "completed" and late > 0:
        r.step("Check ETA", "fail", f"ETA {fmt_ts(x['eta'])} passed {late:.1f} day(s) ago")
        return r.done("Refund overdue — Payments must raise a bank trace and share the ARN.", 0.6, resolvable=False)
    r.step("Check ETA", "pass", f"On track — expected by {fmt_ts(x['eta'])}")
    r.res["facts"]["refund"] = x
    return r.done(f"Refund {x['id']} of {inr(x['amount'])} is {x['status']} and on track for {fmt_ts(x['eta'])}.", 0.92)


def _delivery_delay(r):
    o = r.order(lambda o: o["status"] in ("shipped", "out_for_delivery"), "in-transit")
    if not o:
        return r.res
    r.res["facts"]["courier"] = o["courier"]
    if o["status"] == "delivered":
        r.step("Check shipment status", "warn", f"Courier marked it delivered on {fmt_ts(o['delivered_at'])}")
        return r.done("Courier shows delivered but customer has not received it — possible misdelivery.", 0.58, resolvable=False)
    if o["status"] in ("placed", "packed"):
        r.step("Check shipment status", "pass", f"{o['status'].title()} — dispatch within 24h, ETA {fmt_ts(o['eta'])}")
        return r.done("Order is on schedule.", 0.9)
    if o["status"] in ("cancelled", "returned"):
        r.step("Check shipment status", "info", f"Order is {o['status']}")
        return r.done(f"Order was {o['status']}.", 0.85)
    late = (r.ref - parse_ts(o["eta"])).total_seconds() / 86400
    r.step("Check shipment status", "pass", f"In transit with {o['courier']} (AWB {o['awb']}), shipped {fmt_ts(o.get('shipped_at'))}")
    if late >= 1:
        r.step("Compare with promised ETA", "fail", f"ETA was {fmt_ts(o['eta'])} — {late:.0f} day(s) late")
        inc = insights_agent.incident_for(r.store, "delivery_delay", o["courier"], r.ref)
        r.step("Check courier health", "warn" if inc else "pass", inc["hypothesis"] if inc else f"No network-wide disruption at {o['courier']}")
        r.act("expedite_shipment", "Shipment past ETA", order_id=o["id"])
        r.act("issue_coupon", "Apology for the delay", customer_id=r.ctx["customer_id"],
              amount=200 if r.ctx["tier"] == "Premium" else 100, reason=f"Delay on {o['id']}")
        return r.done(f"Shipment is {late:.0f} day(s) late; expediting with the courier and compensating.", 0.88)
    r.step("Compare with promised ETA", "pass", f"On track — ETA {fmt_ts(o['eta'])}")
    return r.done("Shipment on track.", 0.92)


def _cancel_order(r):
    o = r.order(lambda o: o["status"] in ("placed", "packed"), "cancellable")
    if not o:
        return r.res
    if o["status"] in ("placed", "packed"):
        r.step("Check cancellation eligibility", "pass", f"Status '{o['status']}' — not shipped, free cancellation (KB-009)")
        r.act("cancel_order", "Customer requested cancellation", order_id=o["id"])
        return r.done("Order not yet shipped; cancelling with automatic refund.", 0.95)
    if o["status"] == "cancelled":
        r.step("Check cancellation eligibility", "info", "Already cancelled")
        return r.done("Order already cancelled.", 0.9)
    if o["status"] in ("shipped", "out_for_delivery"):
        r.step("Check cancellation eligibility", "fail", "Already shipped — cannot be cancelled")
        return r.done("Order already shipped; customer can refuse delivery or return it after delivery.", 0.85)
    return _return_request(r, o)


def _return_request(r, o=None):
    o = o or r.order(lambda o: o["status"] == "delivered" and r.days_since(o.get("delivered_at") or o["placed_at"]) <= 30, "returnable")
    if not o:
        return r.res
    if o["status"] != "delivered":
        r.step("Check return eligibility", "info", f"Order is '{o['status']}' — returns start after delivery")
        return r.done("Order not delivered yet.", 0.85)
    cat = o["items"][0]["category"]
    window = RETURN_WINDOW.get(cat, 15)
    days = r.days_since(o["delivered_at"])
    if days > window:
        r.step("Check return window", "fail", f"{cat} window is {window} days; delivered {days:.0f} days ago")
        return r.done("Return window has ended.", 0.85)
    r.step("Check return window", "pass", f"{cat}: {window}-day window, delivered {days:.0f} day(s) ago (KB-001)")
    r.act("generate_return_label", "Eligible return", order_id=o["id"], reason="customer_return")
    return r.done("Eligible for return; free pickup scheduled, refund on pickup QC.", 0.9)


# ---- SaaS / account workflows -------------------------------------------
def _login(r, reset_only=False):
    acct = r.store.get("accounts", r.ctx["customer_id"]) or {}
    status, attempts = acct.get("status", "active"), acct.get("failed_attempts", 0)
    r.step("Check account status", "pass" if status == "active" else "fail", f"Account status: {status.replace('_', ' ')}")
    r.step("Check password attempts", "fail" if attempts >= 5 else "pass", f"{attempts} failed attempt(s) (lock threshold 5)")
    locked = status == "locked" or attempts >= 5
    r.step("Check lock status", "fail" if locked else "pass",
           f"Locked until {fmt_ts(acct.get('locked_until'))}" if locked and acct.get("locked_until") else ("Locked" if locked else "Not locked"))
    if status in ("security_hold", "suspended"):
        return r.done("Account is on a security hold — only Trust & Safety can release it.", 0.5, resolvable=False,
                      team="Trust & Safety")
    if locked:
        r.act("unlock_account", "Locked by failed password attempts", customer_id=r.ctx["customer_id"])
        r.act("reset_password", "Customer cannot remember password", customer_id=r.ctx["customer_id"])
        return r.done("Account auto-locked after 5 failed password attempts; unlocking and sending a reset link.", 0.93)
    r.act("reset_password", "Secure reset link", customer_id=r.ctx["customer_id"])
    return r.done("Account is healthy; a password reset link resolves forgotten credentials." if reset_only else
                  "Account healthy; likely forgotten password or stale app session — reset link sent, clear app cache.",
                  0.95 if reset_only else 0.84)


def _subscription_not_active(r):
    sub = r.ctx.get("subscription")
    if not sub:
        r.step("Find subscription", "fail", "No subscription on this account")
        return r.done("No subscription found; customer may have paid from another account.", 0.5, resolvable=False)
    r.res["facts"]["subscription_id"] = sub["id"]
    pays = sorted([p for p in r.store.list("payments", customer_id=r.ctx["customer_id"]) if p.get("subscription_id") == sub["id"]],
                  key=lambda p: p["created_at"])
    if pays and pays[-1]["status"] == "captured":
        p = pays[-1]
        r.step("Check payment logs", "pass", f"Payment successful: {p['id']} {inr(p['amount'])} captured {fmt_ts(p['created_at'])}")
    elif sub["status"] == "active":
        r.step("Check payment logs", "pass", "Subscription billed on the regular cycle")
    else:
        r.step("Check payment logs", "fail", "No successful subscription payment")
        return r.done("Subscription payment not completed — customer needs to retry payment.", 0.8)
    if sub["status"] == "active":
        r.step("Check activation status", "pass", f"{sub['plan']} is active — benefits may need an app refresh/re-login")
        return r.done(f"{sub['plan']} subscription is already active.", 0.86)
    r.step("Check activation status", "fail", f"{sub['plan']} status: {sub['status'].replace('_', ' ')}")
    r.step("Check entitlement sync", "fail" if sub.get("sync_status") == "failed" else "pass",
           "Subscription sync failed (entitlement service)" if sub.get("sync_status") == "failed" else "Sync healthy")
    r.act("retry_subscription_sync", "Payment successful but entitlement not synced", subscription_id=sub["id"])
    return r.done("Payment successful but entitlement sync failed; triggering a sync retry.", 0.82, team="Engineering")


def _reactivate(r):
    sub = r.ctx.get("subscription")
    if not sub:
        r.step("Find subscription", "info", "No previous subscription — offering a new plan")
        r.act("upgrade_plan", "Start a new membership", customer_id=r.ctx["customer_id"], plan=r.slots.get("plan", "Plus"))
        return r.done("No subscription to reactivate; starting a new one.", 0.8)
    if sub["status"] in ("expired", "cancelled", "cancels_at_period_end"):
        r.step("Check subscription status", "fail", f"{sub['plan']} is {sub['status'].replace('_', ' ')}")
        r.act("reactivate_subscription", "Customer asked to reactivate", subscription_id=sub["id"])
        return r.done("Subscription inactive; reactivating with saved payment method.", 0.9)
    r.step("Check subscription status", "pass", f"{sub['plan']} already {sub['status'].replace('_', ' ')}, renews {fmt_ts(sub['renews_at'])}")
    return r.done("Subscription is already active.", 0.88)


def _upgrade(r):
    sub = r.ctx.get("subscription")
    current = sub["plan"] if sub else "Basic"
    target = r.slots.get("plan") or "Premium"
    r.step("Check current plan", "pass", f"Current plan: {current}{' (' + sub['status'].replace('_', ' ') + ')' if sub else ''}")
    if PLAN_RANK.get(target, 0) <= PLAN_RANK.get(current, 0):
        r.step("Validate target plan", "info", f"Already on {current}; {target} is not an upgrade")
        return r.done(f"Customer is already on {current}.", 0.85)
    r.step("Validate target plan", "pass", f"{current} → {target} (prorated, KB-014)")
    r.act("upgrade_plan", "Customer requested upgrade", customer_id=r.ctx["customer_id"], plan=target)
    return r.done(f"Upgrading {current} → {target}.", 0.9)


def _cancel_sub(r):
    sub = r.ctx.get("subscription")
    if not sub or sub["status"] in ("cancelled", "expired", "cancels_at_period_end"):
        r.step("Check subscription status", "info", "No active subscription to cancel")
        return r.done("Nothing to cancel.", 0.88)
    r.step("Check subscription status", "pass", f"{sub['plan']} active, renews {fmt_ts(sub['renews_at'])}")
    r.act("cancel_subscription", "Customer requested cancellation", subscription_id=sub["id"])
    return r.done("Cancelling auto-renewal; benefits continue until period end (KB-016).", 0.9)


# ---- Telecom workflows ----------------------------------------------------
def _line(r):
    lines = r.store.list("service_lines", customer_id=r.ctx["customer_id"])
    sid = r.slots.get("service_id")
    if sid:
        lines = [l for l in lines if l["id"] == sid] or lines
    if len(lines) != 1:
        r.ask("service_id", "Need the NovaFiber service ID (SRV…) to run diagnostics." if lines else
              "No NovaFiber connection found on this account — asked for the service ID.")
        return None
    line = lines[0]
    r.slots["service_id"] = line["id"]
    r.res["facts"]["area"] = line["area"]
    r.step("Identify service line", "pass", f"{line['id']} · {line['plan']} · {line['area']} · router {line['router']}")
    return line


def _network(r):
    line = _line(r)
    if not line:
        return r.res
    outages = [o for o in r.store.list("outages", status="active") if o["area"] == line["area"]]
    if outages:
        o = outages[0]
        r.step("Check area outage map", "fail", f"Active outage in {o['area']} since {fmt_ts(o['started_at'])}: {o['cause']}")
        r.step("Estimate restoration", "info", f"Restoration ETA {fmt_ts(o['eta_restore'])}")
        r.act("create_service_request", "Link customer to the area outage", service_id=line["id"], outage_id=o["id"],
              issue="Area outage")
        return r.done(f"Area-wide outage in {o['area']} (not a line fault); restoration ETA {fmt_ts(o['eta_restore'])}.", 0.95)
    r.step("Check area outage map", "pass", f"No outage in {line['area']}")
    weak = line["signal_dbm"] < -75
    r.step("Run remote line diagnostics", "fail" if weak or line["line_fault"] != "none" else "pass",
           f"Optical signal {line['signal_dbm']} dBm (healthy ≥ -75) · fault: {line['line_fault']}")
    if line["line_fault"] == "hardware" or weak:
        r.act("remote_line_reset", "Try a remote reset first", service_id=line["id"],
              fallback=dict(action="schedule_technician_visit", params=dict(service_id=line["id"]), reason="Hardware fault"))
        return r.done("Weak optical signal indicates a hardware/fibre fault; remote reset first, technician visit as fallback.", 0.86)
    r.act("remote_line_reset", "Clear line/router configuration", service_id=line["id"])
    return r.done("No outage and no hardware fault; a remote line reset clears configuration issues.", 0.84)


def _technician(r):
    line = _line(r)
    if not line:
        return r.res
    r.act("schedule_technician_visit", "Customer requested a visit", service_id=line["id"])
    return r.done("Scheduling a free technician visit (KB-019).", 0.9)


# ---- Security / legal -----------------------------------------------------
def _fraud(r):
    txns = sorted(r.store.list("payments", customer_id=r.ctx["customer_id"]), key=lambda p: p["created_at"], reverse=True)[:4]
    suspicious = [p for p in txns if p.get("merchant_note") or (p["amount"] >= 10000 and r.days_since(p["created_at"]) <= 1)]
    r.step("Review recent transactions", "warn" if suspicious else "info",
           "; ".join(f"{p['id']} {inr(p['amount'])} {p.get('method', '')} {p.get('merchant_note') or ''}".strip() for p in suspicious)
           or "No obviously anomalous transaction — customer report still taken seriously")
    acct = r.store.get("accounts", r.ctx["customer_id"]) or {}
    r.step("Check account security", "info", f"Last login {fmt_ts(acct.get('last_login'))}, {acct.get('sessions', 1)} active session(s)")
    r.act("freeze_account", "Protect the customer while Trust & Safety investigates", protective=True, customer_id=r.ctx["customer_id"])
    if suspicious:
        r.res["facts"]["suspicious_txn"] = suspicious[0]["id"]
    return r.done("Customer reports unauthorized activity" + (f" ({suspicious[0]['id']}, {inr(suspicious[0]['amount'])})" if suspicious else "")
                  + "; protective security hold placed.", 0.95, resolvable=False, team="Trust & Safety")


def _breach(r):
    r.step("Classify incident", "warn", "Possible personal-data exposure — mandatory Security Incident Response")
    r.act("reset_password", "Precaution: revoke sessions and force reset", protective=True, customer_id=r.ctx["customer_id"],
          revoke_sessions=True)
    return r.done("Suspected data exposure; sessions revoked as precaution.", 0.95, resolvable=False, team="Security Incident Response")


def _legal(r):
    r.step("Classify legal request", "warn", "Legal notice / consumer forum / data-erasure request — Legal & Privacy only (KB-022)")
    return r.done("Legal or regulatory request; no commitments made by the assistant.", 0.95, resolvable=False, team="Legal & Privacy")


WORKFLOWS = {
    "wrong_item": _wrong_item, "damaged_item": _damaged_item, "payment_failure": _payment_failure,
    "double_charge": _double_charge, "refund_request": _refund_request, "refund_status": _refund_status,
    "delivery_delay": _delivery_delay, "cancel_order": _cancel_order, "return_request": _return_request,
    "login_issue": lambda r: _login(r), "password_reset": lambda r: _login(r, reset_only=True),
    "subscription_not_active": _subscription_not_active, "reactivate_subscription": _reactivate,
    "upgrade_plan": _upgrade, "cancel_subscription": _cancel_sub, "network_issue": _network,
    "technician_visit": _technician, "fraud_report": _fraud, "data_breach": _breach, "legal_request": _legal,
}


def investigate(store, intent, ctx, slots, articles=None, ref=None, kb_conf=None):
    """Run the diagnostic workflow for an intent. `slots` may be updated with inferred ids."""
    ref = ref or now()
    r = _Run(intent if intent in INTENTS else "general_inquiry", store, ctx, slots, ref)
    if intent in NEEDS_IDENTITY and not ctx:
        if intent in ("fraud_report", "data_breach"):
            r.step("Identify customer", "warn", "Customer not identified — escalating immediately for safety")
            return r.done("Security report from an unidentified customer.", 0.9, resolvable=False)
        return r.ask("customer_identity", "Customer not identified — need registered email, phone or an Order ID.")
    fn = WORKFLOWS.get(intent)
    if fn:
        return fn(r)
    # general inquiry → answer from the knowledge base
    if articles:
        top = articles[0]
        r.step("Search knowledge base", "pass" if (kb_conf or 0) >= 0.55 else "warn",
               f"Best match {top['id']} '{top['title']}' (relevance {top['relevance']:.0%}, reliability {top['reliability']:.0%})")
    else:
        r.step("Search knowledge base", "fail", "No relevant article found")
    conf = kb_conf if kb_conf is not None else 0.3
    return r.done("Answered from the knowledge base." if conf >= 0.55 else "Knowledge base has no confident answer.", conf,
                  resolvable=conf >= 0.55)
