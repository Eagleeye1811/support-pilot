"""Live operations: today's desk at a glance — workload, SLA risk, incidents and the CSAT feedback loop."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

import agents as A
from agents.shared_agent import humanize_minutes

from .common import CATEGORY_COLORS, CATEGORY_ORDER, SERIES_1, SLA_ICON, where, workspace


def page():
    store = workspace()
    st.title("Live operations")
    where("support", "tickets, csat, notifications", store)
    m = A.supervisor_metrics(store)
    cs = A.csat_summary(store)
    with st.container(horizontal=True):
        st.metric("Open tickets", m["open"], border=True)
        st.metric("Resolution rate", f"{m['resolution_rate']:.0%}", border=True)
        st.metric("Resolved by AI", f"{m['automation_rate']:.0%}", border=True, help="Share of all tickets resolved without a human")
        st.metric("Escalation rate", f"{m['escalation_rate']:.0%}", border=True)
        st.metric("Avg resolution", humanize_minutes(m["avg_resolution_min"] or 0), border=True,
                  help=f"AI {humanize_minutes(m['avg_resolution_ai_min'] or 0)} · Human {humanize_minutes(m['avg_resolution_human_min'] or 0)}")
        st.metric("CSAT", f"{cs['avg']:.2f}/5" if cs["avg"] else "—", f"{cs['csat_pct']:.0%} satisfied" if cs["csat_pct"] else None,
                  delta_color="off", border=True)
        st.metric("SLA at risk / breached", f"{m['sla_at_risk']} / {m['sla_breached']}", border=True)
    st.caption("Includes the 21 days of seeded demo tickets plus every live ticket.")

    incidents = A.detect_incidents(store)
    if incidents:
        st.subheader("Incidents detected now")
        for inc in incidents:
            with st.container(border=True):
                sev = "🔴 Critical" if inc["severity"] == "critical" else "🟠 High"
                stats = (f"{inc['recent_count']} tickets in 24h · {inc['spike_ratio']}× baseline · {inc['dominant_share']:.0%} on "
                         f"{inc['dominant_value']}") if inc["spike_ratio"] else f"{inc['affected_customers']} lines affected"
                st.markdown(f"**{sev} · {inc['hypothesis']}**  \n{stats}  \n➡️ {inc['recommended_action']}")
                st.caption(f"Source: {inc['source']}")

    c1, c2 = st.columns([1.6, 1], gap="large")
    with c1:
        vol = pd.DataFrame(A.daily_volume(store))
        if not vol.empty:
            fig = px.bar(vol, x="date", y="tickets", color="category", category_orders={"category": CATEGORY_ORDER},
                         color_discrete_map=CATEGORY_COLORS, title="Tickets per day by category")
            fig.update_layout(bargap=0.25, legend_title_text="", margin=dict(t=50, l=10, r=10, b=10), xaxis_title=None,
                              yaxis_title=None, hovermode="x unified")
            st.plotly_chart(fig, width="stretch")
    with c2:
        st.subheader("SLA monitor")
        flagged = A.sla_scan(store, auto_escalate=False)
        if flagged:
            st.dataframe(pd.DataFrame([dict(Ticket=f["ticket_id"], Tier=f["tier"], Priority=f["priority"], State=SLA_ICON[f["state"]],
                                            Left=humanize_minutes(f["minutes_left"]) + (" over" if f["minutes_left"] < 0 else ""),
                                            Owner=f["assignee"]) for f in flagged]), hide_index=True, width="stretch")
        else:
            st.success("No open ticket is at SLA risk.", icon=":material/verified:")
        if st.button("Escalate at-risk tickets now", icon=":material/notification_important:"):
            done = [f for f in A.sla_scan(store) if f["escalated_now"]]
            st.toast(f"Escalated {len(done)} ticket(s) to the Escalation Desk" if done else "Nothing new to escalate")

    c3, c4 = st.columns(2, gap="large")
    with c3:
        bi = pd.DataFrame([dict(issue=A.INTENTS[k]["label"], avg=v["avg"], n=v["n"]) for k, v in cs["by_intent"].items() if v["n"] >= 3])
        if not bi.empty:
            fig = px.bar(bi.sort_values("avg"), x="avg", y="issue", orientation="h", title="CSAT by issue type (n ≥ 3)",
                         hover_data={"n": True}, color_discrete_sequence=[SERIES_1])
            fig.update_layout(margin=dict(t=50, l=10, r=10, b=10), xaxis=dict(range=[0, 5], title=None), yaxis_title=None, bargap=0.3)
            st.plotly_chart(fig, width="stretch")
    with c4:
        st.subheader("Escalation thresholds in use")
        lt = A.learned_thresholds(store)
        lt.update(store.policy.threshold_overrides)
        rows = [dict(Issue=A.INTENTS[k]["label"], Threshold=f"{v['threshold']:.0%}", Why=v["reason"]) for k, v in lt.items()
                if v["reason"] != "default"]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("Learned from CSAT on AI-resolved tickets, or set by an approved recommendation. Base threshold 70%.")
        st.subheader("Open tickets by team")
        st.dataframe(pd.DataFrame([dict(Team=k, Open=v) for k, v in sorted(m["by_team_open"].items(), key=lambda x: -x[1])]),
                     hide_index=True, width="stretch")
