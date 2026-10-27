"""Prescriptive analytics: ranked, evidence-backed recommendations — applied only after a human approves.

Each recommendation carries its evidence, an estimated impact (tickets/week or affected customers), a confidence
derived from the evidence level, an effort size and an explicit effect. Ranking score = impact × confidence ÷ effort.
The ranking is a heuristic for prioritisation, stated as such — not a forecast of savings.

Effects (all reversible platform changes; nothing touches customers or company systems directly):
  * ``task``       — an internal work item for a team (``tasks`` collection)
  * ``threshold``  — an escalation-threshold override for one intent in the tenant policy (live support reads it);
                     the previous value is kept so ``revert`` can restore it
Recommendations live in the tenant's support store (they are decisions, so they survive warehouse rebuilds); a
re-run of the pipeline refreshes the evidence of open ones and never resurrects decided ones.
"""
from __future__ import annotations

import datetime
import hashlib

import numpy as np
from scipy.stats import ttest_ind

from agents import audit
from agents.insights_agent import HYPOTHESES
from agents.policy import Policy
from agents.shared_agent import INTENTS, TEAMS, iso

CONFIDENCE = {"strong": 0.85, "moderate": 0.6, "weak": 0.3}
EFFORT = {"S": 1.0, "M": 2.0, "L": 3.0}
AGENT_CAPACITY_PER_DAY = 25  # stated planning assumption: escalated tickets one agent handles per day


def _rid(kind, subject):
    return "REC-" + hashlib.sha1(f"{kind}:{subject}".encode()).hexdigest()[:8].upper()


def _rec(kind, subject, title, rationale, evidence, impact, impact_unit, confidence, effort, effect, owner, caveat=None):
    score = round(impact * confidence / EFFORT[effort], 2)
    return dict(id=_rid(kind, subject), kind=kind, subject=subject, title=title, rationale=rationale, evidence=evidence,
                impact=round(float(impact), 1), impact_unit=impact_unit, confidence=confidence, effort=effort, score=score,
                effect=effect, owner=owner, caveat=caveat or "Prioritisation heuristic based on observational data.")


