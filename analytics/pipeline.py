"""Customer-intelligence pipeline: one reproducible batch run per tenant.

    extract (read-only from the tenant Workspace + labelled synthetic history + social sources)
      → clean & normalise (quality report)
      → warehouse (per-tenant SQLite, rebuilt each run)
      → descriptive · diagnostic · predictive · social analytics
      → recommendations (stored in the tenant store as *proposed*; humans approve)

It runs outside the request path (dashboard button, ``manage.py analytics-run``, ``POST /v1/analytics/run``) and
only ever *reads* live support data; the one write back is proposed recommendations plus a run record.
Same tenant data + same (seed, anchor) → identical dataset hash and results.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time

import pandas as pd

from agents import audit
from agents.shared_agent import iso

from . import descriptive, diagnostic, ingest, predictive, prescriptive, social, synthetic
from .social_sources import SyntheticSocialSource
from .warehouse import Warehouse, analytics_path

BRAND_TERMS = {"demostore": ["shopnova", "novafiber", "novapay"]}
PII_RE = [(re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"), (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "[phone]")]


def _mask(text):
    for rx, rep in PII_RE:
        text = rx.sub(rep, text)
    return text


def _live_events(ws):
    return [dict(id=o["id"], name=f"{o.get('service', 'Service')} outage — {o['area']}", kind="outage", start=o["started_at"],
                 end=o.get("restored_at") or iso(), attributes=dict(area=o["area"]), source="company outages")
            for o in ws.list("outages")]


def _validate_against_truth(findings, truth, min_effect=2.0):
    """Synthetic data only: compare findings with the generator's known effects (truth is never an input)."""
    positives = {(name, intent) for name, t in truth.items() for intent, m in t["effects"].items() if m >= min_effect}
    found = {(f["event"], f["intent"]) for f in findings if f["evidence_level"] in ("strong", "moderate")}
    tp = found & positives
    control = [f for f in findings if f["event"] == "Homepage redesign"]
    return dict(true_effects=len(positives), found=len(found), true_positives=len(tp),
                precision=round(len(tp) / len(found), 3) if found else None,
                recall=round(len(tp) / len(positives), 3) if positives else None,
                missed=sorted(f"{e} → {i}" for e, i in positives - found),
                false_findings=sorted(f"{e} → {i}" for e, i in found - positives),
                control_event_flagged=bool(control), min_effect=min_effect)


