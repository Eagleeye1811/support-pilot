"""Synthetic, reproducible support world for SupportPilot.

Creates customers, orders, payments, refunds, auth accounts, subscriptions,
telecom service lines, outages, inventory, 21 days of historical tickets and
CSAT responses. Timestamps are anchored to the moment the store is seeded so
SLA timers, "delivered yesterday" and root-cause windows always look live.

Twelve demo personas (CUST1001-CUST1012) are wired to the PS-04 scenarios:
wrong item, payment deducted, locked login, subscription sync failure, area
outage, hardware fault, fraud, delayed delivery, double charge, refund status
and a high-value damage claim. A payment-gateway failure spike is planted in
the last 24 hours for the Root Cause Discovery agent to find.

Run `python data/generate_data.py` to write a JSON snapshot for inspection.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents.shared_agent import INTENTS, SLA_RESOLUTION, SLA_RESPONSE, TEAMS  # noqa: E402

CATALOG = [
    ("EL-200", "Noise-cancelling Headphones X200 (Black)", "Electronics", 7999),
    ("EL-110", "Wireless Earbuds E10 (Blue)", "Electronics", 2499),
    ("EL-310", "Smartwatch Pulse 3", "Electronics", 4999),
    ("EL-900", "NovaBook Pro 14 Laptop", "Electronics", 54999),
    ("EL-420", "Bluetooth Speaker Boom 2", "Electronics", 3299),
    ("FA-101", "Men's Running Shoes (UK 9)", "Fashion", 3499),
    ("FA-205", "Women's Denim Jacket (M)", "Fashion", 2799),
    ("FA-330", "Cotton Kurta Set (L)", "Fashion", 1899),
    ("HM-010", "Non-stick Cookware Set", "Home", 3999),
    ("HM-044", "Air Fryer 4L", "Home", 6499),
    ("HM-120", "Memory Foam Pillow (2-pack)", "Home", 1499),
    ("BK-007", "Atomic Habits (Paperback)", "Books", 499),
    ("BE-055", "Vitamin C Face Serum", "Beauty", 699),
    ("GR-001", "Organic Dry Fruits Box 1kg", "Grocery", 1299),
]
SKU = {s[0]: dict(sku=s[0], name=s[1], category=s[2], price=s[3]) for s in CATALOG}
COURIERS = ["SwiftShip", "Delhivery Express", "BlueParcel"]
GATEWAYS = ["PayFast", "RazorPe", "CardNet"]
AREAS = ["Andheri West", "Koramangala", "Powai", "Indiranagar", "Salt Lake", "Banjara Hills", "Viman Nagar"]
PLANS = {"Basic": 0, "Plus": 199, "Premium": 499}
FIRST = ["Aditya", "Ishaan", "Kabir", "Riya", "Tanvi", "Nikhil", "Sanya", "Yash", "Aisha", "Varun", "Diya", "Manav",
         "Pallavi", "Siddharth", "Nisha", "Om", "Rhea", "Tarun", "Zara", "Gaurav", "Simran", "Harsh", "Kiara", "Rohit",
         "Anjali", "Parth", "Mira", "Vivek", "Leela", "Samar"]
LAST = ["Bose", "Chopra", "Das", "Fernandes", "Ghosh", "Jain", "Kulkarni", "Mishra", "Naidu", "Pandey", "Rao", "Saxena",
        "Thakur", "Venkat", "Yadav", "Banerjee", "Kapoor", "Sethi", "Pillai", "Chauhan"]


def make(now=None, seed=42):
    rng = random.Random(seed)
    now = now or dt.datetime.now(dt.timezone.utc)
    now = now.replace(microsecond=0)

    def ago(days=0, hours=0, minutes=0):
        return (now - dt.timedelta(days=days, hours=hours, minutes=minutes)).isoformat()

    def ahead(days=0, hours=0, minutes=0):
        return (now + dt.timedelta(days=days, hours=hours, minutes=minutes)).isoformat()

    customers, orders, payments, refunds, accounts, subs, lines = [], [], [], [], [], [], []
    seq = {"order": 12400, "txn": 50100, "refund": 7100}

    def item(sku, qty=1):
        p = SKU[sku]
        return dict(sku=sku, name=p["name"], category=p["category"], qty=qty, price=p["price"])

    def add_customer(cid, name, tier, city, since_days, phone, email=None):
        email = email or f"{name.lower().replace(' ', '.')}@example.com"
        customers.append(dict(id=cid, name=name, email=email, phone=phone, tier=tier, city=city,
                              customer_since=ago(days=since_days), preferred_channel="web"))
        accounts.append(dict(id=cid, customer_id=cid, status="active", failed_attempts=0, locked_until=None,
                             two_factor=True, last_login=ago(hours=rng.randint(2, 200)), sessions=rng.randint(1, 3)))
        if tier in ("Plus", "Premium"):
            subs.append(dict(id=f"SUB{cid[4:]}", customer_id=cid, plan=tier, price=PLANS[tier], status="active",
                             auto_renew=True, started_at=ago(days=min(since_days, 200)), renews_at=ahead(days=rng.randint(3, 28)),
                             payment_status="captured", sync_status="ok", sync_fixable=True))
        return cid

    def add_order(cid, skus, status, placed_days, order_id=None, delivered_items=None, gateway=None, pay_status="captured",
                  bank_debited=True, courier=None, eta_days=None, method="UPI", delivered_days=None):
        if order_id is None:
            seq["order"] += 1
            order_id = f"ORD{seq['order']}"
        items = [item(s) for s in skus]
        amount = sum(i["price"] * i["qty"] for i in items)
        placed = now - dt.timedelta(days=placed_days)
        o = dict(id=order_id, customer_id=cid, items=items, delivered_items=delivered_items or items, amount=amount,
                 status=status, placed_at=placed.isoformat(), courier=courier or rng.choice(COURIERS),
                 awb=f"AWB{rng.randint(10**8, 10**9 - 1)}", payment_method=method, shipping_address_city=None)
        if status in ("shipped", "out_for_delivery", "delivered", "returned"):
            o["shipped_at"] = (placed + dt.timedelta(days=1)).isoformat()
        eta = eta_days if eta_days is not None else -(placed_days - 4)
        o["eta"] = ahead(days=eta) if eta >= 0 else ago(days=-eta)
        if status in ("delivered", "returned"):
            dd = delivered_days if delivered_days is not None else max(placed_days - 4, 0)
            o["delivered_at"] = ago(days=dd, hours=rng.randint(1, 8))
        seq["txn"] += 1
        pay = dict(id=f"TXN{seq['txn']}", order_id=order_id, customer_id=cid, amount=amount, status=pay_status,
                   bank_debited=bank_debited, gateway=gateway or rng.choice(GATEWAYS), method=method,
                   created_at=(placed + dt.timedelta(minutes=2)).isoformat(), late_capture=False, purpose="order")
        o["payment_id"] = pay["id"]
        orders.append(o)
        payments.append(pay)
        return o, pay

    # ---------------- demo personas ----------------
    add_customer("CUST1001", "Aarav Sharma", "Premium", "Mumbai", 640, "9876500001")
    add_order("CUST1001", ["EL-200"], "delivered", 4, order_id="ORD12345", delivered_items=[item("EL-110")],
              courier="SwiftShip", delivered_days=1)
    add_order("CUST1001", ["FA-101"], "delivered", 45)
    add_order("CUST1001", ["BK-007", "BE-055"], "delivered", 90)

    add_customer("CUST1002", "Priya Nair", "Plus", "Bengaluru", 410, "9876500002")
    o, p = add_order("CUST1002", ["EL-110"], "payment_pending", 0, order_id="ORD12346", pay_status="failed",
                     bank_debited=True, gateway="PayFast", eta_days=4)
    o["placed_at"] = p["created_at"] = ago(hours=2)
    p["failure_reason"] = "Gateway timeout after bank debit (PG-504)"
    add_order("CUST1002", ["HM-120"], "delivered", 20)

    add_customer("CUST1003", "Rohan Mehta", "Standard", "Pune", 220, "9876500003")
    accounts[-1].update(status="locked", failed_attempts=5, locked_until=ahead(minutes=30), last_login=ago(days=6))
    add_order("CUST1003", ["FA-330"], "delivered", 10)

    add_customer("CUST1004", "John Doe", "Premium", "Delhi", 380, "9876500004", email="john.doe@example.com")
    subs[-1].update(status="pending_activation", started_at=ago(days=1), sync_status="failed", sync_fixable=False,
                    upgraded_from="Plus", renews_at=ahead(days=29))
    seq["txn"] += 1
    payments.append(dict(id=f"TXN{seq['txn']}", order_id=None, subscription_id="SUB1004", customer_id="CUST1004", amount=499,
                         status="captured", bank_debited=True, gateway="CardNet", method="Credit Card", created_at=ago(days=1),
                         late_capture=False, purpose="subscription"))
    add_order("CUST1004", ["GR-001"], "delivered", 1, delivered_days=0)

    add_customer("CUST1005", "Sneha Iyer", "Premium", "Chennai", 300, "9876500005")
    subs[-1].update(status="pending_activation", started_at=ago(hours=5), sync_status="failed", sync_fixable=True,
                    upgraded_from="Plus")
    seq["txn"] += 1
    payments.append(dict(id=f"TXN{seq['txn']}", order_id=None, subscription_id="SUB1005", customer_id="CUST1005", amount=499,
                         status="captured", bank_debited=True, gateway="RazorPe", method="UPI", created_at=ago(hours=5),
                         late_capture=False, purpose="subscription"))
    add_order("CUST1005", ["HM-010"], "delivered", 30)

    add_customer("CUST1006", "Vikram Singh", "Standard", "Mumbai", 500, "9876500006")
    lines.append(dict(id="SRV3006", customer_id="CUST1006", plan="NovaFiber 300 Mbps", area="Andheri West", status="active",
                      signal_dbm=-61, line_fault="none", router="NovaRouter AX3000", installed_at=ago(days=400)))

    add_customer("CUST1007", "Ananya Gupta", "Plus", "Bengaluru", 260, "9876500007")
    lines.append(dict(id="SRV3007", customer_id="CUST1007", plan="NovaFiber 500 Mbps", area="Koramangala", status="active",
                      signal_dbm=-79, line_fault="hardware", router="NovaRouter AX1800", installed_at=ago(days=250)))

    add_customer("CUST1008", "Karan Malhotra", "Standard", "Hyderabad", 150, "9876500008")
    seq["txn"] += 1
    payments.append(dict(id=f"TXN{seq['txn']}", order_id=None, customer_id="CUST1008", amount=18499, status="captured",
                         bank_debited=True, gateway="CardNet", method="Saved Card ••4417", created_at=ago(hours=2),
                         late_capture=False, purpose="wallet_topup", merchant_note="Gift card purchase — new device, Kolkata IP"))
    add_order("CUST1008", ["EL-420"], "delivered", 25)

    add_customer("CUST1009", "Meera Reddy", "Premium", "Hyderabad", 700, "9876500009")
    add_order("CUST1009", ["HM-044"], "shipped", 6, order_id="ORD12349", courier="Delhivery Express", eta_days=-2)
    add_order("CUST1009", ["FA-205"], "packed", 0, order_id="ORD12352", eta_days=3)

    add_customer("CUST1010", "Arjun Verma", "Standard", "Kolkata", 120, "9876500010")
    o, p = add_order("CUST1010", ["EL-420"], "delivered", 5, order_id="ORD12350", gateway="PayFast")
    seq["txn"] += 1
    dup = dict(p, id=f"TXN{seq['txn']}", created_at=(dt.datetime.fromisoformat(p["created_at"]) + dt.timedelta(seconds=40)).isoformat())
    payments.append(dup)

    add_customer("CUST1011", "Fatima Khan", "Plus", "Lucknow", 330, "9876500011")
    o, p = add_order("CUST1011", ["FA-101"], "returned", 12, order_id="ORD12351")
    seq["refund"] += 1
    refunds.append(dict(id=f"RF{seq['refund']}", order_id="ORD12351", customer_id="CUST1011", amount=o["amount"],
                        status="processing", method="UPI", initiated_at=ago(days=2), eta=ahead(days=2), reason="Return received"))
    p["status"] = "refund_initiated"

    add_customer("CUST1012", "Dev Patel", "Premium", "Ahmedabad", 900, "9876500012")
    add_order("CUST1012", ["EL-900"], "delivered", 5, order_id="ORD12353", delivered_days=2)

    # ---------------- background customers ----------------
    for i in range(30):
        cid = f"CUST{1013 + i}"
        tier = rng.choices(["Standard", "Plus", "Premium"], [0.55, 0.28, 0.17])[0]
        name = f"{FIRST[i]} {rng.choice(LAST)}"
        add_customer(cid, name, tier, rng.choice(["Mumbai", "Delhi", "Bengaluru", "Pune", "Chennai", "Hyderabad", "Kolkata"]),
                     rng.randint(30, 900), f"98{rng.randint(10**7, 10**8 - 1)}")
        for _ in range(rng.randint(1, 4)):
            status = rng.choices(["delivered", "shipped", "packed", "returned"], [0.75, 0.12, 0.08, 0.05])[0]
            add_order(cid, rng.sample(list(SKU), rng.randint(1, 2)), status, rng.randint(1, 120))
        if rng.random() < 0.35:
            lines.append(dict(id=f"SRV{3013 + i}", customer_id=cid, plan=rng.choice(["NovaFiber 100 Mbps", "NovaFiber 300 Mbps"]),
                              area=rng.choice(AREAS), status="active", signal_dbm=rng.randint(-70, -55), line_fault="none",
                              router="NovaRouter AX1800", installed_at=ago(days=rng.randint(60, 700))))

    outages = [dict(id="OUT-ANW-01", area="Andheri West", service="NovaFiber", status="active", started_at=ago(hours=3),
                    eta_restore=ahead(hours=4), cause="Fibre cut on trunk line near SV Road (civic works)", affected_lines=412)]
    inventory = [dict(id=s, sku=s, name=SKU[s]["name"], stock=(0 if s == "EL-310" else rng.randint(8, 120))) for s in SKU]

    # ---------------- historical tickets (21 days) ----------------
    tickets, csat = [], []
    persona_history = {"CUST1004": ["subscription_not_active", "login_issue", "delivery_delay"],
                       "CUST1008": ["delivery_delay", "refund_request", "payment_failure"],
                       "CUST1001": ["return_request"]}
    weights = {"delivery_delay": 14, "refund_request": 9, "refund_status": 8, "payment_failure": 5, "login_issue": 9,
               "password_reset": 8, "wrong_item": 5, "damaged_item": 5, "cancel_order": 7, "return_request": 7,
               "subscription_not_active": 3, "cancel_subscription": 3, "upgrade_plan": 2, "network_issue": 6,
               "technician_visit": 2, "double_charge": 2, "fraud_report": 1, "legal_request": 1, "general_inquiry": 6}
    auto_ok = {"delivery_delay", "refund_status", "password_reset", "login_issue", "cancel_order", "return_request",
               "wrong_item", "refund_request", "upgrade_plan", "cancel_subscription", "general_inquiry", "payment_failure",
               "double_charge", "network_issue"}
    customer_ids = [c["id"] for c in customers]
    tier_of = {c["id"]: c["tier"] for c in customers}
    name_of = {c["id"]: c["name"] for c in customers}
    tid = 1000

    def mk_ticket(cid, intent, created, *, force_open=False, meta=None):
        nonlocal tid
        tid += 1
        spec = INTENTS[intent]
        prio = spec["priority"]
        tier = tier_of[cid]
        created_dt = created
        escalated = intent not in auto_ok or rng.random() < 0.22 or force_open
        team = spec["team"] if escalated else "AI Agent"
        assignee = rng.choice(TEAMS[spec["team"]]) if escalated else "SupportPilot AI"
        resp_min, res_min = SLA_RESPONSE[tier][prio], SLA_RESOLUTION[tier][prio]
        t = dict(id=f"TKT-{tid}", customer={"id": cid, "name": name_of[cid], "tier": tier}, customer_id=cid,
                 channel=rng.choices(["web", "whatsapp", "email", "app", "telegram"], [0.38, 0.27, 0.15, 0.14, 0.06])[0],
                 intent=intent, category=spec["category"], title=spec["label"], priority=prio,
                 sentiment=rng.choice(["negative", "negative", "neutral"]), escalated=escalated, team=team, assignee=assignee,
                 created_at=created_dt.isoformat(), updated_at=created_dt.isoformat(), meta=meta or {}, historical=True,
                 sla=dict(response_minutes=resp_min, resolution_minutes=res_min,
                          response_due=(created_dt + dt.timedelta(minutes=resp_min)).isoformat(),
                          resolution_due=(created_dt + dt.timedelta(minutes=res_min)).isoformat()),
                 investigation=[], actions_taken=[], conversation_summary=f"Historical {spec['label'].lower()} case.",
                 events=[dict(ts=created_dt.isoformat(), status="Opened", actor="SupportPilot AI", note="Ticket created")])
        if not escalated:
            done = created_dt + dt.timedelta(minutes=rng.uniform(1, 9))
            t.update(status="Closed", resolved_by="SupportPilot AI", first_response_at=created_dt.isoformat(),
                     resolved_at=done.isoformat(), closed_at=(done + dt.timedelta(hours=rng.uniform(1, 30))).isoformat())
        else:
            age_h = (now - created_dt).total_seconds() / 3600
            if force_open or (age_h < 30 and rng.random() < 0.7):
                status = rng.choice(["Assigned", "Assigned", "Pending Customer", "Opened"]) if not force_open else "Assigned"
                t.update(status=status, resolved_by=None)
                if status == "Pending Customer":
                    t["first_response_at"] = (created_dt + dt.timedelta(minutes=resp_min * rng.uniform(0.3, 0.9))).isoformat()
                t["events"].append(dict(ts=created_dt.isoformat(), status="Assigned", actor="Router", note=f"Assigned to {assignee}"))
            else:
                hours = rng.uniform(0.6, 1.4) * res_min / 60
                done = created_dt + dt.timedelta(hours=hours)
                if done > now:
                    done = now - dt.timedelta(minutes=5)
                t.update(status="Closed" if (now - done).total_seconds() > 86400 else "Resolved", resolved_by=assignee,
                         first_response_at=(created_dt + dt.timedelta(minutes=resp_min * rng.uniform(0.2, 1.3))).isoformat(),
                         resolved_at=done.isoformat())
                if t["status"] == "Closed":
                    t["closed_at"] = (done + dt.timedelta(hours=24)).isoformat()
        if t.get("resolved_at") and rng.random() < 0.62:
            base = 4.6 if not escalated else 3.9
            if intent == "refund_request" and not escalated:
                base = 2.9  # weak auto-resolution quality → the feedback loop raises its escalation threshold
            rating = max(1, min(5, round(rng.gauss(base, 0.8))))
            t["csat"] = rating
            csat.append(dict(id=f"CSAT-{tid}", ticket_id=t["id"], customer_id=cid, rating=rating, intent=intent,
                             channel=t["channel"], resolved_by="AI" if not escalated else "Human",
                             sentiment="positive" if rating >= 4 else ("neutral" if rating == 3 else "negative"),
                             resolution_quality=round(rating * 17 + (15 if not escalated else 5), 1),
                             comment="", ts=t["resolved_at"]))
        tickets.append(t)
        return t

    for day in range(21, 0, -1):
        for _ in range(rng.randint(8, 13)):
            intent = rng.choices(list(weights), list(weights.values()))[0]
            created = now - dt.timedelta(days=day, hours=rng.uniform(0, 23), minutes=rng.uniform(0, 59))
            meta = {}
            if intent in ("payment_failure", "double_charge"):
                meta = {"gateway": rng.choice(GATEWAYS)}
            elif intent in ("network_issue", "technician_visit"):
                meta = {"area": rng.choice(AREAS)}
            elif intent in ("delivery_delay", "wrong_item", "damaged_item"):
                meta = {"courier": rng.choice(COURIERS)}
            mk_ticket(rng.choice(customer_ids[12:]), intent, created, meta=meta)
    for cid, intents in persona_history.items():
        for k, intent in enumerate(intents):
            mk_ticket(cid, intent, now - dt.timedelta(days=5 + 4 * k, hours=rng.uniform(0, 10)))
    # Root-cause plant: payment failures spike on PayFast in the last 20 hours.
    for _ in range(15):
        created = now - dt.timedelta(hours=rng.uniform(0.3, 20))
        mk_ticket(rng.choice(customer_ids[12:]), "payment_failure", created,
                  meta={"gateway": "PayFast" if rng.random() < 0.87 else rng.choice(GATEWAYS[1:])})
    # A few live escalations so the SLA monitor has something at risk / breached.
    for cid, intent, mins in [("CUST1015", "damaged_item", 150), ("CUST1020", "refund_request", 260),
                              ("CUST1027", "network_issue", 100), ("CUST1033", "subscription_not_active", 12),
                              ("CUST1018", "fraud_report", 50), ("CUST1024", "wrong_item", 35)]:
        mk_ticket(cid, intent, now - dt.timedelta(minutes=mins), force_open=True)

    return dict(
        anchor=now.isoformat(),
        collections=dict(customers=customers, orders=orders, payments=payments, refunds=refunds, accounts=accounts,
                         subscriptions=subs, service_lines=lines, outages=outages, inventory=inventory, tickets=tickets,
                         csat=csat),
        counters=dict(ticket=tid, order=seq["order"], refund=seq["refund"]),
    )


if __name__ == "__main__":
    world = make()
    out = os.path.join(ROOT, "data", "seed_snapshot.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(world, f, indent=2, ensure_ascii=False)
    print({k: len(v) for k, v in world["collections"].items()}, "->", out)
