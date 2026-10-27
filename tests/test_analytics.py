"""Phase 2: customer-intelligence pipeline — data quality, diagnostics vs ground truth, leakage-free models,
human-approved recommendations feeding live support, tenant isolation."""
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPPORTPILOT_DISABLE_LLM"] = "1"

import agents as A  # noqa: E402
from agents.tenancy import Registry  # noqa: E402
from analytics import diagnostic, ingest, pipeline, predictive, prescriptive, social, synthetic  # noqa: E402
from analytics.social_sources import MastodonSource  # noqa: E402
from analytics.warehouse import Warehouse  # noqa: E402

ANCHOR = dt.datetime(2026, 10, 27, 12, tzinfo=dt.timezone.utc)
TODAY = ANCHOR.astimezone(ingest.IST).date()


@pytest.fixture(scope="module")
def world():
    return synthetic.make(anchor=ANCHOR, seed=7)


@pytest.fixture(scope="module")
def clean(world):
    return ingest.clean_tickets(world["tickets"])


@pytest.fixture(scope="module")
def feat(clean):
    return predictive.creation_features(clean[0])


@pytest.fixture(scope="module")
def run_result():
    ws = A.as_workspace(A.Store(":memory:"))
    A.handle_message(ws, "My order was delivered but I received the wrong item", None, "CUST1001", use_llm=False)
    before = json.dumps(sorted(ws.list("tickets"), key=lambda t: t["id"]), sort_keys=True)
    wh = Warehouse(":memory:")
    rep = pipeline.run(ws, wh, anchor=ANCHOR)
    after = json.dumps(sorted(ws.list("tickets"), key=lambda t: t["id"]), sort_keys=True)
    return dict(ws=ws, wh=wh, rep=rep, tickets_unchanged=before == after)


# ---- data ------------------------------------------------------------------------------------------------------
def test_synthetic_data_is_reproducible_and_labelled(world):
    again = synthetic.make(anchor=ANCHOR, seed=7)
    assert again["tickets"] == world["tickets"] and again["social_posts"] == world["social_posts"]
    assert synthetic.make(anchor=ANCHOR, seed=8)["tickets"] != world["tickets"]
    assert all(t["source"] == "synthetic" for t in world["tickets"])
    assert all(p["source"] == "synthetic" for p in world["social_posts"])
    assert "SYNTHETIC" in world["label"]


def test_cleaning_handles_duplicates_missing_and_inconsistent_records(world, clean):
    df, rep = clean
    assert rep["exact_duplicates_removed"] > 0 and rep["stale_versions_removed"] > 0
    assert df["id"].is_unique and len(df) == len({t["id"] for t in world["tickets"]})
    assert set(df["channel"]) <= {"web", "whatsapp", "email", "app", "telegram", "unknown"}
    assert rep["channel_fixed"] > 0 and rep["intent_fixed"] > 0 and rep["invalid_csat_nulled"] > 0
    assert df["csat"].dropna().between(1, 5).all()
    assert (df["resolved_at"].dropna() >= df.loc[df["resolved_at"].notna(), "created_at"]).all()
    # the most complete version of a re-exported ticket wins
    stale_ids = {t["id"] for t in world["tickets"] if t.get("export_version") == 0}
    assert stale_ids and not (df[df["id"].isin(stale_ids)]["status"] == "Assigned").any()


def test_timestamp_formats():
    assert ingest.parse_time("2026-10-01T10:00:00Z") == pd.Timestamp("2026-10-01T10:00:00Z")
    assert ingest.parse_time("01/10/2026 15:30") == pd.Timestamp("2026-10-01T10:00:00Z")  # IST export
    assert pd.isna(ingest.parse_time("yesterday")) and pd.isna(ingest.parse_time(None))


def test_social_cleaning(world):
    sd, rep = ingest.clean_social(world["social_posts"], ["shopnova", "novafiber", "novapay"])
    assert rep["duplicate_posts_removed"] > 0 and rep["off_brand_dropped"] > 0
    assert sd["id"].is_unique and set(sd["source"]) == {"synthetic"}
    assert not sd["text"].str.contains("Cricket match").any()