def run(ws, warehouse=None, seed=7, anchor=None, synthetic_history=None, social_sources=None, actor="pipeline"):
    t0 = time.time()
    anchor = (anchor or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    today = anchor.astimezone(ingest.IST).date()
    use_synth = ws.adapter.simulated if synthetic_history is None else synthetic_history
    wh = warehouse or Warehouse(analytics_path(ws.tenant["id"]))
    notes = []

    # ---- extract ------------------------------------------------------------------------------
    raw = ingest.live_tickets(ws)
    seeded = sum(1 for t in ws.list("tickets") if t.get("historical"))
    if seeded:
        notes.append(f"{seeded} seeded demo tickets excluded (superseded by the analytics history; avoids double counting)")
    data = synthetic.make(anchor=anchor, seed=seed) if use_synth else None
    if data:
        raw += data["tickets"]
        notes.append(synthetic.LABEL)
    sources = list(social_sources or [])
    if data and not any(s.synthetic for s in sources):
        sources.insert(0, SyntheticSocialSource(data["social_posts"]))
    raw_posts, source_status = [], []
    for src in sources:
        posts = src.fetch()
        raw_posts += posts
        source_status.append(dict(source=src.name, synthetic=src.synthetic, posts=len(posts), error=getattr(src, "error", None)))

    # ---- clean ----------------------------------------------------------------------------------
    tickets, tq = ingest.clean_tickets(raw)
    posts, sq = ingest.clean_social(raw_posts, BRAND_TERMS.get(ws.tenant["id"], [ws.tenant.get("name", "").lower()]))
    events = (data["events"] if data else []) + _live_events(ws)
    gateway = pd.DataFrame(data["gateway_daily"]) if data else pd.DataFrame()
    runs, actions = ingest.live_runs(ws), ingest.live_actions(ws)
    if not tickets.empty:
        tickets["first_message"] = tickets["first_message"].map(_mask)
    dataset_hash = hashlib.sha256(
        ("|".join(f"{i}:{c}" for i, c in zip(tickets.get("id", []), tickets.get("created_at", []))) +
         "|".join(posts.get("id", []))).encode()).hexdigest()[:16]

    # ---- analytics ------------------------------------------------------------------------------
    R = dict(tickets=tickets)
    cur14 = descriptive.filter_tickets(tickets, start=today - dt.timedelta(days=13), end=today)
    prev14 = descriptive.filter_tickets(tickets, start=today - dt.timedelta(days=27), end=today - dt.timedelta(days=14))
    desc = dict(kpis_all=descriptive.kpis(tickets), kpis_last14=descriptive.kpis(cur14), kpis_prev14=descriptive.kpis(prev14),
                top_complaints=descriptive.top_complaints(tickets, 14, today).to_dict("records") if not tickets.empty else [],
                social=descriptive.social_kpis(posts),
                automation=dict(runs=int(len(runs)), llm_modes=runs["llm_mode"].value_counts().to_dict() if len(runs) else {},
                                decisions=runs["decision"].value_counts().to_dict() if len(runs) else {},
                                actions=actions["status"].value_counts().to_dict() if len(actions) else {}))
    impact = diagnostic.event_impact(tickets, events)
    R["diagnostic"] = diag = dict(change=diagnostic.metric_change(tickets, 14, today), findings=impact["findings"],
                                  tests_run=len(impact["tests"]), correlations=diagnostic.operational_correlation(tickets, gateway),
                                  anomalies=diagnostic.anomalies(tickets, events))
    pred, live_scores, models = {}, [], {}
    if len(tickets) >= 200:
        series = predictive.daily_series(tickets, today)
        fc = predictive.forecast(series)
        backtest = fc.pop("backtest", pd.DataFrame())
        pred["forecast"] = fc
        feat = predictive.creation_features(tickets)
        for key, fn in (("escalation", predictive.escalation_model), ("low_csat", predictive.low_csat_model)):
            rep = fn(feat)
            models[key] = rep
            pred[key] = predictive.public_report(rep)
        live = feat[feat["source"] == "supportpilot"]
        if len(live) and models["escalation"].get("status") == "ok":
            m = models["escalation"]
            p = m["model"].predict_proba(live[m["features"]])[:, 1]
            live_scores += [dict(ticket_id=r.id, kind="escalation", intent=r.intent, risk=round(float(x), 3),
                                 actual=bool(r.escalated), created_at=str(r.created_at)) for r, x in zip(live.itertuples(), p)]
        cand = live[live["resolved"] & live["csat"].isna()].dropna(subset=predictive.CSAT_NUM)
        if len(cand) and models["low_csat"].get("status") == "ok":
            m = models["low_csat"]
            p = m["model"].predict_proba(cand[m["features"]])[:, 1]
            R["live_low_csat_risk"] = [dict(ticket_id=r.id, intent=r.intent, risk=round(float(x), 3)) for r, x in zip(cand.itertuples(), p)]
            live_scores += [dict(x, kind="low_csat", actual=None, created_at=None) for x in R["live_low_csat_risk"]]
    else:
        backtest = pd.DataFrame()
        notes.append("Fewer than 200 tickets — predictive models skipped")
    R["predictive"] = pred
    R["social"] = soc = dict(emerging=social.emerging_issues(posts, tickets, today), lead_lag=social.lead_lag(posts, tickets, today))
    recs = prescriptive.sync(ws, prescriptive.generate(R, today))
    validation = _validate_against_truth(diag["findings"], data["truth"]) if data else None

    # ---- persist ----------------------------------------------------------------------------------
    run_id = f"AN-{ws.next_seq('analytics_run', 1):04d}"
    report = dict(run_id=run_id, tenant=ws.tenant["id"], generated_at=iso(), anchor=anchor.isoformat(), today=str(today), seed=seed,
                  synthetic=bool(data), dataset_hash=dataset_hash, notes=notes, sources=source_status,
                  quality=dict(tickets=tq, social=sq), counts=dict(tickets=int(len(tickets)), posts=int(len(posts)),
                  events=len(events), live_tickets=int((tickets["source"] == "supportpilot").sum()) if len(tickets) else 0),
                  descriptive=desc, diagnostic=diag, predictive=pred, social=soc, validation=validation,
                  recommendations=[r["id"] for r in recs if r["status"] == "proposed"], seconds=None)
    wh.write("tickets", tickets.drop(columns=["first_response_at"], errors="ignore"))
    wh.write("social_posts", posts)
    wh.write("events", pd.DataFrame(events))
    wh.write("gateway_daily", gateway)
    wh.write("runs", runs)
    wh.write("actions", actions)
    wh.write("forecast_backtest", backtest)
    wh.write("live_scores", pd.DataFrame(live_scores, columns=["ticket_id", "kind", "intent", "risk", "actual", "created_at"]))
    report["seconds"] = round(time.time() - t0, 2)
    wh.put_json("report", report)
    ws.put("analytics_runs", run_id, {k: report[k] for k in ("run_id", "generated_at", "anchor", "seed", "synthetic", "dataset_hash",
                                                             "counts", "seconds", "recommendations")} | dict(warehouse=wh.path))
    audit.log(ws, "analytics.run", actor, outcome="completed", run_id=run_id, dataset_hash=dataset_hash,
              tickets=report["counts"]["tickets"], recommendations=len(report["recommendations"]))
    return report


def load_report(ws, warehouse=None):
    wh = warehouse or Warehouse(analytics_path(ws.tenant["id"]))
    return wh.get_json("report"), wh
