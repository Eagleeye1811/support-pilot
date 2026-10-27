"""Descriptive analytics: what happened — volumes, complaints, sentiment, resolution, escalation, speed, CSAT, social."""
from __future__ import annotations

import pandas as pd


def filter_tickets(df, start=None, end=None, channels=None, categories=None, sources=None):
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    if start is not None:
        m &= df["date"] >= start
    if end is not None:
        m &= df["date"] <= end
    if channels:
        m &= df["channel"].isin(channels)
    if categories:
        m &= df["category"].isin(categories)
    if sources:
        m &= df["source"].isin(sources)
    return df[m]


def kpis(df):
    if df.empty:
        return dict(tickets=0)
    rated = df["csat"].dropna()
    res = df[df["resolved"]]
    days = max((df["date"].max() - df["date"].min()).days + 1, 1)
    return dict(
        tickets=int(len(df)), per_day=round(len(df) / days, 1),
        resolution_rate=round(df["resolved"].mean(), 3), escalation_rate=round(df["escalated"].mean(), 3),
        automation_rate=round((df["resolved"] & ~df["escalated"]).mean(), 3),
        median_first_response_min=round(df["first_response_min"].median(), 1) if df["first_response_min"].notna().any() else None,
        median_resolution_min=round(res["resolution_min"].median(), 1) if len(res) else None,
        median_human_resolution_h=round(res[res["escalated"]]["resolution_min"].median() / 60, 1) if res["escalated"].any() else None,
        sla_breach_rate=round(res["sla_breached"].mean(), 3) if len(res) else None,
        csat_avg=round(rated.mean(), 2) if len(rated) else None, csat_pct=round((rated >= 4).mean(), 3) if len(rated) else None,
        low_csat_share=round((rated <= 2).mean(), 3) if len(rated) else None, csat_responses=int(len(rated)),
        negative_share=round((df["sentiment_score"] <= -0.15).mean(), 3),
    )


def daily(df, by=None):
    if df.empty:
        return pd.DataFrame(columns=["date", "tickets"])
    keys = ["date"] + ([by] if by else [])
    out = df.groupby(keys).agg(tickets=("id", "size"), escalated=("escalated", "mean"), csat=("csat", "mean"),
                               negative=("sentiment_score", lambda s: (s <= -0.15).mean())).reset_index()
    return out


def weekly_kpis(df):
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["week"] = pd.to_datetime(d["date"]).dt.to_period("W-SUN").dt.start_time.dt.date
    g = d.groupby("week")
    res = d[d["resolved"]].groupby("week")
    out = pd.DataFrame(dict(tickets=g.size(), escalation_rate=g["escalated"].mean(), csat=g["csat"].mean(),
                            median_first_response_min=g["first_response_min"].median(),
                            sla_breach_rate=res["sla_breached"].mean(),
                            median_resolution_h=res["resolution_min"].median() / 60)).reset_index()
    return out


def top_complaints(df, current_days=14, ref=None):
    """Intents by volume in the current window vs the previous window of the same length."""
    if df.empty:
        return pd.DataFrame()
    ref = ref or df["date"].max()
    cur = df[(df["date"] > ref - pd.Timedelta(days=current_days)) & (df["date"] <= ref)]
    prev = df[(df["date"] > ref - pd.Timedelta(days=2 * current_days)) & (df["date"] <= ref - pd.Timedelta(days=current_days))]
    a, b = cur["intent"].value_counts(), prev["intent"].value_counts()
    out = pd.DataFrame(dict(current=a, previous=b)).fillna(0).astype(int)
    out["change"] = out["current"] - out["previous"]
    out["change_pct"] = (out["change"] / out["previous"].where(out["previous"] > 0)).round(3)
    out["negative_share"] = cur.groupby("intent")["sentiment_score"].apply(lambda s: (s <= -0.15).mean()).round(3)
    out["csat"] = cur.groupby("intent")["csat"].mean().round(2)
    return out.sort_values("current", ascending=False).reset_index(names="intent")


def social_daily(sd, by="topic"):
    if sd.empty:
        return pd.DataFrame(columns=["date", by, "posts", "complaints"])
    return sd.groupby(["date", by]).agg(posts=("id", "size"), complaints=("complaint", "sum"),
                                        sentiment=("sentiment_score", "mean")).reset_index()


def social_kpis(sd):
    if sd.empty:
        return dict(posts=0)
    return dict(posts=int(len(sd)), complaint_share=round(sd["complaint"].mean(), 3),
                negative_share=round((sd["sentiment"] == "negative").mean(), 3),
                positive_share=round((sd["sentiment"] == "positive").mean(), 3),
                engagement=int(sd["likes"].sum() + sd["reposts"].sum()),
                sources=sd["source"].value_counts().to_dict())
