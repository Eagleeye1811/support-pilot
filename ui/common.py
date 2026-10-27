"""Shared pieces for the dashboard pages: the tenant workspace, palette, and "where this data lives" labels."""
from __future__ import annotations

import os

import streamlit as st

import agents as A
from agents.shared_agent import ROOT
from agents.tenancy import DEMO_TENANT, get_registry

# Validated categorical palette (fixed order, one hue per ticket category) — see the dataviz palette.
CATEGORY_ORDER = ["Billing", "Orders", "Account", "Subscription", "Connectivity", "Security", "Legal", "General"]
CATEGORY_COLORS = dict(zip(CATEGORY_ORDER, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]))
SERIES_1 = "#2a78d6"
STEP_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "info": "ℹ️"}
PRIORITY_COLOR = {"critical": "red", "high": "orange", "medium": "blue", "low": "gray"}
SLA_ICON = {"ok": "🟢 on track", "at_risk": "🟠 at risk", "breached": "🔴 breached", "met": "✅ met", "missed": "❌ missed", "n/a": "—"}
ACTION_ICON = {"success": "✅", "skipped": "↩️", "blocked": "⛔", "failed": "❌", "pending_approval": "⏳"}


@st.cache_resource
def _workspace(code_version):
    return get_registry().workspace(DEMO_TENANT)


def workspace():
    """The demo tenant's workspace (support database + company adapter + policy), one per server process.
    Keyed on the classes so a hot reload of agents/ builds a fresh one over the same database file."""
    return _workspace((id(A.Store), id(A.Workspace)))


def rel(path):
    try:
        return os.path.relpath(path, ROOT) if path not in (None, ":memory:") else str(path)
    except ValueError:
        return str(path)


def where(kind, detail="", store=None):
    """One-line provenance label shown next to data: which store, which file, which collection/table."""
    store = store or workspace()
    if kind == "support":
        st.caption(f"🗄️ **SupportPilot database** · `{rel(store.path)}`" + (f" → collection `{detail}`" if detail else ""))
    elif kind == "company":
        sim = " (simulated, synthetic data)" if store.adapter.simulated else ""
        st.caption(f"🏢 **Company system: {store.adapter.name}{sim}** · read through the adapter, read-only"
                   + (f" → `{detail}`" if detail else ""))
    elif kind == "warehouse":
        st.caption(f"📊 **Analytics warehouse** · `{rel(detail)}` (rebuilt by each analytics run)")
    elif kind == "files":
        st.caption(f"📄 **Help-centre files** · `{detail or 'docs/help-center/'}`")
    elif kind == "platform":
        st.caption(f"🔐 **Platform registry** · `{rel(get_registry().store.path)}`" + (f" → `{detail}`" if detail else ""))


def step_lines(steps):
    for s in steps:
        st.markdown(f"{STEP_ICON.get(s['status'], '•')} **{s['check']}** — {s['detail']}")


def theme():
    return getattr(st.context.theme, "type", None) or "light"


def customers(store):
    """Customer picker options, read from the company system (not a hard-coded list)."""
    rows = sorted(store.list("customers"), key=lambda c: c["id"])
    return [None] + [c["id"] for c in rows], {c["id"]: f"{c['name']} · {c['tier']} · {c['id']}" for c in rows}
