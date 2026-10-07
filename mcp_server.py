"""Model Context Protocol server for SupportPilot.

Exposes the multi-agent support system as MCP tools, resources and a prompt —
including every individual agent (the same tools the orchestrator calls over MCP) —
so Claude (Desktop / Code / any MCP client) can act as — or supervise — the
autonomous support desk. It shares the same SQLite store as the Streamlit app
(data/supportpilot.db by default, override with SUPPORTPILOT_DB), so a ticket
created through MCP shows up on the Supervisor Dashboard immediately.

Run:
    python mcp_server.py                      # stdio (Claude Desktop / Claude Code)
    python mcp_server.py --http --port 8000   # streamable HTTP at http://127.0.0.1:8000/mcp

Works with the official `mcp` Python SDK v2 (MCPServer) and v1 (FastMCP).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agents as A  # noqa: E402
from agents.mcp_bus import add_agent_tools  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

INSTRUCTIONS = (
    "SupportPilot is an autonomous customer-support desk for ShopNova (e-commerce), Nova Plus/Premium (SaaS "
    "subscriptions) and NovaFiber (telecom). Use support_handle_message to process a customer message end-to-end "
    "(intent → context → knowledge → troubleshooting → actions → escalation → ticket → tracking). Keep the returned "
    "conversation_id to continue a multi-turn conversation. Use the other tools to inspect customers, search the "
    "knowledge base, manage tickets, run the SLA monitor and discover root causes. Demo customers: CUST1001-CUST1012."
)

INSTRUCTIONS += (
    " Lower-level agent tools (understand_message, customer_context, search_knowledge, troubleshoot, decide_escalation, "
    "execute_actions, create_ticket, select_media, send_email, ...) are the exact tools the SupportPilot orchestrator "
    "itself calls over MCP for every customer message; use them to inspect or run a single agent."
)

mcp = _Server("supportpilot", instructions=INSTRUCTIONS)
add_agent_tools(mcp, A.get_store())  # the same agent tools the orchestrator calls (agents/tools.py)


def store():
    return A.get_store()


def _ticket_view(t):
    if not t:
        return None
    view = {k: v for k, v in t.items() if k != "transcript"}
    view["sla_status"] = A.sla_status(t)
    view["markdown"] = A.render_ticket(t)
    return view


# ---- end-to-end -------------------------------------------------------------------------
@mcp.tool()
def support_handle_message(message: str, customer_id: Optional[str] = None, conversation_id: Optional[str] = None,
                           channel: str = "web") -> dict[str, Any]:
    """Process one customer message through the full 11-agent workflow and return the reply plus evidence.

    Args:
        message: The customer's message, e.g. "My payment failed but money was deducted".
        customer_id: Logged-in customer (e.g. CUST1002). Omit for a guest — the agent will ask for email/phone/order id.
        conversation_id: Pass the id returned by a previous call to continue the same conversation (multi-turn memory).
        channel: One of web, whatsapp, email, telegram, app.
    """
    if not message.strip():
        raise ToolError("message must not be empty")
    r = A.handle_message(store(), message, conversation_id, customer_id, channel)
    return A.summarize_turn(r)


@mcp.tool()
def support_channel_message(channel: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Process a raw inbound payload from a channel (omnichannel adapter).

    Args:
        channel: whatsapp, email, telegram, app or web.
        payload: Channel-native payload, e.g. WhatsApp {"from": "+91…", "text": {"body": "…"}} or
                 email {"from": "Name <a@b.com>", "subject": "…", "body": "…"}.
    """
    r = A.handle_message(store(), channel=channel, payload=payload)
    return A.summarize_turn(r)


# ---- individual agents ---------------------------------------------------------------------
@mcp.tool()
def support_classify_intent(message: str) -> dict[str, Any]:
    """Conversation Understanding Agent: intent, category, priority, sentiment/emotion, entities and confidence."""
    return A.understand(message)


@mcp.tool()
def support_get_customer_context(customer_id: Optional[str] = None, email: Optional[str] = None,
                                 phone: Optional[str] = None, order_id: Optional[str] = None) -> dict[str, Any]:
    """Customer Context Agent: account, tier, purchases, previous tickets, subscription, transactions and flags."""
    cid, method = A.resolve_customer_id(store(), customer_id, dict(email=email, phone=phone, order_id=order_id))
    if not cid:
        return {"found": False, "message": "No customer matches the given identifiers."}
    return {"found": True, "identified_by": method, "context": A.customer_context(store(), cid)}


