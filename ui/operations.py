"""Tickets & approvals: the human-agent workspace — tickets, actions held for approval, and every workflow run."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import agents as A
from agents.shared_agent import fmt_ts, humanize_minutes

from .common import SLA_ICON, where, workspace


def _tickets(store):
    tickets = store.list("tickets")
    f1, f2, f3, f4 = st.columns([2, 1.3, 1.3, 1])
    statuses = f1.multiselect("Status", A.STATUS_FLOW, default=["Opened", "Assigned", "Pending Customer", "Resolved"])
    teams = f2.multiselect("Team", sorted({t.get("team") or "—" for t in tickets}))
    prios = f3.multiselect("Priority", ["critical", "high", "medium", "low"])
    live_only = f4.toggle("Live only", value=True, help="Hide the seeded demo history")
    rows = [t for t in tickets if (not statuses or t["status"] in statuses) and (not teams or (t.get("team") or "—") in teams)
            and (not prios or t["priority"] in prios) and (not live_only or not t.get("historical"))]
    rows.sort(key=lambda t: t["created_at"], reverse=True)
    where("support", "tickets", store)
    if not rows:
        st.info("No tickets match. Live tickets appear here as soon as a conversation needs one.")
        return
    df = pd.DataFrame([dict(Ticket=t["id"], Title=t["title"], Customer=t["customer"]["name"], Tier=t["customer"].get("tier"),
                            Priority=t["priority"], Status=t["status"], Team=t.get("team"), Owner=t.get("assignee"),
                            Channel=A.CHANNELS.get(t.get("channel"), t.get("channel")),
                            SLA=SLA_ICON[A.sla_status(t).get("state", "n/a")], Created=fmt_ts(t["created_at"])) for t in rows[:400]])
    ev = st.dataframe(df, hide_index=True, width="stretch", height=320, on_select="rerun", selection_mode="single-row", key="tkt_table")
    sel = ev.selection.rows if ev and ev.selection else []
    if not sel:
        st.caption(f"{len(rows)} ticket(s) · select one to work on it")
        return
    t = store.get("tickets", df.iloc[sel[0]]["Ticket"])
    st.divider()
    a, b = st.columns([1.6, 1], gap="large")
    with a:
        st.markdown(A.render_ticket(t))
        if t.get("transcript"):
            with st.expander("Full transcript"):
                for m in t["transcript"]:
                    st.markdown(f"**{'Customer' if m['role'] == 'user' else 'SupportPilot'}:** {m['text']}")
    with b:
        s = A.sla_status(t)
        st.metric("SLA", SLA_ICON[s.get("state", "n/a")],
                  f"{humanize_minutes(s['minutes_left'])} {'left' if s['minutes_left'] >= 0 else 'overdue'} ({s['target']})"
                  if "minutes_left" in s else None, delta_color="off", border=True)
        st.markdown("**Lifecycle**")
        for e in t.get("events", []):
            st.caption(f"{fmt_ts(e['ts'])} · **{e['status']}** · {e['actor']}" + (f" — {e['note']}" if e.get("note") else ""))
        allowed = sorted(A.TRANSITIONS.get(t["status"], []))
        if allowed:
            with st.form(f"upd_{t['id']}"):
                st.markdown("**Update ticket**")
                target = st.selectbox("Move to", ["Reopened" if x == "Opened" and t["status"] == "Resolved" else x for x in allowed])
                note = st.text_input("Note to customer")
                resolution = st.text_area("Resolution note (when resolving — may become a knowledge-base article)")
                if st.form_submit_button("Apply", type="primary"):
                    try:
                        nt = A.transition(store, t["id"], target, actor="Human Agent", note=note or resolution)
                        A.audit.log(store, "ticket.transition", "Human Agent (dashboard)", outcome=nt["status"], ticket_id=nt["id"],
                                    note=(note or resolution) or None)
                        if target == "Resolved" and resolution.strip():
                            nt["resolution"] = resolution
                            store.put("tickets", nt["id"], nt)
                            kb = A.expand_kb(store, nt, resolution)
                            st.session_state["kb_msg"] = (f"📚 New article {kb['article']['id']} saved to `kb_auto`"
                                                          if kb["created"] else f"Knowledge base not expanded: {kb['reason']}")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
        if st.session_state.get("kb_msg"):
            st.success(st.session_state.pop("kb_msg"))
        notes = sorted(store.list("notifications", ticket_id=t["id"]), key=lambda n: n["created_at"])
        if notes:
            st.markdown("**Notifications sent to the customer**")
            for n in notes:
                st.caption(f"{fmt_ts(n['created_at'])} · {A.CHANNELS.get(n['channel'], n['channel'])}: {n['message']}")


def _approvals(store):
    st.caption("Actions the policy does not let the AI run alone: refunds above the AI limit, or actions this business set "
               "to *approval*. Approving runs the action, updates the ticket and notifies the customer. Rejecting changes nothing.")
    where("support", "approvals", store)
    pend = A.approvals.pending(store)
    approver = st.text_input("Your name (recorded as the approver)", key="approver_name", placeholder="e.g. Aditi Rao")
    if not pend:
        st.info("Nothing waiting for approval.", icon=":material/approval:")
    for ap in pend:
        with st.container(border=True):
            c1, c2 = st.columns([3, 1.2], gap="large")
            with c1:
                st.markdown(f"**{ap['id']} · {ap['label']}** for {ap.get('customer_name') or ap.get('customer_id')} · ticket "
                            f"{ap.get('ticket_id') or '—'}")
                st.caption(f"Why: {ap['reason']} · requested {fmt_ts(ap['requested_at'])}")
                st.code(json.dumps(ap["params"], ensure_ascii=False), language="json")
                if store.adapter.simulated:
                    st.caption(f"Runs in {store.adapter.name} (simulated) — changes demo data only.")
            with c2:
                note = st.text_input("Note", key=f"apn_{ap['id']}")
                b1, b2 = st.columns(2)
                go = b1.button("Approve", key=f"apy_{ap['id']}", type="primary", disabled=not approver.strip())
                no = b2.button("Reject", key=f"apx_{ap['id']}", disabled=not approver.strip())
                if go or no:
                    res = A.approvals.decide(store, ap["id"], bool(go), approver.strip() + " (dashboard)", note)
                    st.session_state["ap_msg"] = f"{res['id']} {res['status']}" + (
                        f" — {res['result']['message']}" if res.get("result") else "")
                    st.rerun()
    if st.session_state.get("ap_msg"):
        st.success(st.session_state.pop("ap_msg"))
    done = sorted([a for a in store.list("approvals") if a["status"] != "pending"], key=lambda a: a["id"], reverse=True)
    if done:
        st.markdown("**Decided**")
        st.dataframe(pd.DataFrame([dict(Approval=a["id"], Action=a["label"], Customer=a.get("customer_name"), Ticket=a.get("ticket_id"),
                                        Status=a["status"], By=a.get("decided_by"), At=fmt_ts(a.get("decided_at")),
                                        Result=(a.get("result") or {}).get("message", a.get("note", ""))) for a in done[:200]]),
                     hide_index=True, width="stretch")


def _runs(store):
    runs = sorted(store.list("workflow_runs"), key=lambda r: r["id"], reverse=True)
    st.caption("Every customer message, from any channel (chat, Telegram, API, MCP), is one saved workflow run: the agent "
               "steps, each read from the company system, each LLM call and the outcome.")
    where("support", "workflow_runs", store)
    if not runs:
        st.info("No runs yet — send a message on the Support desk.")
        return
    rdf = pd.DataFrame([dict(Run=r["id"], Started=fmt_ts(r.get("started_at")), Channel=r.get("channel"), Customer=r.get("customer_id"),
                             Intent=r.get("intent"), Decision=r.get("decision"), Ticket=r.get("ticket_id"),
                             LLM=(r.get("llm") or {}).get("mode"), Reads=len(r.get("tool_calls", [])), ms=r.get("ms"),
                             Status=r.get("status")) for r in runs[:500]])
    ev = st.dataframe(rdf, hide_index=True, width="stretch", height=280, on_select="rerun", selection_mode="single-row",
                      key="runs_table", column_config={"ms": st.column_config.NumberColumn("ms", format="%.0f")})
    sel = ev.selection.rows if ev and ev.selection else []
    run = store.get("workflow_runs", rdf.iloc[sel[0]]["Run"]) if sel else runs[0]
    st.markdown(f"#### {run['id']} · {run.get('status')} · {run.get('ms', 0):.0f} ms")
    if run.get("error"):
        st.error(run["error"])
    st.markdown(f"**Customer:** {run.get('message', '')}")
    st.markdown(f"**Reply:** {run.get('reply', '')}")
    lm = run.get("llm") or {}
    st.caption(f"LLM: **{lm.get('mode')}** — {lm.get('reason', '')}")
    g1, g2 = st.columns(2, gap="large")
    with g1:
        st.markdown("**Agent steps**")
        st.dataframe(pd.DataFrame(run.get("steps") or []), hide_index=True, width="stretch")
    with g2:
        st.markdown(f"**Company-system calls** · {run.get('adapter')}" + (" (simulated)" if run.get("simulated") else ""))
        tc = run.get("tool_calls") or []
        if tc:
            st.dataframe(pd.DataFrame([dict(Tool=c["tool"], Kind=c["kind"], Args=json.dumps(c["args"], ensure_ascii=False),
                                            Status=c["status"], Result=json.dumps(c["result"], ensure_ascii=False), ms=c["ms"])
                                       for c in tc]), hide_index=True, width="stretch")
    with st.expander("Raw run record"):
        st.json(run, expanded=False)


def page():
    store = workspace()
    st.title("Tickets & approvals")
    n = len(A.approvals.pending(store))
    t1, t2, t3 = st.tabs([":material/confirmation_number: Tickets", f":material/approval: Approvals ({n})" if n else
                          ":material/approval: Approvals", ":material/history: Workflow runs"])
    with t1:
        _tickets(store)
    with t2:
        _approvals(store)
    with t3:
        _runs(store)
