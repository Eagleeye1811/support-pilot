"""Append-only audit log of security-relevant events (``audit`` collection in the tenant's store).

Recorded: every action authorization (allowed / approval requested / denied) and its outcome, approval
decisions, human ticket updates, API authentication failures, API key changes and demo-data resets.
"""
from __future__ import annotations

from .shared_agent import iso


def log(store, event, actor, outcome="ok", **details):
    seq = store.next_seq("audit", 1)
    rec = dict(id=f"AUD-{seq:06d}", ts=iso(), event=event, actor=actor, outcome=outcome,
               details={k: v for k, v in details.items() if v is not None})
    store.put("audit", rec["id"], rec)
    return rec


def recent(store, limit=200, event=None):
    rows = store.list("audit", event=event) if event else store.list("audit")
    return sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]