# ---- diagnostics: checked against the generator's ground truth ------------------------------------------------
def test_diagnostics_find_the_planted_causes_and_not_the_control(world, clean):
    res = diagnostic.event_impact(clean[0], world["events"])
    strong = {(f["event"], f["intent"]) for f in res["findings"] if f["evidence_level"] == "strong"}
    assert ("PayFast gateway degradation", "payment_failure") in strong
    assert ("SwiftShip monsoon disruption", "delivery_delay") in strong
    assert ("Air Fryer HM-044 batch defect", "damaged_item") in strong
    assert not [f for f in res["findings"] if f["event"] == "Homepage redesign"]
    assert all("not proof of causation" in f["caveat"] for f in res["findings"])
    assert len(res["tests"]) > 100  # many pairs tested → Holm correction matters


def test_holm_correction_is_monotone_and_conservative():
    p = [0.001, 0.04, 0.03, 0.5]
    adj = diagnostic.holm(p)
    assert all(a >= b for a, b in zip(adj, p)) and adj[0] == pytest.approx(0.004)


def test_operational_correlation_singles_out_the_degraded_gateway(world, clean):
    cor = diagnostic.operational_correlation(clean[0], pd.DataFrame(world["gateway_daily"]))
    assert cor[0]["gateway"] == "PayFast" and cor[0]["interpretation"] == "strong" and cor[0]["permutation_p"] < 0.01
    assert all(c["interpretation"] == "none" for c in cor[1:])


# ---- predictive: no leakage, honest baselines ----------------------------------------------------------------------
def test_forecast_backtest_has_no_lookahead_and_separate_selection_folds(clean):
    series = predictive.daily_series(clean[0], TODAY)
    assert series.index.max() < pd.Timestamp(TODAY)  # today's partial day is excluded
    fc = predictive.forecast(series)
    bt = fc["backtest"]
    assert (pd.to_datetime(bt["date"]) > pd.to_datetime(bt["origin"])).all()
    assert fc["eval_folds"] == 8 and fc["chosen_method"] in predictive.METHODS
    assert {e["method"] for e in fc["evaluation"]} == set(predictive.METHODS)
    assert len(fc["forecast"]) == 7 and all(d["low"] <= d["forecast"] <= d["high"] for d in fc["forecast"])


