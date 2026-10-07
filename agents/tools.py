"""MCP tool registry — every agent exposed as a JSON-in / JSON-out tool.

The orchestrator calls these through a real MCP client session (see ``mcp_bus.py``),
and ``mcp_server.py`` publishes the very same tools to Claude Desktop / any MCP client.
Each tool takes the store as its first argument; it is bound when the server is built.
State that must survive between tools (the conversation, the active issue) is passed in
and returned explicitly, because MCP arguments and results are serialized copies.
"""

import inspect
import json
from typing import Any, Optional

from . import (action_agent, channel_agent, context_agent, escalation_agent, intent_agent, knowledge_agent, memory_agent,
               response_agent, satisfaction_agent, ticket_agent, tracking_agent, troubleshooting_agent)

Obj = Optional[dict[str, Any]]


def jsonable(value):
    """Round-trip through JSON so direct calls see exactly what an MCP client would."""
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


# ---- the tools -------------------------------------------------------------------------------
def normalize_channel(store, channel: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Omnichannel adapter: turn a native Web/WhatsApp/Email/Telegram/App payload into one standard message."""
    return channel_agent.normalize_inbound(channel, payload)


def understand_message(store, text: str, use_llm: bool = False) -> dict[str, Any]:
    """Conversation Understanding: intent, category, priority, sentiment/emotion and entities of a message."""
    return intent_agent.understand(text, use_llm)


def track_conversation(store, conv: dict[str, Any], understanding: dict[str, Any], text: str,
                       force_new: bool = False) -> dict[str, Any]:
    """Conversation Memory: continue the active issue, fill a missing slot, or open a new issue.

    Returns the updated conversation; the issue is ``conv["active_issue"]`` when ``has_issue`` is true.
    """
    if force_new:
        issue, mode = memory_agent.open_issue(conv, understanding, text), "new"
    else:
        issue, mode = memory_agent.track_issue(conv, understanding, text)
    return dict(conv=conv, mode=mode, has_issue=issue is not None)


def customer_context(store, customer_id: Optional[str] = None, entities: Obj = None, sender: Obj = None) -> dict[str, Any]:
    """Customer Context: identify the customer (session, email, phone, order or ticket id) and load their account."""
    cid, method = context_agent.resolve_customer_id(store, customer_id, entities or {}, sender or {})
    return dict(customer_id=cid, method=method, context=context_agent.customer_context(store, cid) if cid else None)


def search_knowledge(store, query: str, intent: Optional[str] = None, category: Optional[str] = None,
                     top_k: int = 4) -> dict[str, Any]:
    """Knowledge Retrieval: BM25 over FAQs, policies and guides, plus the most reliable workflow for the intent."""
    articles = knowledge_agent.search(query, intent, category, top_k, store)
    return dict(articles=articles, workflow=knowledge_agent.select_workflow(intent, articles),
                kb_confidence=knowledge_agent.kb_confidence(articles, intent))


def troubleshoot(store, intent: str, context: Obj = None, slots: Obj = None, articles: Optional[list[dict[str, Any]]] = None,
                 kb_confidence: Optional[float] = None, attachments: Optional[list[str]] = None) -> dict[str, Any]:
    """Troubleshooting: run the diagnostic workflow for the intent against live account, order and network data."""
    slots = dict(slots or {})
    res = troubleshooting_agent.investigate(store, intent, context, slots, articles, kb_conf=kb_confidence,
                                            attachments=attachments)
    return dict(res, slots=slots)  # the workflow may infer ids (e.g. the latest order) into the slots


def decide_escalation(store, understanding: dict[str, Any], investigation: dict[str, Any], context: Obj = None,
                      issue: Obj = None, actions: Optional[list[dict[str, Any]]] = None, human_requested: bool = False,
                      clarification_count: int = 0) -> dict[str, Any]:
    """Escalation Decision: auto-resolve, ask for details, or escalate — with every rule's outcome and learned thresholds."""
    return escalation_agent.decide(understanding, investigation, context, issue, actions,
                                   satisfaction_agent.learned_thresholds(store), human_requested, clarification_count)


def execute_actions(store, actions: list[dict[str, Any]], context: Obj = None,
                    conversation_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Action Execution: run guarded actions (refund, replacement, unlock, sync retry, technician visit, ...)."""
    done = []
    for a in actions:
        done += action_agent.execute(store, a["action"], a.get("params") or {}, context, conversation_id=conversation_id)
    return done


def create_ticket(store, conv: dict[str, Any], issue: dict[str, Any], understanding: dict[str, Any], context: Obj,
                  investigation: dict[str, Any], actions: list[dict[str, Any]], decision: dict[str, Any], summary: str,
                  articles: Optional[list[dict[str, Any]]] = None, attachments: Optional[list[str]] = None) -> dict[str, Any]:
    """Ticket Generation: a complete ticket (context, investigation, actions, escalation reasons, next step, SLA)."""
    t = ticket_agent.create_or_update(store, conv, issue, understanding, context, investigation, actions, decision, summary, articles)
    if attachments:
        t["attachments"] = list(dict.fromkeys((t.get("attachments") or []) + attachments))
        store.put("tickets", t["id"], t)
    for a in actions:
        a["ticket_id"] = t["id"]
        store.put("actions", a["id"], a)
    return t


def track_resolution(store, ticket_id: str, decision: str, actions: list[dict[str, Any]], context: Obj = None,
                     channel: str = "web", conversation_id: Optional[str] = None) -> dict[str, Any]:
    """Resolution Tracking: notify the customer of the outcome and start the SLA clock."""
    ticket = store.get("tickets", ticket_id)
    if decision == "auto_resolve" and context:
        msgs = [a["customer_message"] for a in actions if a["status"] == "success" and a.get("customer_message")]
        if msgs:
            action_agent.execute(store, "notify_customer", dict(customer_id=context["customer_id"], channel=channel,
                                 ticket_id=ticket_id, message=f"{ticket_id} resolved: " + " ".join(msgs), kind="resolution"),
                                 context, conversation_id=conversation_id)
    return dict(ticket_id=ticket_id, status=ticket["status"], sla=tracking_agent.sla_status(ticket))


def ticket_status(store, customer_id: str, ticket_id: Optional[str] = None) -> dict[str, Any]:
    """Resolution Tracking: current status of a customer's ticket, phrased for the customer."""
    t, msg = tracking_agent.status_reply(store, customer_id, ticket_id)
    return dict(ticket=t, message=msg)


def compose_reply(store, decision: dict[str, Any], understanding: dict[str, Any], investigation: dict[str, Any],
                  actions: list[dict[str, Any]], context: Obj = None, ticket: Obj = None, issue: Obj = None,
                  articles: Optional[list[dict[str, Any]]] = None, channel: str = "web") -> dict[str, Any]:
    """Response Composer: a sentiment-aware reply grounded only in executed facts."""
    return dict(reply=response_agent.compose(decision, understanding, investigation, actions, context, ticket, issue,
                                             articles, channel))


def polish_reply(store, reply: str, emotion: str = "calm") -> dict[str, Any]:
    """Optional LLM polish; rejected unless every id, amount and date survives unchanged."""
    text, polished = response_agent.polish(reply, emotion)
    return dict(reply=text, polished=polished)


def review_reply(store, reply: str, decision: Optional[str], actions: list[dict[str, Any]], ticket: Obj = None,
                 intent: Optional[str] = None, context: Obj = None) -> dict[str, Any]:
    """Reviewer guardrail: verify the reply before it reaches the customer (claims, limits, escalations, PII)."""
    from .orchestrator_agent import review
    return review(reply, decision, actions, ticket, intent, context)


def record_csat(store, ticket_id: str, rating: int, comment: str = "", conversation_id: Optional[str] = None) -> dict[str, Any]:
    """Customer Satisfaction: record a 1–5 rating; it tunes future escalation thresholds."""
    rec = satisfaction_agent.record_csat(store, ticket_id, rating, comment, conversation_id)
    t = store.get("tickets", ticket_id)
    if t and t["status"] == "Resolved":
        tracking_agent.transition(store, t, "Closed", actor="Customer", note="Customer confirmed via CSAT", notify=False)
    return rec


# name → (function, agent label shown in the UI)
TOOLS = {
    "normalize_channel": (normalize_channel, "Omnichannel Adapter"),
    "understand_message": (understand_message, "Conversation Understanding"),
    "track_conversation": (track_conversation, "Conversation Memory"),
    "customer_context": (customer_context, "Customer Context"),
    "search_knowledge": (search_knowledge, "Knowledge Retrieval"),
    "troubleshoot": (troubleshoot, "Troubleshooting"),
    "decide_escalation": (decide_escalation, "Escalation Decision"),
    "execute_actions": (execute_actions, "Action Execution"),
    "create_ticket": (create_ticket, "Ticket Generation"),
    "track_resolution": (track_resolution, "Resolution Tracking"),
    "ticket_status": (ticket_status, "Resolution Tracking"),
    "compose_reply": (compose_reply, "Response Composer"),
    "polish_reply": (polish_reply, "Response Composer"),
    "review_reply": (review_reply, "Reviewer"),
    "record_csat": (record_csat, "Customer Satisfaction"),
}


def register(name, fn, label):
    """Add a tool to the registry (used by the media and email agents)."""
    TOOLS[name] = (fn, label)


def bind(fn, store):
    """The tool with its store argument filled in, keeping a clean signature for MCP schema generation."""
    def tool(**kwargs):
        return {"value": jsonable(fn(store, **kwargs))}
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]
    tool.__signature__ = sig.replace(parameters=params, return_annotation=dict[str, Any])
    tool.__name__, tool.__doc__ = fn.__name__, fn.__doc__
    return tool


def call_direct(store, name, args):
    """Run a tool in-process without MCP (fallback and SUPPORTPILOT_MCP=0)."""
    fn, _ = TOOLS[name]
    return jsonable(fn(store, **args))
