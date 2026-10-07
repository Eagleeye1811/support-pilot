"""Advanced: Root Cause Discovery — turn many similar complaints into one operational incident.

Compares the last-24h ticket volume per intent against a 14-day baseline. When
an intent spikes, it drills down on ticket metadata (gateway, area, courier,
channel) to find a dominant attribute and states a hypothesis, e.g.
"Spike in payment failures — 87% on PayFast → possible gateway outage".
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

from .shared_agent import INTENTS, fmt_ts, now, parse_ts

HYPOTHESES = {
    "payment_failure": ("gateway", "Possible {value} payment gateway outage / degradation",
                        "Switch default routing away from {value}, post a status banner, auto-reconcile debited payments"),
    "double_charge": ("gateway", "Duplicate-capture bug on {value} retries",
                      "Enable idempotency keys on {value} retries and bulk-refund duplicates"),
    "network_issue": ("area", "Possible network outage in {value}", "Confirm with NOC, publish outage ETA to affected customers"),
    "technician_visit": ("area", "Hardware failures clustering in {value}", "Inspect the distribution node serving {value}"),
    "delivery_delay": ("courier", "Courier partner {value} disruption", "Re-allocate pending shipments to alternate couriers"),
    "wrong_item": ("courier", "Mis-sorting at a hub used by {value}", "Audit dispatch scans at the affected hub"),
    "damaged_item": ("courier", "Handling damage with {value}", "Raise quality escalation with {value}"),
    "login_issue": ("channel", "Authentication service degradation ({value})", "Check auth/OTP provider status"),
    "subscription_not_active": ("channel", "Entitlement sync service failing", "Check entitlement service (ENT-503) and replay sync queue"),
}


def detect_incidents(store, ref=None, window_hours=24, baseline_days=14, min_count=5, min_ratio=2.5):
    ref = ref or now()
    win_start = ref - dt.timedelta(hours=window_hours)
    base_start = win_start - dt.timedelta(days=baseline_days)
    recent, base = {}, Counter()
    for t in store.list("tickets"):
        created = parse_ts(t["created_at"])
        if created >= win_start:
            recent.setdefault(t["intent"], []).append(t)
        elif created >= base_start:
            base[t["intent"]] += 1
    incidents = []
    for intent, items in recent.items():
        expected = base[intent] / baseline_days * (window_hours / 24)
        ratio = len(items) / max(expected, 0.5)
        if len(items) < min_count or ratio < min_ratio:
            continue
        key, hypo, action = HYPOTHESES.get(intent, ("channel", "Unusual spike in {value}", "Investigate common cause"))
        values = Counter((t.get("meta") or {}).get(key) or (t.get("channel") if key == "channel" else None) for t in items)
        values.pop(None, None)
        dominant, share = (None, 0.0)
        if values:
            dominant, cnt = values.most_common(1)[0]
            share = cnt / sum(values.values())
        hypothesis = hypo.format(value=dominant) if dominant and share >= 0.6 else f"Unusual spike in {INTENTS[intent]['label'].lower()}"
        incidents.append(dict(
            id=f"INC-{intent.upper()[:12]}-{ref.strftime('%m%d')}", intent=intent, label=INTENTS[intent]["label"],
            recent_count=len(items), baseline_per_window=round(expected, 2), spike_ratio=round(ratio, 1),
            dominant_attribute=key if dominant else None, dominant_value=dominant, dominant_share=round(share, 2),
            hypothesis=hypothesis, recommended_action=action.format(value=dominant or "the affected component"),
            severity="critical" if ratio >= 5 else "high", affected_customers=len({t["customer_id"] for t in items}),
            first_seen=min(t["created_at"] for t in items), source="ticket spike"))
    for o in store.list("outages", status="active"):
        incidents.append(dict(id=o["id"], intent="network_issue", label="Network outage", recent_count=None,
                              baseline_per_window=None, spike_ratio=None, dominant_attribute="area", dominant_value=o["area"],
                              dominant_share=1.0, hypothesis=f"Confirmed {o['service']} outage in {o['area']}: {o['cause']}",
                              recommended_action=f"Proactively notify {o['affected_lines']} affected lines; restoration ETA {fmt_ts(o['eta_restore'])}",
                              severity="high", affected_customers=o["affected_lines"], first_seen=o["started_at"],
                              source="network operations"))
    return sorted(incidents, key=lambda i: (i["severity"] != "critical", -(i["spike_ratio"] or 0)))


def incident_for(store, intent, attribute_value, ref=None):
    for inc in detect_incidents(store, ref):
        if inc["intent"] == intent and inc["dominant_value"] == attribute_value:
            return inc
    return None


def daily_volume(store, days=21, ref=None):
    ref = ref or now()
    start = ref - dt.timedelta(days=days)
    rows = Counter()
    for t in store.list("tickets"):
        created = parse_ts(t["created_at"])
        if created >= start:
            rows[(created.date().isoformat(), INTENTS.get(t["intent"], {}).get("category", "General"))] += 1
    return [dict(date=d, category=c, tickets=n) for (d, c), n in sorted(rows.items())]