@mcp.tool()
def support_search_knowledge(query: str, intent: Optional[str] = None, top_k: int = 4) -> list[dict[str, Any]]:
    """Knowledge Base Agent: BM25 search over FAQs, policies, product docs, troubleshooting guides and auto-generated articles."""
    return A.search_kb(query, intent, None, max(1, min(top_k, 10)), store())


@mcp.tool()
def support_troubleshoot(intent: str, customer_id: str, order_id: Optional[str] = None,
                         service_id: Optional[str] = None) -> dict[str, Any]:
    """Troubleshooting Agent (read-only): run the diagnostic workflow for an intent and return evidence, diagnosis,
    confidence and recommended actions. No action is executed.

    Args:
        intent: e.g. wrong_item, payment_failure, login_issue, subscription_not_active, network_issue. See resource
                supportpilot://intents for the full list.
    """
    if intent not in A.INTENTS:
        raise ToolError(f"Unknown intent '{intent}'. Valid: {', '.join(A.INTENTS)}")
    ctx = A.customer_context(store(), customer_id)
    if not ctx:
        raise ToolError(f"Customer {customer_id} not found")
    slots = {k: v for k, v in dict(order_id=order_id, service_id=service_id).items() if v}
    return A.investigate(store(), intent, ctx, slots)


