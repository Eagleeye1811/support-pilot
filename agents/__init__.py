"""Public agent API for SupportPilot.

Each role has a dedicated module (``*_agent.py``); this package exposes a
stable import surface (``import agents as A``) for the Streamlit UI, the MCP
server, evaluation and tests.

| #  | Module                    | Role                                   |
|----|---------------------------|----------------------------------------|
| 1  | intent_agent              | Conversation Understanding             |
| 2  | context_agent             | Customer Context                       |
| 3  | knowledge_agent           | Knowledge Retrieval (+ KB expansion)   |
| 4  | troubleshooting_agent     | Troubleshooting                        |
| 5  | action_agent              | Action Execution                       |
| 6  | memory_agent              | Multi-Turn Conversation Memory         |
| 7  | escalation_agent          | Escalation Decision                    |
| 8  | ticket_agent              | Ticket Generation                      |
| 9  | tracking_agent            | Resolution Tracking + SLA monitoring   |
| 10 | satisfaction_agent        | Customer Satisfaction (feedback loop)  |
| 11 | orchestrator_agent        | Master Orchestrator                    |
| +  | insights_agent            | Root Cause Discovery                   |
| +  | channel_agent             | Omnichannel adapter                    |
| +  | response_agent            | Sentiment-aware response composition   |
"""

from . import (action_agent, channel_agent, context_agent, escalation_agent, insights_agent, intent_agent, knowledge_agent,
               memory_agent, orchestrator_agent, response_agent, satisfaction_agent, ticket_agent, tracking_agent,
               troubleshooting_agent)
from .action_agent import ACTIONS, catalog as action_catalog, execute as execute_action
from .channel_agent import SAMPLE_PAYLOADS, format_outbound, normalize_inbound
from .context_agent import compact as compact_context, customer_context, resolve_customer_id
from .escalation_agent import decide as decide_escalation
from .insights_agent import daily_volume, detect_incidents
from .intent_agent import analyze_sentiment, extract_entities, understand
from .knowledge_agent import expand_kb, load_kb, search as search_kb, select_workflow
from .orchestrator_agent import handle_message, review, run_conversation, summarize_turn
from .satisfaction_agent import csat_summary, learned_thresholds, record_csat
from .shared_agent import (ALWAYS_ESCALATE, AUTO_REFUND_LIMIT, CHANNELS, COMPANY, INTENTS, PRODUCT, TEAMS, fmt_ts,
                           get_groq_api_key, inr, llm_enabled)
from .store import Store, get_store
from .ticket_agent import render_markdown as render_ticket
from .tracking_agent import OPEN_STATUSES, STATUS_FLOW, TRANSITIONS, sla_scan, sla_status, supervisor_metrics, transition
from .troubleshooting_agent import investigate

AGENTS = [
    ("1", "Conversation Understanding", "intent_agent", "Intent, category, priority, sentiment/emotion, entities"),
    ("2", "Customer Context", "context_agent", "Account, purchases, previous tickets, subscription, transactions"),
    ("3", "Knowledge Retrieval", "knowledge_agent", "BM25 over FAQs/policies/docs/guides, reliability-weighted workflow choice"),
    ("4", "Troubleshooting", "troubleshooting_agent", "Engineer-style diagnostic workflows over live account data"),
    ("5", "Action Execution", "action_agent", "18 guarded actions: refunds, replacements, unlocks, sync, visits…"),
    ("6", "Conversation Memory", "memory_agent", "Multi-turn slot filling and issue lifecycle"),
    ("7", "Escalation Decision", "escalation_agent", "Rule + confidence decision with learned thresholds"),
    ("8", "Ticket Generation", "ticket_agent", "Complete structured ticket for human agents"),
    ("9", "Resolution Tracking", "tracking_agent", "Lifecycle, auto customer updates, SLA monitor"),
    ("10", "Customer Satisfaction", "satisfaction_agent", "CSAT, sentiment, resolution quality → feedback loop"),
    ("11", "Master Orchestrator", "orchestrator_agent", "Planner → executors → reviewer guardrail, with trace"),
    ("+", "Root Cause Discovery", "insights_agent", "Ticket-spike detection with attribute drill-down"),
    ("+", "Omnichannel Adapter", "channel_agent", "Web, WhatsApp, Email, Telegram, Mobile App"),
    ("+", "Response Composer", "response_agent", "Sentiment-aware, fact-grounded replies (optional LLM polish)"),
]

__all__ = [
    "ACTIONS", "AGENTS", "ALWAYS_ESCALATE", "AUTO_REFUND_LIMIT", "CHANNELS", "COMPANY", "INTENTS", "OPEN_STATUSES",
    "PRODUCT", "SAMPLE_PAYLOADS", "STATUS_FLOW", "TEAMS", "TRANSITIONS", "Store", "action_catalog", "analyze_sentiment",
    "compact_context", "csat_summary", "customer_context", "daily_volume", "decide_escalation", "detect_incidents",
    "execute_action", "expand_kb", "extract_entities", "fmt_ts", "format_outbound", "get_groq_api_key", "get_store",
    "handle_message", "inr", "investigate", "learned_thresholds", "llm_enabled", "load_kb", "normalize_inbound",
    "record_csat", "render_ticket", "resolve_customer_id", "review", "run_conversation", "search_kb", "select_workflow",
    "sla_scan", "sla_status", "summarize_turn", "supervisor_metrics", "transition", "understand",
]