def generate(R, today):
    """Build recommendations from a pipeline result ``R`` (descriptive, diagnostic, predictive, social)."""
    recs = []
    tickets = R["tickets"]
    # 1. root causes behind recent spikes
    for f in R["diagnostic"]["findings"]:
        start, end = datetime.date.fromisoformat(f["start"]), datetime.date.fromisoformat(f["end"])
        days_ago = (today - end).days
        if days_ago > 45 or f["evidence_level"] == "weak":
            continue
        weeks = ((end - start).days + 1) / 7
        per_week = f["excess"] / weeks
        recurring = days_ago > 3
        hyp = HYPOTHESES.get(f["intent"])
        action = hyp[2].format(value=f.get("event")) if hyp else "Investigate and fix the common cause"
        team = INTENTS.get(f["intent"], {}).get("team", "Tier-1 Support")
        recs.append(_rec(
            "root_cause", f"{f['event_id']}:{f['intent']}",
            (f"Prevent a repeat of “{f['event']}”" if recurring else f"Fix the cause behind “{f['event']}”")
            + f" ({INTENTS.get(f['intent'], {}).get('label', f['intent'])})",
            f"{f['excess']:.0f} extra {f['intent']} tickets ({f['ratio']}× baseline) between {f['start']} and {f['end']}. "
            f"Suggested: {action}.", f["evidence"], per_week * (0.5 if recurring else 1.0), "avoidable tickets/week",
            CONFIDENCE[f["evidence_level"]], "M", dict(type="task", team=team), team, f["caveat"]))
    # 2. emerging issues on social media
    for e in R["social"]["emerging"]:
        recs.append(_rec(
            "emerging", e["topic"], f"Investigate emerging “{e['topic'].replace('_', ' ')}” complaints on social media",
            f"{e['recent_posts']} complaint posts in 3 days vs {e['expected']} expected (×{e['ratio']}); related tickets ×"
            f"{e['ticket_ratio']}. " + ("Not yet visible in support tickets — early warning." if e["early_warning"] else
                                        "Related tickets are also rising (they may share other causes)."),
            [f"Holm-adjusted p={e['p_adj']:.1e}", "Sample: " + " | ".join(e["sample_posts"][:2]),
             f"Sources: {e['sources']}"], e["recent_posts"] / 3 * 7 * 0.3, "estimated tickets/week if unaddressed",
            0.6, "M", dict(type="task", team="Engineering"), "Engineering",
            "Social chatter is a noisy signal; volume can come from a few accounts. Verify with product telemetry."))
    # 3. AI resolutions customers rate poorly → escalate more for that intent
    res = tickets[tickets["csat"].notna()]
    for intent, g in res.groupby("intent"):
        ai, human = g[~g["escalated"]]["csat"], g[g["escalated"]]["csat"]
        if len(ai) < 30 or len(human) < 15:
            continue
        gap = human.mean() - ai.mean()
        p = float(ttest_ind(human, ai, equal_var=False).pvalue)
        if gap >= 0.5 and p < 0.01:
            weekly = len(g[~g["escalated"]]) / max((g["date"].max() - g["date"].min()).days / 7, 1)
            recs.append(_rec(
                "threshold", intent, f"Escalate more “{INTENTS[intent]['label']}” cases to humans",
                f"AI-resolved CSAT {ai.mean():.2f} vs human-resolved {human.mean():.2f} (n={len(ai)}/{len(human)}, Welch p={p:.1e}). "
                "Raise the escalation threshold to 90% for this intent.",
                [f"CSAT gap {gap:.2f} points", f"{weekly:.0f} AI-resolved cases/week"], weekly * gap, "CSAT points × cases/week",
                0.6, "S", dict(type="threshold", intent=intent, threshold=0.9), "Support operations",
                "Human-handled cases may differ in kind (selection); the gap is not purely the resolver's effect. "
                "More escalations add human workload — check the staffing recommendation."))
    # 4. staffing from the forecast
    fc = R["predictive"].get("forecast") or {}
    if fc.get("status") == "ok":
        recent = tickets[tickets["date"] > today - datetime.timedelta(days=28)]
        share = recent[recent["escalated"]]["team"].value_counts() / max(len(recent), 1)
        nxt = sum(d["forecast"] for d in fc["forecast"])
        last28_daily = len(recent) / 28
        for team, sh in share.items():
            if team not in TEAMS:
                continue
            exp_daily = nxt / len(fc["forecast"]) * sh
            base_daily = last28_daily * sh
            need = exp_daily / AGENT_CAPACITY_PER_DAY
            roster = len(TEAMS[team])
            if exp_daily >= 3 and (need > roster or exp_daily > base_daily * 1.15):
                recs.append(_rec(
                    "staffing", team, f"Add capacity to {team} for the next 7 days",
                    f"Forecast {exp_daily:.0f} escalated tickets/day for {team} next week (last 28 days: {base_daily:.0f}/day, "
                    f"{exp_daily / max(base_daily, 0.1) - 1:+.0%}). Roster {roster} at an assumed {AGENT_CAPACITY_PER_DAY}/agent/day "
                    f"→ ~{need:.1f} agents needed.",
                    [f"Forecast method {fc['chosen_method']}, eval MAE "
                     f"{next(e['mae'] for e in fc['evaluation'] if e['method'] == fc['chosen_method'])} tickets/day",
                     f"{team} handled {sh:.0%} of all tickets in the last 28 days"],
                    max(exp_daily - base_daily, (need - roster) * AGENT_CAPACITY_PER_DAY) * 7, "extra tickets/week", 0.5, "S",
                    dict(type="task", team="Escalation Desk"), "Escalation Desk",
                    f"Capacity of {AGENT_CAPACITY_PER_DAY} tickets/agent/day is a planning assumption — replace with your own."))
    # 5. SLA breaches by team
    done = tickets[tickets["resolved"] & tickets["escalated"] & (tickets["date"] > today - datetime.timedelta(days=28))]
    worst = sorted(done.groupby("team"), key=lambda kv: -kv[1]["sla_breached"].sum())[:2]
    for team, g in worst:
        if len(g) >= 30 and g["sla_breached"].mean() >= 0.25:
            lc = R["predictive"].get("low_csat") or {}
            recs.append(_rec(
                "sla", team, f"Reduce SLA breaches in {team}",
                f"{g['sla_breached'].mean():.0%} of {len(g)} escalated tickets resolved in the last 28 days breached the SLA. "
                "SLA breach is one of the strongest low-CSAT predictors.",
                [f"median resolution {g['resolution_min'].median() / 60:.1f} h"] +
                [f"low-CSAT model weight for sla_breached: {f['weight']}" for f in lc.get("top_features", []) if f["feature"] == "sla_breached_f"],
                g["sla_breached"].sum() / 4, "breached tickets/week", 0.6, "M", dict(type="task", team=team), team))
    # 6. live customers likely to be unhappy → proactive follow-up
    at_risk = R.get("live_low_csat_risk") or []
    hot = [r for r in at_risk if r["risk"] >= 0.3]
    if hot:
        recs.append(_rec(
            "follow_up", "live-low-csat", f"Proactively follow up with {len(hot)} recently resolved customer(s)",
            "The low-CSAT model scores these resolved, not-yet-rated live tickets as likely to be rated ≤ 2.",
            [f"{r['ticket_id']} ({r['intent']}): risk {r['risk']:.0%}" for r in hot[:5]], len(hot), "customers", 0.5, "S",
            dict(type="task", team="Customer Success", tickets=[r["ticket_id"] for r in hot]), "Customer Success",
            "Model trained mostly on synthetic history; treat the score as a prompt for a human check."))
    # 7. unexplained anomalies
    for a in [a for a in R["diagnostic"]["anomalies"] if not a["explained_by_event"]][:3]:
        if (today - datetime.date.fromisoformat(a["date"])).days <= 14:
            recs.append(_rec(
                "anomaly", f"{a['category']}:{a['date']}", f"Investigate unexplained {a['category']} spike on {a['date']}",
                f"{a['tickets']} tickets vs {a['expected']} expected (robust z {a['robust_z']}) and no logged event explains it.",
                ["No event in the event log overlaps this day"], a["tickets"] - a["expected"], "excess tickets (one day)", 0.3, "S",
                dict(type="task", team="Tier-1 Support"), "Tier-1 Support"))
    recs.sort(key=lambda r: -r["score"])
    for i, r in enumerate(recs, 1):
        r["rank"] = i
    return recs