@mcp.tool()
def support_execute_action(action: str, customer_id: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Action Execution Agent: run one guarded action (allow-list, ownership check, AI refund limit ₹10,000, idempotency).

    Args:
        action: e.g. initiate_refund, create_replacement, generate_return_label, cancel_order, unlock_account,
                reset_password, retry_subscription_sync, upgrade_plan, schedule_technician_visit. See support_list_actions.
        customer_id: Customer the action is performed for (ownership is verified).
        params: Action parameters, e.g. {"order_id": "ORD12345"} or {"subscription_id": "SUB1004"}.
    """
    ctx = A.customer_context(store(), customer_id)
    if not ctx:
        raise ToolError(f"Customer {customer_id} not found")
    p = dict(params or {})
    p.setdefault("customer_id", customer_id)
    return A.execute_action(store(), action, p, ctx, actor="AI")


@mcp.tool()
def support_list_actions() -> list[dict[str, Any]]:
    """List every action the Action Execution Agent can perform, grouped by domain."""
    return A.action_catalog()


# ---- tickets, tracking, CSAT ---------------------------------------------------------------
@mcp.tool()
def support_get_ticket(ticket_id: str) -> dict[str, Any]:
    """Ticket Generation Agent output: full structured ticket (investigation, actions taken, escalation reasons, SLA,
    conversation summary) plus a ready-to-read markdown version for human agents."""
    t = store().get("tickets", ticket_id)
    if not t:
        raise ToolError(f"Ticket {ticket_id} not found")
    return _ticket_view(t)


@mcp.tool()
def support_list_tickets(status: Optional[str] = None, priority: Optional[str] = None, team: Optional[str] = None,
                         escalated_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    """List tickets newest first, filtered by status (Opened/Assigned/Pending Customer/Resolved/Closed), priority,
    team or escalation."""
    rows = store().list("tickets")
    if status:
        rows = [t for t in rows if t["status"].lower() == status.lower()]
    if priority:
        rows = [t for t in rows if t["priority"] == priority.lower()]
    if team:
        rows = [t for t in rows if (t.get("team") or "").lower() == team.lower()]
    if escalated_only:
        rows = [t for t in rows if t.get("escalated")]
    rows.sort(key=lambda t: t["created_at"], reverse=True)
    return [dict(id=t["id"], title=t["title"], customer=t["customer"]["name"], tier=t["customer"].get("tier"),
                 status=t["status"], priority=t["priority"], team=t.get("team"), assignee=t.get("assignee"),
                 channel=t.get("channel"), created_at=t["created_at"], sla=A.sla_status(t).get("state"))
            for t in rows[:max(1, min(limit, 100))]]


@mcp.tool()
def support_update_ticket(ticket_id: str, status: str, note: str = "", actor: str = "Human Agent",
                          resolution_note: Optional[str] = None) -> dict[str, Any]:
    """Resolution Tracking Agent: move a ticket through Opened → Assigned → Pending Customer → Resolved → Closed
    (or Reopened). The customer is notified automatically. When resolving with a resolution_note, the Knowledge
    Base Agent may auto-create a new FAQ article from it (Auto Knowledge Base Expansion)."""
    try:
        t = A.transition(store(), ticket_id, status, actor=actor, note=note or (resolution_note or ""))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    kb = None
    if status == "Resolved" and resolution_note:
        t["resolution"] = resolution_note
        store().put("tickets", t["id"], t)
        kb = A.expand_kb(store(), t, resolution_note)
    return {"ticket": _ticket_view(t), "kb_expansion": kb}


@mcp.tool()
def support_record_csat(ticket_id: str, rating: int, comment: str = "") -> dict[str, Any]:
    """Customer Satisfaction Agent: record a 1-5 CSAT rating; computes sentiment and resolution quality and feeds the
    learned escalation thresholds."""
    try:
        return A.record_csat(store(), ticket_id, rating, comment)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ---- operations ------------------------------------------------------------------------------
@mcp.tool()
def support_sla_scan(auto_escalate: bool = True) -> list[dict[str, Any]]:
    """SLA Monitoring: list open tickets at risk (≥75% of deadline used) or breached; optionally auto-escalate them to
    the Escalation Desk (e.g. Premium users have a 15-minute response deadline)."""
    return A.sla_scan(store(), auto_escalate=auto_escalate)


@mcp.tool()
def support_root_cause_incidents() -> list[dict[str, Any]]:
    """Root Cause Discovery: detect ticket spikes vs a 14-day baseline and drill down to a likely cause
    (e.g. payment failures concentrated on one gateway → possible gateway outage)."""
    return A.detect_incidents(store())


@mcp.tool()
def support_dashboard_metrics() -> dict[str, Any]:
    """Supervisor Dashboard numbers: open tickets, resolution/automation/escalation rates, average resolution time,
    SLA risk, CSAT summary and learned escalation thresholds."""
    return dict(metrics=A.supervisor_metrics(store()), csat=A.csat_summary(store()),
                learned_thresholds={k: v for k, v in A.learned_thresholds(store()).items() if v["reason"] != "default"})


@mcp.tool()
def support_reset_demo_data(confirm: bool = False) -> dict[str, Any]:
    """Wipe all tickets/conversations/actions and re-seed the synthetic demo world. Requires confirm=true."""
    if not confirm:
        return {"reset": False, "message": "Pass confirm=true to wipe and re-seed the demo data."}
    store().reset()
    return {"reset": True, "collections": store().stats()}


# ---- resources & prompt ------------------------------------------------------------------------
@mcp.resource("supportpilot://kb", name="knowledge_base", description="All knowledge-base articles", mime_type="application/json")
def kb_resource() -> list[dict[str, Any]]:
    return A.load_kb(store())


@mcp.resource("supportpilot://intents", name="intents", description="Intent taxonomy with category, priority and owning team",
              mime_type="application/json")
def intents_resource() -> dict[str, Any]:
    return {k: dict(label=v["label"], category=v["category"], priority=v["priority"], team=v["team"], domain=v["domain"],
                    required_slots=v["required_slots"]) for k, v in A.INTENTS.items()}


@mcp.resource("supportpilot://agents", name="agents", description="The multi-agent architecture", mime_type="application/json")
def agents_resource() -> list[dict[str, str]]:
    return [dict(no=n, agent=a, module=m, job=j) for n, a, m, j in A.AGENTS]


@mcp.prompt()
def triage_customer_issue(message: str, customer_id: str = "") -> str:
    """Ask the model to resolve a customer issue with SupportPilot's tools."""
    return (f"A customer{' (' + customer_id + ')' if customer_id else ''} wrote: \"{message}\".\n"
            "Use support_handle_message to process it. Report the intent, what was investigated, which actions were "
            "executed, whether it was escalated (and why), and the ticket id. If escalated, fetch the ticket with "
            "support_get_ticket and summarise it for a human agent.")


def main():
    parser = argparse.ArgumentParser(description="SupportPilot MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.http:
        try:
            mcp.run("streamable-http", host=args.host, port=args.port)
        except TypeError:  # mcp 1.x configures host/port on settings
            mcp.settings.host, mcp.settings.port = args.host, args.port
            mcp.run("streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
