"""Agent 7: Escalation Decision — genuine decision-making, not "please contact support".

Combines intent confidence and investigation confidence into one score and
evaluates explicit rules, every one of which is reported for transparency:

  * ALWAYS escalate: fraud, data breach, legal requests
  * customer explicitly asks for a human
  * required information still missing → clarify (escalate after 2 failed attempts)
  * investigation says not auto-resolvable (high-value, policy exception, security hold)
  * any executed action failed or was blocked by a guardrail
  * confidence below the threshold (70% base, learned per intent from CSAT)
  * sentiment-aware: angry customers get a stricter threshold and repeat
    contacts escalate faster
"""
from __future__ import annotations

from .shared_agent import ALWAYS_ESCALATE, BASE_CONFIDENCE_THRESHOLD, INTENTS, bump_priority

AUTO_RESOLVE_EXAMPLES = ["password reset", "order tracking", "refund status", "return label", "plan upgrade"]


def combined_confidence(understanding, investigation, actions=None):
    conf = 0.35 * understanding.get("confidence", 0.5) + 0.65 * investigation.get("confidence", 0.5)
    if actions and any(a["status"] in ("failed", "blocked") and not a.get("fallback_recovered") for a in actions):
        conf *= 0.5
    return round(conf, 2)


def decide(understanding, investigation, ctx, issue, actions=None, thresholds=None, human_requested=False,
           clarification_count=0):
    intent = issue["intent"] if issue else understanding["intent"]
    spec = INTENTS.get(intent, INTENTS["general_inquiry"])
    emotion = understanding.get("emotion", "calm")
    conf = combined_confidence(understanding, investigation, actions)
    learned = (thresholds or {}).get(intent, {})
    threshold = learned.get("threshold", BASE_CONFIDENCE_THRESHOLD)
    if emotion == "angry":
        threshold = min(0.95, threshold + 0.1)
    repeat = 0
    if ctx:
        repeat = ctx["previous_tickets"]["intents"].count(intent)
    turns = issue.get("turns", 1) if issue else 1
    failed = [a for a in (actions or []) if a["status"] in ("failed", "blocked") and not a.get("fallback_recovered")]

    rules = [
        ("Mandatory escalation (fraud / breach / legal)", intent in ALWAYS_ESCALATE, spec["label"]),
        ("Customer asked for a human", bool(human_requested), "explicit request"),
        ("Required details missing", bool(investigation.get("missing_slots")), ", ".join(investigation.get("missing_slots", []))),
        ("Investigation not auto-resolvable", not investigation.get("resolvable", True), investigation.get("diagnosis", "")),
        ("Action failed / blocked by guardrail", bool(failed), "; ".join(a["message"] for a in failed)),
        (f"Confidence below threshold ({threshold:.0%})", conf < threshold and not investigation.get("missing_slots"),
         f"confidence {conf:.0%}"
         + (f"; threshold learned from {learned['reason']}" if learned.get("reason", "default") != "default" and "escalate more" in learned.get("reason", "") else "")),
        ("Angry + repeat contact", emotion == "angry" and (repeat >= 2 or turns >= 4),
         f"{repeat} previous '{intent}' tickets, {turns} turns"),
    ]
    rule_hits = [dict(rule=r, triggered=bool(t), detail=d) for r, t, d in rules]
    triggered = [r for r in rule_hits if r["triggered"]]
    reasons = [f"{r['rule']}: {r['detail']}" if r["detail"] else r["rule"] for r in triggered]

    priority = issue.get("priority", understanding.get("priority", spec["priority"])) if issue else understanding.get("priority")
    team = investigation.get("team") or spec["team"]
    if intent in ALWAYS_ESCALATE or human_requested:
        decision = "escalate"
    elif investigation.get("missing_slots"):
        decision = "escalate" if clarification_count >= 2 else "clarify"
        if decision == "escalate":
            reasons.append("Could not collect required details after 2 attempts")
            team = "Tier-1 Support"
    elif triggered:
        decision = "escalate"
    else:
        decision = "auto_resolve"

    if decision == "escalate":
        if ctx and ctx["tier"] == "Premium":
            priority = bump_priority(priority, cap="critical" if priority == "critical" else "high")
        if emotion == "angry":
            priority = bump_priority(priority, cap="critical")
        if human_requested and intent not in ALWAYS_ESCALATE and team == spec["team"] and not investigation.get("diagnosis"):
            team = "Tier-1 Support"
    return dict(decision=decision, confidence=conf, threshold=threshold, threshold_reason=learned.get("reason", "default"),
                reasons=reasons, rule_hits=rule_hits, team=team if decision == "escalate" else "AI Agent",
                target_team=team, priority=priority, sentiment_adjusted=emotion == "angry")