def test_creation_features_use_only_the_past(clean):
    df = clean[0]
    base = predictive.creation_features(df).set_index("id")
    cut = df["created_at"].iloc[len(df) // 2]
    future = df[df["created_at"] > cut].copy()
    future["customer_id"] = df["customer_id"].iloc[0]  # flood the future with one customer's tickets
    changed = predictive.creation_features(pd.concat([df[df["created_at"] <= cut], future])).set_index("id")
    past = df[df["created_at"] <= cut]["id"]
    assert (base.loc[past, ["prior_30d", "intent_spike"]] == changed.loc[past, ["prior_30d", "intent_spike"]]).all().all()


def test_temporal_split_is_ordered(feat):
    tr, va, te = predictive.temporal_split(feat)
    assert tr["created_at"].max() <= va["created_at"].min() and va["created_at"].max() <= te["created_at"].min()


def test_models_beat_baselines_on_held_out_future_data(feat):
    esc, low = predictive.escalation_model(feat), predictive.low_csat_model(feat)
    for rep in (esc, low):
        m = rep["test_metrics"]
        assert rep["beats_baseline"] and rep["auc_gain_vs_intent_rate"]["ci95"][0] > 0
        assert m[rep["chosen_model"]]["roc_auc"] > m["per-intent rate baseline"]["roc_auc"] > m["prevalence baseline"]["roc_auc"]
        assert "reopened" not in rep["features"]  # can happen after the rating: excluded


def test_models_do_not_claim_skill_when_there_is_none(feat):
    noise = feat.copy()
    noise["escalated"] = np.random.default_rng(0).random(len(noise)) < 0.4  # labels unrelated to features
    rep = predictive.escalation_model(noise)
    assert not rep["beats_baseline"]
    assert abs(rep["test_metrics"][rep["chosen_model"]]["roc_auc"] - 0.5) < 0.05


# ---- social -------------------------------------------------------------------------------------------------------
def test_emerging_social_issue_is_detected(world, clean):
    sd, _ = ingest.clean_social(world["social_posts"], ["shopnova", "novafiber", "novapay"])
    em = social.emerging_issues(sd, clean[0], TODAY)
    assert em and em[0]["topic"] == "wallet" and em[0]["ratio"] >= 3 and em[0]["sample_posts"]
    assert all(e["topic"] != "pricing" for e in em)


class FakeMastodon:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(params)
        page = self.pages.pop(0) if self.pages else []
        return type("R", (), {"status_code": 200, "json": lambda self: page})()


def test_mastodon_connector_parses_pages_and_hashes_authors():
    page = [dict(id=str(i), created_at="2026-10-20T10:00:00Z", content=f"<p>Order late &amp; no reply #{i}</p>",
                 account=dict(acct="alice@x.y"), favourites_count=2, reblogs_count=1, language="en", url=f"https://m/{i}")
            for i in range(40)]
    page2 = [dict(p, id=str(100 + i), content=f"<p>Refund never arrived, case {i}</p>") for i, p in enumerate(page[:5])]
    fake = FakeMastodon([page, page2])
    posts = MastodonSource("m.example", "#help", limit=60, session=fake, pause_s=0).fetch()
    assert len(posts) == 45 and fake.calls[1]["max_id"] == "39"
    assert posts[0]["text"] == "Order late & no reply #0" and posts[0]["source"] == "mastodon"
    assert "alice" not in posts[0]["author"]
    sd, _ = ingest.clean_social(posts, [])
    assert len(sd) == 45 and sd["complaint"].all()


def test_mastodon_errors_are_reported_not_raised():
    class Down:
        def get(self, *a, **k):
            raise ConnectionError("no network")
    src = MastodonSource(session=Down(), pause_s=0)
    assert src.fetch() == [] and "ConnectionError" in src.error


# ---- pipeline + integration with live support ------------------------------------------------------------------------
def test_pipeline_is_reproducible_and_reads_live_data_without_changing_it(run_result):
    rep, ws = run_result["rep"], run_result["ws"]
    assert run_result["tickets_unchanged"]
    assert rep["counts"]["live_tickets"] == 1 and any("seeded demo tickets excluded" in n for n in rep["notes"])
    assert rep["synthetic"] and rep["validation"]["precision"] == 1.0 and not rep["validation"]["control_event_flagged"]
    again = pipeline.run(ws, Warehouse(":memory:"), anchor=ANCHOR)
    assert again["dataset_hash"] == rep["dataset_hash"]
    assert {"tickets", "social_posts", "events", "forecast_backtest", "live_scores"} <= set(run_result["wh"].tables())
    json.dumps(rep, default=str)
    scores = run_result["wh"].read("live_scores")
    assert len(scores[scores["kind"] == "escalation"]) == 1


def test_recommendations_are_ranked_with_evidence_and_wait_for_a_human(run_result):
    recs = prescriptive.listing(run_result["ws"], "proposed")
    assert recs and [r["rank"] for r in recs] == sorted(r["rank"] for r in recs)
    assert all(r["evidence"] and r["caveat"] for r in recs)
    assert not run_result["ws"].list("tasks")
    assert not run_result["ws"].policy.threshold_overrides


def test_approved_threshold_change_reaches_live_support_and_can_be_reverted():
    ws = A.as_workspace(A.Store(":memory:"))
    reg = Registry(":memory:", demo_db_path=":memory:")
    pipeline.run(ws, Warehouse(":memory:"), anchor=ANCHOR)
    rec = next(r for r in prescriptive.listing(ws) if r["effect"]["type"] == "threshold")
    with pytest.raises(ValueError):
        prescriptive.decide(ws, rec["id"], True, "  ")
    prescriptive.decide(ws, rec["id"], True, "Kavya", "trial for two weeks", registry=reg)
    intent = rec["effect"]["intent"]
    assert ws.policy.threshold_overrides[intent]["threshold"] == 0.9
    r = A.handle_message(ws, "I want a refund for my order ORD12346", None, "CUST1002", use_llm=False)
    if r["issue"]["intent"] == intent and isinstance(r.get("decision"), dict):
        assert r["decision"]["threshold"] >= 0.9
    with pytest.raises(ValueError):
        prescriptive.decide(ws, rec["id"], True, "Kavya")
    prescriptive.revert(ws, rec["id"], "Kavya")
    assert intent not in ws.policy.threshold_overrides
    events = [a["event"] for a in A.audit.recent(ws)]
    assert "recommendation.approved" in events and "recommendation.reverted" in events


def test_approving_a_task_recommendation_creates_a_task_only():
    ws = A.as_workspace(A.Store(":memory:"))
    pipeline.run(ws, Warehouse(":memory:"), anchor=ANCHOR)
    rec = next(r for r in prescriptive.listing(ws) if r["effect"]["type"] == "task")
    tickets_before = len(ws.list("tickets"))
    out = prescriptive.decide(ws, rec["id"], True, "Kavya")
    task = ws.get("tasks", out["applied"]["task_id"])
    assert task["team"] == rec["effect"]["team"] and task["recommendation_id"] == rec["id"]
    assert len(ws.list("tickets")) == tickets_before and not ws.list("notifications", kind="recommendation")
    # a re-run never resurrects a decided recommendation
    pipeline.run(ws, Warehouse(":memory:"), anchor=ANCHOR)
    assert ws.get("recommendations", rec["id"])["status"] == "approved"


def test_live_threshold_override_is_used_by_escalation(monkeypatch):
    ws = A.as_workspace(A.Store(":memory:"))
    ws.policy.threshold_overrides["wrong_item"] = dict(threshold=0.99, reason="test — escalate more")
    r = A.handle_message(ws, "My order was delivered but I received the wrong item", None, "CUST1001", use_llm=False)
    assert r["decision"]["threshold"] == 0.99 and r["decision"]["decision"] == "escalate"


def test_non_simulated_tenant_gets_no_synthetic_history():
    ws = A.as_workspace(A.Store(":memory:"))
    rep = pipeline.run(ws, Warehouse(":memory:"), anchor=ANCHOR, synthetic_history=False)
    assert not rep["synthetic"] and rep["counts"]["tickets"] == 0 and rep["validation"] is None
    assert any("Fewer than 200 tickets" in n for n in rep["notes"])


# ---- API + tenant isolation ----------------------------------------------------------------------------------------------
def test_analytics_api_respects_roles_and_tenants(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from api import server
    monkeypatch.setenv("SUPPORTPILOT_ANALYTICS_DIR", str(tmp_path))
    reg = Registry(":memory:", demo_db_path=":memory:")
    reg.create_tenant("acme", "Acme", dict(type="demostore"), db_path=":memory:")
    server.set_registry(reg)
    server._hits.clear()
    try:
        demo_admin = {"Authorization": "Bearer " + reg.create_key("demostore", "admin")[0]}
        demo_channel = {"Authorization": "Bearer " + reg.create_key("demostore", "channel")[0]}
        acme_admin = {"Authorization": "Bearer " + reg.create_key("acme", "admin")[0]}
        c = TestClient(server.app)
        assert c.get("/v1/analytics/summary", headers=demo_admin).status_code == 404  # nothing run yet
        assert c.post("/v1/analytics/run", headers=demo_channel).status_code == 403
        r = c.post("/v1/analytics/run", headers=demo_admin)
        assert r.status_code == 200 and r.json()["synthetic"]
        assert (tmp_path / "demostore.db").exists() and not (tmp_path / "acme.db").exists()
        assert c.get("/v1/analytics/summary", headers=acme_admin).status_code == 404  # other tenant's warehouse is invisible
        recs = c.get("/v1/analytics/recommendations", headers=demo_admin).json()
        assert recs and c.get("/v1/analytics/recommendations", headers=acme_admin).json() == []
        assert c.post(f"/v1/analytics/recommendations/{recs[0]['id']}/decision", headers=acme_admin,
                      json={"approve": True, "approver": "x"}).status_code == 404
        d = c.post(f"/v1/analytics/recommendations/{recs[0]['id']}/decision", headers=demo_admin,
                   json={"approve": False, "approver": "Kavya", "note": "not now"})
        assert d.status_code == 200 and d.json()["status"] == "rejected"
        for path in ("summary", "diagnostics", "predictions", "social"):
            assert c.get(f"/v1/analytics/{path}", headers=demo_admin).status_code == 200
    finally:
        server.set_registry(None)
