"""Agent 2: Customer Context — who is this customer before we answer?

Resolves the customer (logged-in id, email, phone, order id or ticket id) and
assembles account details, purchase history, previous tickets, subscription,
recent transactions, auth status and telecom lines into one profile.
"""
from __future__ import annotations

from .shared_agent import humanize_days, inr, now, parse_ts

OPEN_STATUSES = {"Opened", "Assigned", "Pending Customer"}


def _same(key, stored, value):
    stored = str(stored or "").lower()
    if key == "phone":
        return len(value) >= 10 and stored[-10:] == value[-10:]
    return stored == value


def resolve_customer_id(store, customer_id=None, entities=None, sender=None):
    """Return (customer_id, method) using the strongest identifier available."""
    entities = entities or {}
    if customer_id and store.get("customers", customer_id):
        return customer_id, "session"
    if entities.get("customer_id") and store.get("customers", entities["customer_id"]):
        return entities["customer_id"], "customer id"
    for key in ("email", "phone"):
        value = str(entities.get(key) or (sender or {}).get(key) or "").lower()
        if value:
            match = store.list("customers", where=lambda c: _same(key, c.get(key), value))
            if match:
                return match[0]["id"], key
    if entities.get("order_id"):
        order = store.get("orders", entities["order_id"])
        if order:
            return order["customer_id"], "order id"
    if entities.get("ticket_id"):
        ticket = store.get("tickets", entities["ticket_id"])
        if ticket:
            return ticket["customer_id"], "ticket id"
    return None, None


def customer_context(store, customer_id, ref=None):
    if not customer_id:
        return None
    c = store.get("customers", customer_id)
    if not c:
        return None
    ref = ref or now()
    orders = sorted(store.list("orders", customer_id=customer_id), key=lambda o: o["placed_at"], reverse=True)
    payments = sorted(store.list("payments", customer_id=customer_id), key=lambda p: p["created_at"], reverse=True)
    tickets = sorted(store.list("tickets", customer_id=customer_id), key=lambda t: t["created_at"], reverse=True)
    subs = store.list("subscriptions", customer_id=customer_id)
    lines = store.list("service_lines", customer_id=customer_id)
    refunds = store.list("refunds", customer_id=customer_id)
    account = store.get("accounts", customer_id) or {}
    csat = [r["rating"] for r in store.list("csat", customer_id=customer_id)]

    last_purchase = max([o["placed_at"] for o in orders] + [p["created_at"] for p in payments if p.get("purpose") == "subscription"],
                        default=None)
    ltv = sum(o["amount"] for o in orders if o["status"] not in ("cancelled",))
    open_tickets = [t for t in tickets if t.get("status") in OPEN_STATUSES]
    recent_30 = [t for t in tickets if (ref - parse_ts(t["created_at"])).days <= 30]
    sub = subs[0] if subs else None

    flags = []
    if c["tier"] == "Premium":
        flags.append("Premium User")
    if len(recent_30) >= 3:
        flags.append(f"Repeat contact ({len(recent_30)} tickets in 30 days)")
    if open_tickets:
        flags.append(f"{len(open_tickets)} open ticket(s)")
    if ltv >= 50000:
        flags.append("High lifetime value")
    if csat and sum(csat) / len(csat) < 3:
        flags.append("At-risk: low past CSAT")
    if account.get("status") in ("locked", "security_hold", "suspended"):
        flags.append(f"Account {account['status'].replace('_', ' ')}")

    headline = " · ".join(filter(None, [
        f"{c['tier']} User",
        f"Tickets: {len(tickets)} previous issue{'s' if len(tickets) != 1 else ''}",
        f"Last purchase: {humanize_days(last_purchase, ref)}" if last_purchase else "No purchases yet",
        f"Subscription: {sub['plan']} ({sub['status'].replace('_', ' ')})" if sub else None,
    ]))

    return dict(
        customer_id=customer_id, name=c["name"], first_name=c["name"].split()[0], email=c["email"], phone=c["phone"],
        tier=c["tier"], city=c.get("city"), customer_since=c.get("customer_since"), lifetime_value=ltv,
        lifetime_value_fmt=inr(ltv), headline=headline, flags=flags,
        orders=[dict(id=o["id"], status=o["status"], amount=o["amount"], placed_at=o["placed_at"],
                     items=", ".join(i["name"] for i in o["items"]), delivered_at=o.get("delivered_at")) for o in orders[:6]],
        order_count=len(orders), last_purchase=last_purchase,
        last_purchase_human=humanize_days(last_purchase, ref) if last_purchase else None,
        previous_tickets=dict(count=len(tickets), open=len(open_tickets), last_30_days=len(recent_30),
                              recent=[dict(id=t["id"], intent=t["intent"], status=t["status"], created_at=t["created_at"])
                                      for t in tickets[:5]],
                              intents=[t["intent"] for t in tickets]),
        subscription=sub, account=dict(status=account.get("status", "active"), failed_attempts=account.get("failed_attempts", 0),
                                       two_factor=account.get("two_factor"), last_login=account.get("last_login"),
                                       locked_until=account.get("locked_until")),
        recent_transactions=[dict(id=p["id"], amount=p["amount"], status=p["status"], method=p.get("method"),
                                  created_at=p["created_at"], purpose=p.get("purpose"), order_id=p.get("order_id"),
                                  note=p.get("merchant_note")) for p in payments[:5]],
        refunds=refunds, service_lines=lines, avg_csat=round(sum(csat) / len(csat), 2) if csat else None,
    )


def compact(ctx):
    """Small view of the context for traces, tickets and MCP responses."""
    if not ctx:
        return None
    return {k: ctx[k] for k in ("customer_id", "name", "tier", "headline", "flags", "lifetime_value_fmt")} | {
        "previous_tickets": ctx["previous_tickets"]["count"], "open_tickets": ctx["previous_tickets"]["open"],
        "subscription": (ctx["subscription"] or {}).get("plan"), "account_status": ctx["account"]["status"]}
