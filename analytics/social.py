"""Social media analytics: topics, sentiment, complaint trends, emerging issues and their link to tickets.

Emerging issue = a topic whose complaint posts in the last ``window_days`` far exceed its weekday-adjusted baseline
(Poisson tail test, Holm-corrected across topics). Each one is compared with the related ticket intents: an issue
that is loud on social but not yet in tickets is an early warning. Lead/lag is the lag (−3…+3 days) at which daily
complaint posts best correlate with related tickets — again association, not causation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import poisson

from .diagnostic import holm
from .ingest import TOPIC_INTENTS


def _daily(frame, today):
    s = frame.groupby("date").size()
    s.index = pd.to_datetime(s.index)
    s = s[s.index <= pd.Timestamp(today)]
    if s.empty:
        return s.astype(float)
    return s.reindex(pd.date_range(s.index.min(), pd.Timestamp(today)), fill_value=0).astype(float)


def emerging_issues(sd, tickets, today, window_days=3, baseline_days=28, min_posts=10, min_ratio=3.0, alpha=0.001):
    if sd.empty:
        return []
    comp = sd[sd["complaint"]]
    rows = []
    for topic, g in comp.groupby("topic"):
        if topic == "other":
            continue
        s = _daily(g, today)
        recent = s[s.index > pd.Timestamp(today) - pd.Timedelta(days=window_days)]
        base = s[(s.index <= pd.Timestamp(today) - pd.Timedelta(days=window_days)) &
                 (s.index > pd.Timestamp(today) - pd.Timedelta(days=window_days + baseline_days))]
        if len(base) < 7:
            continue
        expected = max(base.mean() * len(recent), 0.5)
        obs = int(recent.sum())
        rows.append(dict(topic=topic, recent_posts=obs, expected=round(float(expected), 1), ratio=round(float(obs / expected), 2),
                         p=float(poisson.sf(obs - 1, expected)), first_day=str(recent[recent > 0].index.min().date()) if obs else None))
    if not rows:
        return []
    for r, padj in zip(rows, holm([r["p"] for r in rows])):
        r["p_adj"] = float(padj)
    out = []
    for r in rows:
        if not (r["p_adj"] < alpha and r["ratio"] >= min_ratio and r["recent_posts"] >= min_posts):
            continue
        intents = TOPIC_INTENTS.get(r["topic"], [])
        t_ratio = None
        if intents and not tickets.empty:
            tk = _daily(tickets[tickets["intent"].isin(intents)], today)
            t_recent = tk[tk.index > pd.Timestamp(today) - pd.Timedelta(days=window_days)].sum()
            t_base = tk[(tk.index <= pd.Timestamp(today) - pd.Timedelta(days=window_days)) &
                        (tk.index > pd.Timestamp(today) - pd.Timedelta(days=window_days + baseline_days))].mean() * window_days
            t_ratio = round(float(t_recent / max(t_base, 0.5)), 2)
        samples = comp[(comp["topic"] == r["topic"]) &
                       (pd.to_datetime(comp["date"]) > pd.Timestamp(today) - pd.Timedelta(days=window_days))]
        out.append(dict(r, related_intents=intents, ticket_ratio=t_ratio,
                        early_warning=t_ratio is not None and t_ratio < 1.5,
                        sample_posts=samples.sort_values("likes", ascending=False)["text"].head(3).tolist(),
                        sources=samples["source"].value_counts().to_dict()))
    return sorted(out, key=lambda r: -r["ratio"])


def lead_lag(sd, tickets, today, max_lag=3):
    """Lag (days) at which complaint posts best correlate with related tickets; positive = social leads."""
    out = []
    if sd.empty or tickets.empty:
        return out
    comp = sd[sd["complaint"]]
    for topic, intents in TOPIC_INTENTS.items():
        if not intents:
            continue
        x = _daily(comp[comp["topic"] == topic], today)
        y = _daily(tickets[tickets["intent"].isin(intents)], today)
        idx = x.index.intersection(y.index)
        if len(idx) < 30:
            continue
        x, y = x.reindex(idx), y.reindex(idx)
        best = None
        for lag in range(-max_lag, max_lag + 1):
            r = x.shift(lag).corr(y)
            if not np.isnan(r) and (best is None or r > best[1]):
                best = (lag, r)
        if best:
            out.append(dict(topic=topic, related_intents=intents, best_lag_days=best[0], pearson_r=round(float(best[1]), 3),
                            reading=("social complaints lead tickets" if best[0] > 0 else
                                     "tickets lead social complaints" if best[0] < 0 else "same day")))
    return sorted(out, key=lambda r: -r["pearson_r"])
