"""Action authorization — deterministic rules decide what may happen; the LLM never does.

Every action passes ``authorize`` before the company adapter is called. Outcomes:
  * ``allow``     — run it now
  * ``approval``  — do not run it; queue it for a human to approve (``agents/approvals.py``)
  * ``deny``      — refuse (status ``blocked``)

Rules, in order: allow-list → supported by the company adapter → tenant switch (``disabled``) → an AI action
needs an identified customer → the customer owns every referenced order / payment / subscription / line →
AI refund limit → tenant ``approval`` mode → sensitive actions requested directly by an external AI client
(actor ``AI:<client>``, e.g. over MCP) need approval. Humans (actor other than "AI") skip the limit and approval rules
but never the ownership and allow-list checks.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .shared_agent import AUTO_REFUND_LIMIT, inr

MODES = ("auto", "approval", "disabled")
# Actions that change money, orders or subscriptions. When they were planned from an intent only the LLM saw
# (the rules disagreed or found nothing), the customer must confirm before they run.
SENSITIVE_ACTIONS = {"initiate_refund", "cancel_order", "create_replacement", "generate_return_label", "cancel_subscription",
                     "upgrade_plan", "reactivate_subscription", "reconcile_payment", "schedule_technician_visit"}
OWNED_PARAMS = {"order_id": "orders", "payment_id": "payments", "subscription_id": "subscriptions",
                "service_id": "service_lines"}
CUSTOMER_FREE = {"notify_customer"}


@dataclass
class Policy:
    auto_refund_limit: float = AUTO_REFUND_LIMIT
    action_modes: dict = field(default_factory=dict)  # action → auto | approval | disabled (default auto)
    confirm_llm_only_actions: bool = True
    # intent → {threshold, reason}: escalation thresholds set by an approved analytics recommendation
    threshold_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        modes = {k: v for k, v in (data.get("action_modes") or {}).items() if v in MODES}
        overrides = {k: dict(threshold=min(max(float(v["threshold"]), 0.5), 0.99), reason=str(v.get("reason", "")))
                     for k, v in (data.get("threshold_overrides") or {}).items() if isinstance(v, dict) and "threshold" in v}
        return cls(auto_refund_limit=float(data.get("auto_refund_limit", AUTO_REFUND_LIMIT)), action_modes=modes,
                   confirm_llm_only_actions=bool(data.get("confirm_llm_only_actions", True)), threshold_overrides=overrides)

    def to_dict(self):
        return asdict(self)

    def mode(self, action):
        return self.action_modes.get(action, "auto")


def is_ai(actor):
    """'AI' is SupportPilot's own workflow; 'AI:<client>' is an external AI agent (e.g. an MCP client)."""
    return actor == "AI" or str(actor).startswith("AI:")


def authorize(ws, action, params, ctx, actor="AI"):
    """Decide whether ``action`` may run. Returns dict(outcome, reason, rule)."""
    from .action_agent import ACTIONS
    policy = ws.policy

    def result(outcome, rule, reason=""):
        return dict(outcome=outcome, rule=rule, reason=reason)

    if action not in ACTIONS:
        return result("deny", "allow-list", f"Action '{action}' is not on the allow-list")
    if action not in CUSTOMER_FREE and action not in ws.adapter.supported_actions():
        return result("deny", "adapter", f"{ws.adapter.name} does not support '{action}'")
    if policy.mode(action) == "disabled":
        return result("deny", "tenant policy", f"'{action}' is disabled for {ws.tenant['name']}")
    if action in CUSTOMER_FREE:
        return result("allow", "notification")
    if is_ai(actor) and not ctx:
        return result("deny", "identity", "The customer is not identified — AI actions need a verified customer")
    cid = (ctx or {}).get("customer_id")
    if cid and params.get("customer_id") and params["customer_id"] != cid:
        return result("deny", "ownership", "Action targets a different customer")
    for key, entity in OWNED_PARAMS.items():
        if params.get(key):
            rec = ws.get(entity, params[key])
            if rec is None:
                return result("deny", "ownership", f"{params[key]} not found")
            if cid and rec.get("customer_id") != cid:
                return result("deny", "ownership", f"{params[key]} does not belong to this customer")
    if is_ai(actor):
        if action == "initiate_refund":
            amount = float(params.get("amount") or (ws.get("orders", params.get("order_id")) or {}).get("amount") or 0)
            if amount > policy.auto_refund_limit:
                return result("approval", "refund limit",
                              f"Refund {inr(amount)} exceeds the AI limit of {inr(policy.auto_refund_limit)}")
        if policy.mode(action) == "approval":
            return result("approval", "tenant policy", f"{ws.tenant['name']} requires human approval for '{action}'")
        if actor != "AI" and action in SENSITIVE_ACTIONS:
            return result("approval", "external AI", f"'{action}' was requested directly by an external AI client ({actor}); "
                                                     "a human must approve it")
    return result("allow", "policy")
