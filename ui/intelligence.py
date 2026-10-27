"""Customer Intelligence section of the Streamlit dashboard (Phase 2).

Reads the tenant's analytics warehouse (never the live request path) and lets a human run the pipeline,
explore descriptive / diagnostic / predictive / social results and approve or reject recommendations.
Charts follow the dashboard's validated categorical palette (fixed order, one hue per category); each chart has a
table view because three palette hues are below 3:1 contrast on the light surface.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from agents.shared_agent import INTENTS
from analytics import descriptive, pipeline, prescriptive
from analytics.social_sources import MastodonSource

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MUTED = "#8a8a86"
LEVEL_BADGE = {"strong": ("Strong evidence", "green"), "moderate": ("Moderate evidence", "orange"), "weak": ("Weak", "gray")}
STATUS_BADGE = {"proposed": "blue", "approved": "green", "rejected": "gray", "reverted": "orange", "expired": "gray"}


def _layout(fig, height=300):
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=30, b=8), legend=dict(orientation="h", y=-0.2),
                      hovermode="x unified", plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,.18)")
    fig.update_traces(selector=dict(type="scatter"), line=dict(width=2))
    return fig


def _table(df, label="Data table"):
    with st.expander(label, icon=":material/table:"):
        st.dataframe(df, hide_index=True, width="stretch")


def _delta(cur, prev, pct=True):
    if cur is None or prev is None:
        return None
    d = cur - prev
    return f"{d:+.1%}" if pct else f"{d:+.2f}"


def render(store, registry, category_order, category_colors):
    st.caption("Analytics run as a separate batch job over this tenant's data. It reads support data only. Live "
               "conversations never wait on it, and its only write-back is *proposed* recommendations that a human approves.")
    report, wh = pipeline.load_report(store)
    c1, c2, c3 = st.columns([1.2, 1.2, 2.6], gap="medium")
    with c1:
        if st.button("Run analytics pipeline", type="primary", icon=":material/insights:", width="stretch"):
            with st.spinner("Extract → clean → analyse → recommend…"):
                pipeline.run(store, actor="dashboard")
            st.rerun()
    with c2:
        with st.popover("Add real social data", icon=":material/public:", width="stretch"):
            st.caption("Fetch **real public posts** from a Mastodon hashtag (public API, no credentials). They are stored as "
                       "`source = mastodon` and never mixed unlabelled with synthetic posts.")
            inst = st.text_input("Instance", "mastodon.social")
            tag = st.text_input("Hashtag", "customerservice")
            lim = st.slider("Posts", 20, 200, 80, 20)
            if st.button("Fetch and re-run"):
                with st.spinner("Fetching public posts…"):
                    pipeline.run(store, social_sources=[MastodonSource(inst, tag, lim)], actor="dashboard")
                st.rerun()
    if not report:
        st.info("No analytics run yet. Click **Run analytics pipeline** to build the warehouse for this tenant.")
        return
    with c3:
        st.caption(f"Run **{report['run_id']}** · {report['generated_at']} · {report['counts']['tickets']:,} tickets "
                   f"({report['counts']['live_tickets']} live) · {report['counts']['posts']:,} posts · dataset "
                   f"`{report['dataset_hash']}` · {report['seconds']} s")
    st.caption(f"📊 Results stored in the analytics warehouse `{wh.path}` · recommendations and decisions in the support "
               "database (`recommendations`, `tasks`)")
    if report["synthetic"]:
        st.warning("**Synthetic demo data.** ShopNova is fictional. The 180-day helpdesk history, social posts and event log "
                   "come from `analytics/synthetic.py` (seeded, reproducible) and are labelled `synthetic`. Live SupportPilot "
                   "tickets are added as `supportpilot`. Nothing here describes real customers or real social media.",
                   icon=":material/science:")

    tickets = wh.read("tickets")
    posts = wh.read("social_posts")
    tabs = st.tabs([":material/dashboard: Overview", ":material/troubleshoot: Diagnostics", ":material/query_stats: Predictions",
                    ":material/recommend: Recommendations", ":material/forum: Social media", ":material/fact_check: Data quality"])

    # ---- overview (descriptive) -----------------------------------------------------------------------------
    with tabs[0]:
        if tickets.empty:
            st.info("No tickets in the warehouse.")
        else:
            dmin, dmax = tickets["date"].min(), tickets["date"].max()
            f1, f2, f3, f4 = st.columns([2, 1.4, 1.4, 1.2])
            rng = f1.date_input("Date range", (max(dmin, dmax - dt.timedelta(days=89)), dmax), min_value=dmin, max_value=dmax,
                                key="int_range")
            chans = f2.multiselect("Channel", sorted(tickets["channel"].unique()), key="int_ch")
            cats = f3.multiselect("Category", [c for c in category_order if c in set(tickets["category"])], key="int_cat")
            srcs = f4.multiselect("Source", sorted(tickets["source"].unique()), key="int_src")
            start, end = (rng if isinstance(rng, tuple) and len(rng) == 2 else (dmin, dmax))
            view = descriptive.filter_tickets(tickets, start, end, chans, cats, srcs)
            span = (end - start).days + 1
            prev = descriptive.filter_tickets(tickets, start - dt.timedelta(days=span), start - dt.timedelta(days=1), chans, cats, srcs)
            k, kp = descriptive.kpis(view), descriptive.kpis(prev)
            if k["tickets"] == 0:
                st.info("No tickets match these filters.")
            else:
                m = st.columns(6)
                m[0].metric("Tickets / day", k["per_day"], _delta(k["per_day"], kp.get("per_day"), False), border=True)
                m[1].metric("Resolution rate", f"{k['resolution_rate']:.0%}", _delta(k["resolution_rate"], kp.get("resolution_rate")), border=True)
                m[2].metric("Escalation rate", f"{k['escalation_rate']:.0%}", _delta(k["escalation_rate"], kp.get("escalation_rate")),
                            delta_color="inverse", border=True)
                m[3].metric("Median 1st response", f"{k['median_first_response_min'] or 0:.0f} min",
                            _delta(k["median_first_response_min"], kp.get("median_first_response_min"), False), delta_color="inverse", border=True)
                m[4].metric("CSAT", k["csat_avg"] or "—", _delta(k["csat_avg"], kp.get("csat_avg"), False), border=True)
                m[5].metric("SLA breach (resolved)", f"{(k['sla_breach_rate'] or 0):.0%}", _delta(k["sla_breach_rate"], kp.get("sla_breach_rate")),
                            delta_color="inverse", border=True)
                st.caption(f"Deltas compare with the previous {span} days.")
                daily = descriptive.daily(view, "category")
                fig = px.area(daily, x="date", y="tickets", color="category", category_orders={"category": category_order},
                              color_discrete_map=category_colors, title="Daily tickets by category")
                fig.update_traces(line=dict(width=0.5))
                st.plotly_chart(_layout(fig, 320), width="stretch")
                _table(daily.pivot_table(index="date", columns="category", values="tickets", fill_value=0).reset_index())
                wk = descriptive.weekly_kpis(view)
                g1, g2, g3 = st.columns(3)
                for col, y, title, fmt in [(g1, "escalation_rate", "Escalation rate (weekly)", ".0%"), (g2, "csat", "Average CSAT (weekly)", ".2f"),
                                           (g3, "median_first_response_min", "Median first response, min (weekly)", ".0f")]:
                    f = px.line(wk, x="week", y=y, title=title, markers=True, color_discrete_sequence=[PALETTE[0]])
                    f.update_yaxes(tickformat=fmt)
                    col.plotly_chart(_layout(f, 240), width="stretch")
                _table(wk, "Weekly KPIs table")
                st.markdown("##### Top complaints (last 14 days vs the 14 before)")
                tc = descriptive.top_complaints(view, 14, end)
                tc["label"] = tc["intent"].map(lambda i: INTENTS.get(i, {}).get("label", i))
                st.dataframe(tc[["label", "current", "previous", "change", "change_pct", "negative_share", "csat"]], hide_index=True,
                             width="stretch", column_config={"change_pct": st.column_config.NumberColumn("change", format="percent"),
                                                             "negative_share": st.column_config.NumberColumn("negative msgs", format="percent")})
                sent = view.assign(negative=view["sentiment_score"] <= -0.15).groupby("date")["negative"].mean().reset_index()
                f = px.line(sent, x="date", y="negative", title="Share of messages with negative sentiment",
                            color_discrete_sequence=[PALETTE[7]])
                f.update_yaxes(tickformat=".0%")
                st.plotly_chart(_layout(f, 240), width="stretch")
            auto = report["descriptive"]["automation"]
            if auto["runs"]:
                st.markdown("##### Live SupportPilot workflow outcomes")
                a1, a2, a3 = st.columns(3)
                a1.dataframe(pd.DataFrame(list(auto["decisions"].items()), columns=["decision", "runs"]), hide_index=True)
                a2.dataframe(pd.DataFrame(list(auto["actions"].items()), columns=["action status", "count"]), hide_index=True)
                a3.dataframe(pd.DataFrame(list(auto["llm_modes"].items()), columns=["LLM mode", "runs"]), hide_index=True)

    # ---- diagnostics ------------------------------------------------------------------------------------------
    with tabs[1]:
        dg = report["diagnostic"]
        ch = dg.get("change") or {}
        if ch:
            st.markdown(f"##### What changed: last {ch['window_days']} days vs the {ch['window_days']} before")
            m = st.columns(4)
            m[0].metric("Tickets", ch["tickets_current"], f"{ch['volume_change_pct']:+.1%}", border=True)
            m[1].metric("Escalation rate", f"{ch['escalation_rate_current']:.1%}",
                        f"{ch['escalation_rate_current'] - ch['escalation_rate_previous']:+.1%}", delta_color="inverse", border=True)
            m[2].metric("…from issue mix", f"{ch['escalation_mix_effect']:+.1%}", help="Change caused by which issues arrived", border=True)
            m[3].metric("…from handling", f"{ch['escalation_rate_effect']:+.1%}", help="Change in how each issue type was handled", border=True)
            contrib = pd.DataFrame(ch["contributors"])
            contrib = contrib[contrib["delta"] != 0].head(10)
            f = px.bar(contrib, x="delta", y="intent", orientation="h", title="Biggest contributors to the volume change (tickets)",
                       color_discrete_sequence=[PALETTE[0]])
            f.update_yaxes(autorange="reversed")
            st.plotly_chart(_layout(f, 300), width="stretch")
        st.markdown(f"##### Events linked to complaint spikes · {dg['tests_run']} event × issue pairs tested (Holm-corrected)")
        st.caption("Evidence is graded. **Strong**: significant, concentrated on the event's own attribute (gateway, courier, "
                   "area or product), starting the same day, with no overlapping event. All of it is association in "
                   "observational data, not proof of cause.")
        if not dg["findings"]:
            st.info("No event is significantly associated with a complaint spike.")
        events = wh.read("events")
        for fd in dg["findings"]:
            badge, color = LEVEL_BADGE[fd["evidence_level"]]
            with st.container(border=True):
                a, b = st.columns([1.1, 1.4], gap="large")
                with a:
                    st.badge(badge, color=color)
                    st.markdown(f"**{fd['event']}** → {INTENTS.get(fd['intent'], {}).get('label', fd['intent'])}")
                    st.markdown(f"{fd['observed']} tickets vs {fd['expected']} expected · **×{fd['ratio']}** · "
                                f"+{fd['excess']:.0f} excess · {fd['start']} → {fd['end']}")
                    for ev in fd["evidence"]:
                        st.caption("• " + ev)
                    st.caption("⚠️ " + fd["caveat"])
                with b:
                    s = tickets[tickets["intent"] == fd["intent"]].groupby("date").size()
                    s.index = pd.to_datetime(s.index)
                    lo = pd.Timestamp(fd["start"]) - pd.Timedelta(days=35)
                    s = s[s.index >= lo]
                    fig = go.Figure(go.Scatter(x=s.index, y=s.values, mode="lines", name=fd["intent"], line=dict(color=PALETTE[0])))
                    fig.add_vrect(x0=pd.Timestamp(fd["start"]), x1=pd.Timestamp(fd["end"]) + pd.Timedelta(days=1), fillcolor=PALETTE[3],
                                  opacity=0.18, line_width=0, annotation_text="event window", annotation_position="top left")
                    fig.update_layout(title=f"Daily {fd['intent']} tickets", showlegend=False)
                    st.plotly_chart(_layout(fig, 220), width="stretch")
        if dg["correlations"]:
            st.markdown("##### Operational metrics vs complaints")
            st.dataframe(pd.DataFrame(dg["correlations"])[["metric", "lag_days", "pearson_r", "permutation_p", "days",
                                                           "interpretation", "caveat"]], hide_index=True, width="stretch")
        un = [x for x in dg["anomalies"] if not x["explained_by_event"]]
        if dg["anomalies"]:
            st.markdown(f"##### Anomalous days · {len(un)} not explained by any logged event")
            st.dataframe(pd.DataFrame(dg["anomalies"]), hide_index=True, width="stretch")
        if report.get("validation"):
            v = report["validation"]
            with st.expander("How accurate are these diagnostics? (synthetic data only)", icon=":material/verified:"):
                st.markdown(f"The synthetic generator knows which events really change complaints (effect ≥ ×{v['min_effect']}). "
                            "That ground truth is **never** an input to the analysis. Compared with it:")
                st.markdown(f"- precision **{v['precision']}**, recall **{v['recall']}** "
                            f"({v['true_positives']}/{v['true_effects']} true effects found)\n"
                            f"- control event with no effect flagged: **{v['control_event_flagged']}**\n"
                            f"- missed: {', '.join(v['missed']) or 'none'}\n- false findings: {', '.join(v['false_findings']) or 'none'}")
        if not events.empty:
            _table(events[["name", "kind", "start", "end", "source"]], "Event log")

    # ---- predictions --------------------------------------------------------------------------------------------
    with tabs[2]:
        pr = report["predictive"]
        if not pr:
            st.info("Not enough history for predictive models.")
        else:
            fc = pr["forecast"]
            st.markdown("##### Ticket volume forecast — next 7 days")
            if fc.get("status") == "ok":
                hist = tickets.groupby("date").size()
                hist.index = pd.to_datetime(hist.index)
                hist = hist[hist.index < pd.Timestamp(report["today"])].tail(56)
                f = pd.DataFrame(fc["forecast"])
                f["date"] = pd.to_datetime(f["date"])
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=list(f["date"]) + list(f["date"][::-1]), y=list(f["high"]) + list(f["low"][::-1]),
                                         fill="toself", fillcolor="rgba(42,120,214,.15)", line=dict(width=0), name="80% interval",
                                         hoverinfo="skip"))
                fig.add_trace(go.Scatter(x=hist.index, y=hist.values, name="actual", line=dict(color=MUTED)))
                fig.add_trace(go.Scatter(x=f["date"], y=f["forecast"], name=f"forecast ({fc['chosen_method']})",
                                         line=dict(color=PALETTE[0], dash="dash")))
                st.plotly_chart(_layout(fig, 300), width="stretch")
                ev = pd.DataFrame(fc["evaluation"])
                st.caption(f"Backtest: rolling origin, 7-day horizon. The method was **selected on the older folds** and is "
                           f"**reported on the {fc['eval_folds']} newest folds** it was not chosen on. Chosen: "
                           f"**{fc['chosen_method']}**" + (" (a baseline: no model beat it)." if fc["chosen_is_baseline"] else "."))
                st.dataframe(ev, hide_index=True, width="stretch",
                             column_config={"mape": st.column_config.NumberColumn(format="percent"),
                                            "skill_vs_seasonal_naive": st.column_config.NumberColumn("skill vs seasonal naive", format="percent")})
                _table(f, "Forecast table")
            for key, title in (("escalation", "Escalation risk at ticket creation"), ("low_csat", "Low-CSAT risk (rating ≤ 2) at resolution")):
                rep = pr.get(key) or {}
                st.markdown(f"##### {title}")
                if rep.get("status") != "ok":
                    st.info(rep.get("status", "not trained"))
                    continue
                sp = rep["split"]
                st.caption(f"Time-based split. Train {sp['train'][0][:10]} → {sp['train'][1][:10]} ({sp['rows']['train']:,}), "
                           f"validation ({sp['rows']['validation']:,}), test {sp['test'][0][:10]} → {sp['test'][1][:10]} "
                           f"({sp['rows']['test']:,}). Positive rate train {sp['positive_rate']['train']:.1%} / test "
                           f"{sp['positive_rate']['test']:.1%}. Chosen on validation: **{rep['chosen_model']}**.")
                mt = pd.DataFrame(rep["test_metrics"]).T.reset_index(names="model")
                st.dataframe(mt, hide_index=True, width="stretch")
                g = rep["auc_gain_vs_intent_rate"]
                (st.success if rep["beats_baseline"] else st.warning)(
                    f"ROC-AUC gain over the per-intent baseline: **{g['mean']:+.3f}** (95% bootstrap CI {g['ci95'][0]:+.3f} … "
                    f"{g['ci95'][1]:+.3f}) — " + ("better than the baseline." if rep["beats_baseline"] else "not reliably better than the baseline."))
                if rep.get("top_features"):
                    _table(pd.DataFrame(rep["top_features"]), "Strongest model weights (standardised)")
            scores = wh.read("live_scores")
            st.markdown("##### Live SupportPilot tickets scored (advisory only — no automatic action)")
            if scores.empty:
                st.caption("No live tickets yet. Chat with SupportPilot, then re-run the pipeline.")
            else:
                st.dataframe(scores.sort_values("risk", ascending=False), hide_index=True, width="stretch",
                             column_config={"risk": st.column_config.ProgressColumn("risk", min_value=0, max_value=1, format="percent")})
            st.caption("⚠️ Models are trained mostly on synthetic history, so their scores show the method working, not "
                       "real-world accuracy. Retrain on the company's own history before relying on them.")

    # ---- recommendations ------------------------------------------------------------------------------------------
    with tabs[3]:
        recs = prescriptive.listing(store)
        st.caption("Ranked by impact × confidence ÷ effort (a prioritisation heuristic, not a savings forecast). Nothing is "
                   "applied until a person approves it. Effects are internal tasks or an escalation-threshold change that "
                   "live support then uses, and every decision is audited. Threshold changes can be reverted.")
        approver = st.text_input("Your name (recorded as the decision maker)", key="rec_approver", placeholder="e.g. Kavya Menon")
        open_recs = [r for r in recs if r["status"] == "proposed"]
        if not open_recs:
            st.info("No open recommendations.")
        for r in open_recs:
            with st.container(border=True):
                a, b = st.columns([3, 1.1], gap="large")
                with a:
                    st.markdown(f"**#{r['rank']} · {r['title']}**")
                    with st.container(horizontal=True):
                        st.badge(r["kind"].replace("_", " "), color="violet")
                        st.badge(f"impact {r['impact']} {r['impact_unit']}", color="blue")
                        st.badge(f"confidence {r['confidence']:.0%}", color="gray")
                        st.badge(f"effort {r['effort']}", color="gray")
                        st.badge(f"score {r['score']}", color="green")
                    st.markdown(r["rationale"])
                    for e in r["evidence"]:
                        st.caption("• " + e)
                    st.caption("⚠️ " + r["caveat"])
                with b:
                    eff = r["effect"]
                    st.caption("On approval: " + (f"create a task for **{eff['team']}**" if eff["type"] == "task" else
                                                  f"set the escalation threshold for **{eff['intent']}** to **{eff['threshold']:.0%}** in live support"))
                    note = st.text_input("Note", key=f"rn_{r['id']}")
                    x1, x2 = st.columns(2)
                    ok = x1.button("Approve", key=f"ra_{r['id']}", type="primary", disabled=not approver.strip())
                    no = x2.button("Reject", key=f"rr_{r['id']}", disabled=not approver.strip())
                    if ok or no:
                        prescriptive.decide(store, r["id"], bool(ok), approver.strip() + " (dashboard)", note, registry=registry)
                        st.rerun()
        done = [r for r in recs if r["status"] != "proposed"]
        if done:
            st.markdown("##### Decided")
            for r in done:
                cols = st.columns([4, 1])
                cols[0].markdown(f":{STATUS_BADGE.get(r['status'], 'gray')}-badge[{r['status']}] **{r['title']}** — "
                                 f"{r.get('decided_by', '')} {r.get('applied') or ''}")
                if r["status"] == "approved" and r["effect"]["type"] == "threshold":
                    if cols[1].button("Revert", key=f"rv_{r['id']}", disabled=not approver.strip()):
                        prescriptive.revert(store, r["id"], approver.strip() + " (dashboard)", registry=registry)
                        st.rerun()
        tasks = store.list("tasks")
        if tasks:
            st.markdown("##### Tasks created from approved recommendations")
            st.dataframe(pd.DataFrame(tasks)[["id", "title", "team", "status", "created_by", "created_at"]], hide_index=True, width="stretch")
        if store.policy.threshold_overrides:
            st.markdown("##### Escalation thresholds now overridden in live support")
            st.json(store.policy.threshold_overrides)

    # ---- social media -----------------------------------------------------------------------------------------------
    with tabs[4]:
        if posts.empty:
            st.info("No social posts.")
        else:
            src = st.multiselect("Source", sorted(posts["source"].unique()), default=sorted(posts["source"].unique()), key="soc_src",
                                 help="`synthetic` = generated demo posts · `mastodon` = real public posts")
            sp = posts[posts["source"].isin(src)] if src else posts
            if "synthetic" in set(sp["source"]):
                st.caption("🧪 `synthetic` posts are generated demo data, not real social media.")
            if "mastodon" in set(sp["source"]):
                st.caption("🌐 `mastodon` posts are real public posts fetched from a hashtag. They're not about ShopNova, "
                           "which is fictional, so they show the connector working.")
            kp = descriptive.social_kpis(sp)
            m = st.columns(4)
            m[0].metric("Posts", f"{kp['posts']:,}", border=True)
            m[1].metric("Complaints", f"{kp['complaint_share']:.0%}", border=True)
            m[2].metric("Negative", f"{kp['negative_share']:.0%}", border=True)
            m[3].metric("Engagement", f"{kp['engagement']:,}", border=True)
            top = sp["topic"].value_counts().index.tolist()
            keep = [t for t in top if t != "other"][:7]
            sp = sp.assign(topic_g=sp["topic"].where(sp["topic"].isin(keep), "other"))
            sd = sp[sp["complaint"]].groupby(["date", "topic_g"]).size().reset_index(name="complaints")
            order = keep + ["other"]
            f = px.area(sd, x="date", y="complaints", color="topic_g", category_orders={"topic_g": order},
                        color_discrete_sequence=PALETTE, title="Daily complaint posts by topic")
            f.update_traces(line=dict(width=0.5))
            st.plotly_chart(_layout(f, 320), width="stretch")
            _table(sd.pivot_table(index="date", columns="topic_g", values="complaints", fill_value=0).reset_index())
            soc = report["social"]
            st.markdown("##### Emerging issues (last 3 days vs 28-day baseline)")
            if not soc["emerging"]:
                st.caption("None detected.")
            for e in soc["emerging"]:
                with st.container(border=True):
                    st.markdown(f"**{e['topic'].replace('_', ' ')}** — {e['recent_posts']} complaint posts vs {e['expected']} expected "
                                f"(×{e['ratio']}, adjusted p={e['p_adj']:.1e}) · related tickets ×{e['ticket_ratio']}"
                                + (" · 🚨 **early warning: not yet in tickets**" if e["early_warning"] else ""))
                    for t in e["sample_posts"]:
                        st.caption(f"“{t}”")
            st.markdown("##### Do social complaints lead or lag tickets?")
            st.dataframe(pd.DataFrame(soc["lead_lag"]), hide_index=True, width="stretch")
            st.caption("Correlation of daily complaint posts with related ticket intents, at the best lag. A positive lag means "
                       "social media moves first. This is association only.")
            with st.expander("Posts", icon=":material/list:"):
                st.dataframe(sp.sort_values("created_at", ascending=False)[["created_at", "source", "platform", "topic", "sentiment",
                                                                            "complaint", "text", "likes", "url"]].head(500),
                             hide_index=True, width="stretch")

    # ---- data quality --------------------------------------------------------------------------------------------------
    with tabs[5]:
        q = report["quality"]
        st.markdown("Each cleaning decision is counted. Same tenant data plus the same seed and anchor give the same dataset hash.")
        a, b = st.columns(2)
        a.markdown("##### Tickets")
        a.dataframe(pd.DataFrame(list(q["tickets"].items()), columns=["check", "rows"]), hide_index=True, width="stretch")
        b.markdown("##### Social posts")
        b.dataframe(pd.DataFrame(list(q["social"].items()), columns=["check", "posts"]), hide_index=True, width="stretch")
        st.markdown("##### Sources")
        st.dataframe(pd.DataFrame(report["sources"]), hide_index=True, width="stretch")
        for n in report["notes"]:
            st.caption("• " + n)
        st.caption(f"Warehouse `{wh.path}` · tables: {', '.join(wh.tables())} · seed {report['seed']} · anchor {report['anchor']}")
        with st.expander("Raw report JSON"):
            st.json(json.loads(json.dumps(report, default=str)), expanded=False)


def page():
    from agents.tenancy import get_registry

    from .common import CATEGORY_COLORS, CATEGORY_ORDER, workspace
    st.title("Customer intelligence")
    render(workspace(), get_registry(), CATEGORY_ORDER, CATEGORY_COLORS)
