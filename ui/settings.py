"""Settings: the connected company system, AI and email status, action policy, API keys, audit log, demo reset."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import agents as A
from agents.email_agent import email_configured
from agents.shared_agent import fmt_ts
from agents.tenancy import get_registry

from .common import rel, workspace


def page():
    store = workspace()
    reg = get_registry()
    st.title("Settings")
    desc = store.describe()
    t1, t2, t3, t4 = st.tabs([":material/cable: Integration", ":material/policy: Action policy", ":material/key: API keys",
                              ":material/receipt_long: Audit log"])
    with t1:
        ad = desc["adapter"]
        st.markdown(f"**Business:** {desc['tenant']['name']} (`{desc['tenant']['id']}`)")
        (st.warning if ad["simulated"] else st.info)(
            f"**Company system: {ad['name']}**{' — simulated' if ad['simulated'] else ''}. {ad['description']}")
        st.caption("SupportPilot reads these entities from it: " + ", ".join(ad["entities"]) + ". It changes them only "
                   "through authorized actions.")
        st.markdown(f"**Support database:** `{rel(store.path)}` · **Registry:** `{rel(reg.store.path)}`")
        st.divider()
        lm = A.llm.status()
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Language model**")
            st.markdown(f"{lm['provider']} · `{lm['model']}` · mode **{lm['mode']}** ({lm['reason']})")
            if lm.get("model_note"):
                st.caption(lm["model_note"])
            st.caption("Switch it off for a session with the toggle in the sidebar.")
        with c2:
            st.markdown("**Email**")
            st.markdown("Gmail SMTP ready" if email_configured() else "Not configured — set `SMTP_USER` and `SMTP_PASSWORD`")
        st.divider()
        with st.popover("Reset demo data", icon=":material/restart_alt:"):
            st.write("Wipes conversations, tickets, runs, approvals and knowledge-base edits in the support database, and "
                     "re-seeds the simulated company data. API keys and analytics are kept.")
            if st.button("Confirm reset", type="primary"):
                store.reset()
                A.audit.log(store, "demo.reset", "dashboard")
                st.session_state.conv_id, st.session_state.turns = None, []
                st.rerun()
    with t2:
        st.caption(f"AI refund limit {A.inr(desc['policy']['auto_refund_limit'])}. Sensitive actions planned from an LLM-only "
                   f"reading need the customer's confirmation: {desc['policy']['confirm_llm_only_actions']}. Change a mode with "
                   "`python manage.py set-mode <tenant> <action> auto|approval|disabled`.")
        st.dataframe(pd.DataFrame(A.action_catalog(store))[["action", "label", "domain", "mode", "supported", "sensitive"]],
                     hide_index=True, width="stretch")
        if store.policy.threshold_overrides:
            st.markdown("**Escalation thresholds set by approved recommendations**")
            st.json(store.policy.threshold_overrides)
    with t3:
        st.caption("Your systems call the SupportPilot API with a key for this business (`uvicorn api.server:app --port 8080`, "
                   "docs at /docs). A key is shown once; only its SHA-256 hash is stored.")
        with st.form("new_key"):
            role = st.radio("Role", ["channel", "admin"], horizontal=True,
                            help="channel: send customer messages · admin: also tickets, approvals, runs, analytics, audit")
            label = st.text_input("Label", placeholder="e.g. website widget")
            if st.form_submit_button("Create API key", type="primary"):
                key, rec = reg.create_key(store.tenant["id"], role, label)
                st.session_state["new_key"] = (rec["id"], key)
        if st.session_state.get("new_key"):
            kid, key = st.session_state.pop("new_key")
            st.success(f"{kid} created — copy it now, it won't be shown again:")
            st.code(key, language=None)
        keys = reg.keys(store.tenant["id"])
        if keys:
            st.dataframe(pd.DataFrame([dict(Key=k["id"], Role=k["role"], Prefix=k["prefix"] + "…", Label=k.get("label"),
                                            Revoked=k["revoked"], LastUsed=fmt_ts(k.get("last_used_at"))) for k in keys]),
                         hide_index=True, width="stretch")
            active = [k["id"] for k in keys if not k["revoked"]]
            if active:
                rk = st.selectbox("Revoke key", active, index=None, placeholder="Select a key to revoke")
                if rk and st.button("Revoke"):
                    reg.revoke_key(rk)
                    st.rerun()
    with t4:
        aud = A.audit.recent(store, 500) + [dict(a, details=dict(a["details"], scope="platform")) for a in A.audit.recent(reg.store, 200)]
        aud.sort(key=lambda a: a["ts"], reverse=True)
        st.caption(f"Support database → `audit` · registry → `audit` · {len(aud)} most recent events")
        if aud:
            st.dataframe(pd.DataFrame([dict(Time=fmt_ts(a["ts"]), Event=a["event"], Actor=a["actor"], Outcome=a["outcome"],
                                            Details=json.dumps(a["details"], ensure_ascii=False)) for a in aud]),
                         hide_index=True, width="stretch", height=480)
