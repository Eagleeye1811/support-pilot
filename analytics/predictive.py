"""Predictive analytics, each model evaluated against simple baselines on data it never saw.

1. Ticket-volume forecast (next 7 days). Rolling-origin backtest over the last 16 weeks with horizon 7: the method
   is *selected* on the older 8 folds and *reported* on the newer 8, so the reported error is not tuned on itself.
   Baselines: seasonal naive (same weekday last week) and the 7-day mean. Today's partial day is never used.
2. Escalation risk at ticket creation. Features are only what is known when the ticket arrives (intent, channel,
   tier, priority, message sentiment/length, hour/weekday, the customer's tickets in the previous 30 days, and how
   unusual the last 24h of the same intent is). Split by time: train 70% → validation 15% (model choice) → test 15%.
   Baselines: prevalence and the per-intent escalation rate learned on the training window.
3. Low-CSAT risk (rating ≤ 2) at resolution time: creation features + escalated, response/resolution time and SLA
   breach. Re-opening is excluded (it can happen after the rating — leakage). Same split protocol and baselines.
All preprocessing is fitted on the training window only (sklearn Pipeline).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

CAT = ["intent", "category", "channel", "tier", "priority", "emotion"]
NUM = ["sentiment_score", "msg_len", "hour", "dow", "prior_30d", "intent_spike"]
CSAT_NUM = NUM + ["escalated_f", "log_resolution_min", "log_first_response_min", "sla_breached_f"]


# ---- forecasting -----------------------------------------------------------------------------------------
def daily_series(df, today):
    """Complete days only: counts per local date, strictly before ``today``."""
    s = df.groupby("date").size()
    s.index = pd.to_datetime(s.index)
    s = s[s.index < pd.Timestamp(today)]
    if s.empty:
        return s.astype(float)
    return s.reindex(pd.date_range(s.index.min(), s.index.max()), fill_value=0).astype(float)


def f_snaive(train, h):
    last = train.values[-7:]
    return np.array([last[i % 7] for i in range(h)])


def f_mean7(train, h):
    return np.repeat(train.values[-7:].mean(), h)


def _ridge_rows(values, dates, i):
    lags = [values[i - k] for k in range(1, 15)]
    dow = [1.0 if dates[i].dayofweek == k else 0.0 for k in range(7)]
    return lags + [np.mean(values[i - 7:i]), np.mean(values[i - 28:i]), i / 365.0] + dow


def f_ridge(train, h):
    vals, dates = list(train.values), list(train.index)
    if len(vals) < 60:
        return f_snaive(train, h)
    X = [_ridge_rows(vals, dates, i) for i in range(28, len(vals))]
    model = Ridge(alpha=3.0).fit(np.array(X), np.array(vals[28:]))
    out = []
    for _ in range(h):
        dates.append(dates[-1] + pd.Timedelta(days=1))
        vals.append(0.0)
        x = _ridge_rows(vals, dates, len(vals) - 1)
        vals[-1] = max(0.0, float(model.predict(np.array([x]))[0]))
        out.append(vals[-1])
    return np.array(out)


METHODS = {"seasonal_naive": f_snaive, "mean_7d": f_mean7, "ridge_lags": f_ridge}
BASELINES = {"seasonal_naive", "mean_7d"}


def backtest(series, folds=16, horizon=7):
    n = len(series)
    rows = []
    for k in range(folds, 0, -1):
        o = n - horizon * k
        if o < 60:
            continue
        train, test = series.iloc[:o], series.iloc[o:o + horizon]
        for name, fn in METHODS.items():
            pred = fn(train, len(test))
            for step, (d, y, p) in enumerate(zip(test.index, test.values, pred), 1):
                rows.append(dict(fold=k, origin=str(train.index[-1].date()), date=str(d.date()), step=step, method=name,
                                 actual=y, predicted=round(float(p), 2), error=float(p - y)))
    return pd.DataFrame(rows)


def forecast(series, horizon=7, folds=16):
    bt = backtest(series, folds, horizon)
    if bt.empty:
        return dict(status="insufficient history", days=len(series))
    half = folds // 2
    sel, ev = bt[bt["fold"] > half], bt[bt["fold"] <= half]
    sel_mae = sel.assign(ae=sel["error"].abs()).groupby("method")["ae"].mean()
    chosen = sel_mae.idxmin()

    def summary(part):
        g = part.assign(ae=part["error"].abs(), ape=(part["error"].abs() / part["actual"].clip(lower=1)))
        return g.groupby("method").agg(mae=("ae", "mean"), mape=("ape", "mean")).round(3)
    ev_sum = summary(ev)
    snaive_mae = ev_sum.loc["seasonal_naive", "mae"]
    ev_sum["skill_vs_seasonal_naive"] = (1 - ev_sum["mae"] / snaive_mae).round(3)
    resid = ev[ev["method"] == chosen]
    q = resid.groupby("step")["error"].quantile([0.1, 0.9]).unstack()
    pred = METHODS[chosen](series, horizon)
    dates = pd.date_range(series.index[-1] + pd.Timedelta(days=1), periods=horizon)
    fc = [dict(date=str(d.date()), forecast=round(float(p), 1),
               low=round(max(0.0, float(p - q.loc[i + 1, 0.9])), 1) if i + 1 in q.index else None,
               high=round(float(p - q.loc[i + 1, 0.1]), 1) if i + 1 in q.index else None)
          for i, (d, p) in enumerate(zip(dates, pred))]
    return dict(status="ok", chosen_method=chosen, chosen_is_baseline=chosen in BASELINES,
                selection_mae=sel_mae.round(3).to_dict(), evaluation=ev_sum.reset_index().to_dict("records"),
                eval_folds=int(ev["fold"].nunique()), horizon=horizon, forecast=fc, backtest=bt)


# ---- classification features ------------------------------------------------------------------------------
def creation_features(df):
    """Features known at ticket creation. Counts use only strictly earlier tickets."""
    d = df.sort_values("created_at").reset_index(drop=True).copy()
    t = d["created_at"].astype("int64").values // 10**9
    prior = np.zeros(len(d))
    for _, idx in d.groupby("customer_id", dropna=True).indices.items():
        ts = t[idx]
        prior[idx] = np.arange(len(idx)) - np.searchsorted(ts, ts - 30 * 86400, side="left")
    spike = np.ones(len(d))
    for _, idx in d.groupby("intent").indices.items():
        ts = t[idx]
        last24 = np.arange(len(idx)) - np.searchsorted(ts, ts - 86400, side="left")
        prev14 = np.searchsorted(ts, ts - 86400, side="left") - np.searchsorted(ts, ts - 15 * 86400, side="left")
        spike[idx] = (last24 + 1) / (prev14 / 14 + 1)
    local = d["created_at"].dt.tz_convert("Asia/Kolkata")
    d["prior_30d"], d["intent_spike"] = prior, spike
    d["hour"], d["dow"] = local.dt.hour, local.dt.dayofweek
    d["msg_len"] = d["first_message"].str.len().fillna(0)
    d["emotion"] = d["emotion"].fillna("calm")
    d["escalated_f"] = d["escalated"].astype(float)
    d["sla_breached_f"] = d["sla_breached"].astype(float)
    d["log_resolution_min"] = np.log1p(d["resolution_min"].clip(lower=0))
    d["log_first_response_min"] = np.log1p(d["first_response_min"].clip(lower=0).fillna(0))
    return d


def temporal_split(d, train=0.7, val=0.15):
    d = d.sort_values("created_at")
    n = len(d)
    a, b = int(n * train), int(n * (train + val))
    return d.iloc[:a], d.iloc[a:b], d.iloc[b:]


def _pipe(model, num):
    pre = ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), CAT),
                             ("num", StandardScaler(), num)])
    return Pipeline([("pre", pre), ("model", model)])


def _metrics(y, p):
    y, p = np.asarray(y), np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    k = max(1, int(len(y) * 0.1))
    top = np.argsort(-p)[:k]
    prev = y.mean()
    return dict(roc_auc=round(float(roc_auc_score(y, p)), 3) if len(set(y)) > 1 else None,
                pr_auc=round(float(average_precision_score(y, p)), 3), brier=round(float(brier_score_loss(y, p)), 4),
                log_loss=round(float(log_loss(y, p, labels=[0, 1])), 4), precision_top10=round(float(y[top].mean()), 3),
                lift_top10=round(float(y[top].mean() / prev), 2) if prev else None)


def _intent_rate(train, target, m=20):
    prior = train[target].mean()
    g = train.groupby("intent")[target].agg(["sum", "count"])
    rates = (g["sum"] + m * prior) / (g["count"] + m)  # smoothed
    return lambda frame: frame["intent"].map(rates).fillna(prior).values


def _bootstrap_auc_gain(y, p_model, p_base, iters=300, seed=0):
    rng = np.random.default_rng(seed)
    y, a, b = np.asarray(y), np.asarray(p_model), np.asarray(p_base)
    gains = []
    for _ in range(iters):
        i = rng.integers(0, len(y), len(y))
        if len(set(y[i])) < 2:
            continue
        gains.append(roc_auc_score(y[i], a[i]) - roc_auc_score(y[i], b[i]))
    lo, hi = np.percentile(gains, [2.5, 97.5])
    return round(float(np.mean(gains)), 3), round(float(lo), 3), round(float(hi), 3)


def train_classifier(d, target, num, name):
    """Fit candidates on train, choose on validation, report everything on the untouched test window."""
    d = d.dropna(subset=num).copy()
    d[target] = d[target].astype(int)
    tr, va, te = temporal_split(d)
    if min(len(tr), len(va), len(te)) < 50 or tr[target].nunique() < 2:
        return dict(name=name, status="insufficient data", rows=len(d))
    candidates = {
        "logistic_regression": _pipe(LogisticRegression(max_iter=3000, C=0.5), num),
        "gradient_boosting": _pipe(HistGradientBoostingClassifier(max_iter=200, learning_rate=0.06, max_leaf_nodes=15,
                                                                  l2_regularization=1.0, random_state=0), num),
    }
    val_auc = {}
    for k, m in candidates.items():
        m.fit(tr[CAT + num], tr[target])
        val_auc[k] = roc_auc_score(va[target], m.predict_proba(va[CAT + num])[:, 1])
    # parsimony: keep the interpretable logistic model unless another is clearly better on validation
    chosen = max(val_auc, key=val_auc.get)
    if val_auc[chosen] - val_auc["logistic_regression"] < 0.005:
        chosen = "logistic_regression"
    best = candidates[chosen]
    rate = _intent_rate(tr, target)
    y = te[target].values
    p_model = best.predict_proba(te[CAT + num])[:, 1]
    p_rate = rate(te)
    results = {"prevalence baseline": _metrics(y, np.full(len(y), tr[target].mean())),
               "per-intent rate baseline": _metrics(y, p_rate)}
    for k, m in candidates.items():
        results[k] = _metrics(y, m.predict_proba(te[CAT + num])[:, 1])
    gain = _bootstrap_auc_gain(y, p_model, p_rate)
    explain = []
    if chosen == "logistic_regression":
        names = best.named_steps["pre"].get_feature_names_out()
        coefs = best.named_steps["model"].coef_[0]
        order = np.argsort(-np.abs(coefs))[:10]
        explain = [dict(feature=names[i].replace("cat__", "").replace("num__", ""), weight=round(float(coefs[i]), 3)) for i in order]
    return dict(
        name=name, status="ok", target=target, chosen_model=chosen,
        validation_auc={k: round(float(v), 3) for k, v in val_auc.items()},
        split=dict(train=[str(tr["created_at"].min()), str(tr["created_at"].max())],
                   validation=[str(va["created_at"].min()), str(va["created_at"].max())],
                   test=[str(te["created_at"].min()), str(te["created_at"].max())],
                   rows=dict(train=len(tr), validation=len(va), test=len(te)),
                   positive_rate=dict(train=round(float(tr[target].mean()), 3), test=round(float(y.mean()), 3))),
        test_metrics=results, auc_gain_vs_intent_rate=dict(mean=gain[0], ci95=[gain[1], gain[2]]),
        beats_baseline=bool(gain[1] > 0), top_features=explain, model=best, features=CAT + num)


def escalation_model(feat):
    return train_classifier(feat, "escalated", NUM, "Escalation risk at ticket creation")


def low_csat_model(feat):
    rated = feat[feat["csat"].notna() & feat["resolved"]].copy()
    rated["low_csat"] = rated["csat"] <= 2
    return train_classifier(rated, "low_csat", CSAT_NUM, "Low-CSAT risk at resolution")


def public_report(rep):
    """JSON-safe model report (no fitted objects)."""
    return {k: v for k, v in rep.items() if k not in ("model", "backtest")}