def sync(ws, recs):
    """Store fresh recommendations; refresh open ones; never touch decided ones."""
    fresh = {r["id"] for r in recs}
    for r in recs:
        cur = ws.get("recommendations", r["id"])
        if cur and cur["status"] != "proposed":
            continue
        ws.put("recommendations", r["id"], dict(r, status="proposed", created_at=(cur or {}).get("created_at", iso()),
                                                refreshed_at=iso()))
    for cur in ws.list("recommendations", status="proposed"):
        if cur["id"] not in fresh:  # evidence no longer supports it
            cur.update(status="expired", refreshed_at=iso())
            ws.put("recommendations", cur["id"], cur)
    return listing(ws)


def listing(ws, status=None):
    rows = ws.list("recommendations", status=status) if status else ws.list("recommendations")
    order = {"proposed": 0, "approved": 1, "rejected": 2, "reverted": 3, "expired": 4}
    return sorted(rows, key=lambda r: (order.get(r["status"], 9), r.get("rank", 999)))


def _persist_policy(ws, registry):
    if registry is not None and registry.tenant(ws.tenant["id"]):
        registry.update_policy(ws.tenant["id"], ws.policy.to_dict())


def decide(ws, rec_id, approve, approver, note="", registry=None):
    """Human decision. Approval applies the effect; rejection records why. Both are audited."""
    if not approver or not str(approver).strip():
        raise ValueError("An approver name is required")
    rec = ws.get("recommendations", rec_id)
    if rec is None:
        raise KeyError(rec_id)
    if rec["status"] != "proposed":
        raise ValueError(f"{rec_id} is already {rec['status']}")
    rec.update(decided_by=approver, decided_at=iso(), note=note)
    if not approve:
        rec["status"] = "rejected"
    else:
        eff = rec["effect"]
        if eff["type"] == "task":
            seq = ws.next_seq("task", 1)
            task = dict(id=f"TASK-{seq:04d}", title=rec["title"], team=eff["team"], description=rec["rationale"],
                        evidence=rec["evidence"], recommendation_id=rec_id, tickets=eff.get("tickets", []), status="open",
                        created_by=approver, created_at=iso())
            ws.put("tasks", task["id"], task)
            rec["applied"] = dict(task_id=task["id"])
        elif eff["type"] == "threshold":
            prev = ws.policy.threshold_overrides.get(eff["intent"])
            ws.policy.threshold_overrides[eff["intent"]] = dict(
                threshold=eff["threshold"], reason=f"{rec_id} approved by {approver} — escalate more")
            ws.policy = Policy.from_dict(ws.policy.to_dict())
            _persist_policy(ws, registry)
            rec["applied"] = dict(intent=eff["intent"], threshold=eff["threshold"], previous=prev)
        rec["status"] = "approved"
    ws.put("recommendations", rec_id, rec)
    audit.log(ws, f"recommendation.{rec['status']}", f"Human:{approver}", outcome=rec["status"], recommendation_id=rec_id,
              kind=rec["kind"], applied=rec.get("applied"), note=note or None)
    return rec


def revert(ws, rec_id, actor, registry=None):
    """Undo an approved threshold change (tasks are closed by the team instead)."""
    rec = ws.get("recommendations", rec_id)
    if rec is None or rec["status"] != "approved" or rec["effect"]["type"] != "threshold":
        raise ValueError("Only approved threshold recommendations can be reverted")
    intent, prev = rec["applied"]["intent"], rec["applied"].get("previous")
    if prev:
        ws.policy.threshold_overrides[intent] = prev
    else:
        ws.policy.threshold_overrides.pop(intent, None)
    _persist_policy(ws, registry)
    rec.update(status="reverted", reverted_by=actor, reverted_at=iso())
    ws.put("recommendations", rec_id, rec)
    audit.log(ws, "recommendation.reverted", f"Human:{actor}", outcome="reverted", recommendation_id=rec_id, intent=intent)
    return rec


def summary_numbers(recs):
    return dict(total=len(recs), proposed=sum(r["status"] == "proposed" for r in recs),
                top_score=float(np.max([r["score"] for r in recs])) if recs else None)
