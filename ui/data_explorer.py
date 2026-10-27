"""Data explorer: everything SupportPilot stores, where it is stored, and the records themselves."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agents.shared_agent import fmt_ts
from agents.tenancy import get_registry
from analytics.warehouse import Warehouse, analytics_path
from integrations import BUSINESS_ENTITIES

from .common import rel, workspace

STORES = {
    "support": ("🗄️ SupportPilot database", "Conversations, tickets, workflow runs, actions, approvals, audit, knowledge base…"),
    "company": ("🏢 Company system", "Customers, orders, payments, subscriptions… read through the adapter (read-only)"),
    "warehouse": ("📊 Analytics warehouse", "Cleaned, derived tables written by each analytics run"),
    "platform": ("🔐 Platform registry", "Tenants, API keys (hashes hidden) and platform audit"),
}
DESCRIPTIONS = {
    "conversations": "Every chat, with all messages and the active issue", "tickets": "Support tickets and their lifecycle",
    "workflow_runs": "One record per customer message: steps, tool calls, LLM calls, outcome",
    "actions": "Every action attempt with its authorization", "approvals": "Actions held for a human decision",
    "audit": "Security-relevant events", "notifications": "Messages sent to customers", "csat": "Customer ratings",
    "kb_articles": "Help-centre articles (database copy)", "kb_history": "Previous article versions", "kb_auto": "Articles learned from tickets",
    "recommendations": "Analytics recommendations and their decisions", "tasks": "Tasks created from approved recommendations",
    "analytics_runs": "Analytics pipeline runs", "telegram_chats": "Telegram chats", "telegram_turns": "Telegram messages",
    "emails": "Email attempts", "attachments": "Customer photos", "meta": "Internal markers",
}


def _flatten(records, limit=2000):
    df = pd.json_normalize(records[:limit], max_level=1)
    for c in df.columns:
        if df[c].map(lambda v: isinstance(v, (list, dict))).any():
            df[c] = df[c].map(lambda v: json.dumps(v, ensure_ascii=False, default=str)[:300] if isinstance(v, (list, dict)) else v)
    return df


def _show(df, key, records=None):
    q = st.text_input("Filter rows (text match in any column)", key=f"q_{key}")
    view = df
    if q.strip():
        mask = df.astype(str).apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)
        view = df[mask]
    st.caption(f"{len(view):,} of {len(df):,} rows" + (" (first 2,000 loaded)" if records and len(records) > 2000 else ""))
    ev = st.dataframe(view, hide_index=True, width="stretch", height=420, on_select="rerun", selection_mode="single-row",
                      key=f"t_{key}")
    st.download_button("Download CSV", view.to_csv(index=False).encode(), f"{key}.csv", "text/csv", icon=":material/download:")
    sel = ev.selection.rows if ev and ev.selection else []
    if sel and records is not None:
        rid = view.iloc[sel[0]].get("id")
        rec = next((r for r in records if str(r.get("id")) == str(rid)), None)
        if rec:
            st.markdown(f"**Record `{rid}`**")
            st.json(rec, expanded=True)


def page():
    store = workspace()
    reg = get_registry()
    st.title("Data explorer")
    kind = st.segmented_control("Store", list(STORES), format_func=lambda k: STORES[k][0], default="support", key="dx_store") or "support"
    st.caption(STORES[kind][1])

    if kind == "support":
        cols = [(c, n, u) for c, n, u in store.collections() if c not in BUSINESS_ENTITIES]
        st.markdown(f"**File:** `{rel(store.path)}` (SQLite) · table `docs(collection, id, data JSON, updated)` · tenant "
                    f"`{store.tenant['id']}`")
        summary = pd.DataFrame([dict(Collection=c, Records=n, Updated=fmt_ts(u), Holds=DESCRIPTIONS.get(c, "")) for c, n, u in cols])
        st.dataframe(summary, hide_index=True, width="stretch", height=min(38 * (len(cols) + 1), 380))
        name = st.selectbox("Open collection", [c for c, _, _ in cols], key="dx_col",
                            index=next((i for i, (c, _, _) in enumerate(cols) if c == "tickets"), 0))
        records = sorted(store.list(name), key=lambda r: str(r.get("id")), reverse=True)
        if records:
            _show(_flatten(records), f"support_{name}", records)
        with st.expander("Query this file outside the app"):
            st.code(f"sqlite3 {rel(store.path)} \"SELECT id, json_extract(data, '$.status') FROM docs WHERE collection = '{name}' "
                    "LIMIT 20\"", language="bash")
            st.caption("Or open the file in DB Browser for SQLite and filter the docs table by collection.")

    elif kind == "company":
        sim = store.adapter.simulated
        st.markdown(f"**Adapter:** {store.adapter.name}" + (" — **simulated**: synthetic ShopNova data kept in "
                    f"`{rel(store.path)}` as separate collections" if sim else f" — live API ({store.adapter.description})"))
        ents = [e for e in store.adapter.entities()]
        counts = {c: n for c, n, _ in store.collections()} if sim else {}
        name = st.selectbox("Entity", ents, key="dx_ent", format_func=lambda e: f"{e} ({counts.get(e, '?')})" if sim else e)
        records = store.adapter.find(name)
        if records:
            _show(_flatten(records), f"company_{name}", records)
        else:
            st.caption("No records.")

    elif kind == "warehouse":
        path = analytics_path(store.tenant["id"])
        wh = Warehouse(path)
        tables = [t for t in wh.tables() if t != "kv"]
        st.markdown(f"**File:** `{rel(path)}` (SQLite, normal tables) · rebuilt by **Customer intelligence → Run analytics pipeline**")
        if not tables:
            st.info("Empty — run the analytics pipeline first.")
            return
        name = st.selectbox("Table", tables, key="dx_tab")
        df = wh.read(name)
        _show(df.head(5000), f"warehouse_{name}")
        st.code(f"sqlite3 {rel(path)} \"SELECT * FROM {name} LIMIT 20\"", language="bash")

    else:
        st.markdown(f"**File:** `{rel(reg.store.path)}`")
        name = st.selectbox("Collection", ["tenants", "api_keys", "audit"], key="dx_plat")
        records = reg.keys() if name == "api_keys" else sorted(reg.store.list(name), key=lambda r: str(r.get("id")), reverse=True)
        if records:
            _show(_flatten(records), f"platform_{name}", records)
        else:
            st.caption("No records.")
