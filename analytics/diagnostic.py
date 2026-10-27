"""Diagnostic analytics: why did support metrics change?

All of this is observational. Findings are graded by how much independent evidence lines up, never "proven":
  * significance — window count vs a weekday-adjusted baseline (Poisson tail test, Holm-corrected across every
    event × intent pair tested, so testing many pairs does not manufacture findings);
  * attribute concentration — e.g. payment complaints during a "PayFast degradation" are mostly on PayFast;
  * timing — the spike starts within a day of the event;
  * confounding — overlapping events are listed, because they could explain the same spike.
Correlations with operational metrics use a block-permutation test (weekly blocks keep the weekday pattern).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from scipy.stats import poisson

ATTRIBUTES = ("gateway", "courier", "area", "sku")
CAVEAT = ("Association in observational data — consistent with this event contributing, but not proof of causation. "
          "Confirm with the owning team (logs, vendor reports) before acting.")


def _dates(df):
    return pd.to_datetime(df["date"])


def _dow_factors(df):
    d = df.groupby("date").size()
    d.index = pd.to_datetime(d.index)
    f = d.groupby(d.index.dayofweek).mean()
    return (f / f.mean()).reindex(range(7), fill_value=1.0)


def _event_days(e):
    start = pd.Timestamp(e["start"]).tz_convert("Asia/Kolkata").normalize().tz_localize(None)
    end = pd.Timestamp(e["end"]).tz_convert("Asia/Kolkata").normalize().tz_localize(None)
    days = pd.date_range(start, max(end - pd.Timedelta(days=1), start))
    return days


def holm(pvals):
    p = np.asarray(pvals, dtype=float)
    order = np.argsort(p)
    adj = np.empty_like(p)
    running = 0.0
    m = len(p)
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adj[i] = running
    return adj


def metric_change(df, current_days=14, ref=None):
    """Volume and escalation-rate change between two equal windows, with contributions by intent."""
    if df.empty:
        return {}
    ref = ref or df["date"].max()
    cur = df[(df["date"] > ref - dt.timedelta(days=current_days)) & (df["date"] <= ref)]
    prev = df[(df["date"] > ref - dt.timedelta(days=2 * current_days)) & (df["date"] <= ref - dt.timedelta(days=current_days))]
    if cur.empty or prev.empty:
        return {}
    vc, vp = cur["intent"].value_counts(), prev["intent"].value_counts()
    vol = pd.DataFrame(dict(current=vc, previous=vp)).fillna(0)
    vol["delta"] = vol["current"] - vol["previous"]
    total_delta = vol["delta"].sum()
    vol["share_of_change"] = vol["delta"] / total_delta if total_delta else 0.0
    # escalation rate: mix effect (which issues arrive) vs rate effect (how each issue is handled)
    sc, sp = vc / vc.sum(), vp / vp.sum()
    rc, rp = cur.groupby("intent")["escalated"].mean(), prev.groupby("intent")["escalated"].mean()
    idx = sc.index.union(sp.index)
    sc, sp = sc.reindex(idx, fill_value=0), sp.reindex(idx, fill_value=0)
    rc, rp = rc.reindex(idx).fillna(rp.reindex(idx)).fillna(0), rp.reindex(idx).fillna(rc.reindex(idx)).fillna(0)
    mix = float(((sc - sp) * rp).sum())
    rate = float((sc * (rc - rp)).sum())
    csat_c, csat_p = cur["csat"].mean(), prev["csat"].mean()
    return dict(
        window_days=current_days, current_end=str(ref), tickets_current=int(len(cur)), tickets_previous=int(len(prev)),
        volume_change_pct=round(len(cur) / len(prev) - 1, 3),
        escalation_rate_current=round(cur["escalated"].mean(), 3), escalation_rate_previous=round(prev["escalated"].mean(), 3),
        escalation_mix_effect=round(mix, 4), escalation_rate_effect=round(rate, 4),
        csat_current=None if np.isnan(csat_c) else round(csat_c, 2), csat_previous=None if np.isnan(csat_p) else round(csat_p, 2),
        contributors=vol.sort_values("delta", ascending=False).reset_index(names="intent")
        .assign(current=lambda x: x["current"].astype(int), previous=lambda x: x["previous"].astype(int),
                delta=lambda x: x["delta"].astype(int), share_of_change=lambda x: x["share_of_change"].round(3))
        .to_dict("records"))


def event_impact(df, events, min_ratio=1.5, alpha=0.01, baseline_days=28):
    """Test every event against every intent; return graded findings (and every test, for transparency)."""
    if df.empty or not events:
        return dict(findings=[], tests=[])
    dates = _dates(df)
    dow = _dow_factors(df)
    daily = df.assign(_d=dates).groupby(["intent", "_d"]).size()
    windows = {e["id"]: _event_days(e) for e in events}
    covered = set()
    for days in windows.values():
        covered.update(days)
    intents = sorted(df["intent"].unique())
    tests = []
    last_day = dates.max()
    for e in events:
        days = windows[e["id"]]
        days = days[days <= last_day]
        if len(days) == 0:
            continue
        base_days = pd.date_range(days[0] - pd.Timedelta(days=baseline_days), days[0] - pd.Timedelta(days=1))
        base_days = [d for d in base_days if d not in covered]
        if len(base_days) < 10:
            continue
        overlaps = [o["name"] for o in events if o["id"] != e["id"] and len(set(windows[o["id"]]) & set(days))]
        for intent in intents:
            s = daily.get(intent, pd.Series(dtype=float))
            base = np.array([s.get(d, 0) / dow[d.dayofweek] for d in base_days], dtype=float)
            rate = base.mean()
            expected = float(sum(rate * dow[d.dayofweek] for d in days))
            observed = int(sum(s.get(d, 0) for d in days))
            p = float(poisson.sf(observed - 1, max(expected, 0.5)))
            tests.append(dict(event_id=e["id"], event=e["name"], kind=e["kind"], intent=intent, observed=observed,
                              expected=round(expected, 1), ratio=round(observed / max(expected, 0.5), 2), p=p,
                              overlaps=overlaps, _days=days, _base=base_days, _rate=rate))
    if not tests:
        return dict(findings=[], tests=[])
    for t, padj in zip(tests, holm([t["p"] for t in tests])):
        t["p_adj"] = float(padj)
    findings = []
    for t in tests:
        if not (t["p_adj"] < alpha and t["ratio"] >= min_ratio and t["observed"] >= 8):
            continue
        e = next(x for x in events if x["id"] == t["event_id"])
        win = df[dates.isin(t["_days"]) & (df["intent"] == t["intent"])]
        base = df[dates.isin(t["_base"]) & (df["intent"] == t["intent"])]
        evidence = [f"{t['observed']} {t['intent']} tickets vs {t['expected']} expected (×{t['ratio']}), "
                    f"Holm-adjusted p={t['p_adj']:.1e}"]
        attr_ok = None
        for key, value in (e.get("attributes") or {}).items():
            if key in ATTRIBUTES and len(win):
                share_w = float((win[key] == value).mean())
                share_b = float((base[key] == value).mean()) if len(base) else 0.0
                attr_ok = share_w >= 0.5 and share_w >= 1.5 * max(share_b, 0.01)
                evidence.append(f"{share_w:.0%} of these tickets involve {key} {value} (baseline {share_b:.0%})")
        # onset: first day from 2 days before the event where the daily count clearly exceeds expectation
        s = df[df["intent"] == t["intent"]].groupby(dates).size()
        onset = None
        for d in pd.date_range(t["_days"][0] - pd.Timedelta(days=2), t["_days"][-1]):
            exp_d = t["_rate"] * float(_dow_factors(df)[d.dayofweek])
            if s.get(d, 0) > exp_d + 2.5 * np.sqrt(max(exp_d, 1)):
                onset = d
                break
        lag = None if onset is None else int((onset - t["_days"][0]).days)
        aligned = lag is not None and -1 <= lag <= 1
        evidence.append("spike starts the same day as the event" if lag == 0 else
                        f"spike starts {lag:+d} day(s) from the event start" if lag is not None else "no clear onset day")
        if t["overlaps"]:
            evidence.append("overlapping events that could also explain it: " + ", ".join(t["overlaps"]))
        if attr_ok and aligned and not t["overlaps"]:
            level = "strong"
        elif (attr_ok or aligned) and attr_ok is not False:
            level = "moderate"
        else:
            level = "weak"
        findings.append(dict(event_id=t["event_id"], event=t["event"], kind=t["kind"], intent=t["intent"],
                             observed=t["observed"], expected=t["expected"], excess=round(t["observed"] - t["expected"], 1),
                             ratio=t["ratio"], p_adj=t["p_adj"], onset_lag_days=lag, attribute_match=attr_ok,
                             overlapping_events=t["overlaps"], evidence_level=level, evidence=evidence, caveat=CAVEAT,
                             start=str(t["_days"][0].date()), end=str(t["_days"][-1].date())))
    rank = {"strong": 0, "moderate": 1, "weak": 2}
    findings.sort(key=lambda f: (rank[f["evidence_level"]], -f["excess"]))
    clean_tests = [{k: v for k, v in t.items() if not k.startswith("_")} for t in tests]
    return dict(findings=findings, tests=clean_tests)


def _block_permutation_p(x, y, r_obs, iters=500, block=7, seed=0):
    rng = np.random.default_rng(seed)
    n = len(x)
    blocks = [np.arange(i, min(i + block, n)) for i in range(0, n, block)]
    hits = 0
    for _ in range(iters):
        order = np.concatenate([blocks[i] for i in rng.permutation(len(blocks))])[:n]
        r = np.corrcoef(x, y[order])[0, 1]
        hits += abs(r) >= abs(r_obs)
    return (hits + 1) / (iters + 1)


def operational_correlation(df, gateway_daily, intents=("payment_failure", "double_charge")):
    """Daily payment complaints vs each gateway's failure rate (lags 0–2 days)."""
    if df.empty or gateway_daily is None or gateway_daily.empty:
        return []
    complaints = df[df["intent"].isin(intents)].groupby(_dates(df)).size()
    gd = gateway_daily.copy()
    gd["date"] = pd.to_datetime(gd["date"])
    out = []
    for g, rows in gd.groupby("gateway"):
        rate = (rows.set_index("date")["failures"] / rows.set_index("date")["attempts"]).sort_index()
        idx = rate.index
        y = complaints.reindex(idx, fill_value=0).astype(float).values
        best = None
        for lag in (0, 1, 2):
            x = rate.shift(lag).values
            ok = ~np.isnan(x)
            if ok.sum() < 30 or np.std(x[ok]) == 0:
                continue
            r = float(np.corrcoef(x[ok], y[ok])[0, 1])
            if best is None or abs(r) > abs(best[1]):
                best = (lag, r, ok)
        if best is None:
            continue
        lag, r, ok = best
        p = _block_permutation_p(rate.shift(lag).values[ok], y[ok], r)
        out.append(dict(metric=f"{g} payment failure rate", gateway=g, complaint_intents=list(intents), lag_days=lag,
                        pearson_r=round(r, 3), permutation_p=round(float(p), 4), days=int(ok.sum()),
                        interpretation=("strong" if abs(r) >= 0.6 and p < 0.01 else "moderate" if abs(r) >= 0.3 and p < 0.05
                                        else "none"),
                        caveat="Correlation only: a shared cause (e.g. a bank-side outage) could move both."))
    return sorted(out, key=lambda c: -abs(c["pearson_r"]))


def anomalies(df, events=(), z=3.5):
    """Days where a category's volume is far from its weekday-adjusted rolling median; flags unexplained ones."""
    if df.empty:
        return []
    covered = set()
    for e in events:
        for d in _event_days(e):
            covered.update({d - pd.Timedelta(days=1), d, d + pd.Timedelta(days=1)})
    out = []
    dow = _dow_factors(df)
    for cat, rows in df.groupby("category"):
        s = rows.groupby(_dates(rows)).size()
        s = s.reindex(pd.date_range(s.index.min(), s.index.max()), fill_value=0).astype(float)
        adj = s / dow.reindex(s.index.dayofweek).values
        med = adj.shift(1).rolling(28, min_periods=14).median()
        mad = (adj.shift(1) - med).abs().rolling(28, min_periods=14).median() * 1.4826
        score = (adj - med) / mad.clip(lower=1.0)
        for d, sc in score.items():
            if sc >= z and s[d] >= 5 and d != s.index.max():
                out.append(dict(date=str(d.date()), category=cat, tickets=int(s[d]), expected=round(float(med[d] * dow[d.dayofweek]), 1),
                                robust_z=round(float(sc), 1), explained_by_event=d in covered))
    return sorted(out, key=lambda a: a["date"], reverse=True)
