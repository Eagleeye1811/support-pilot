"""Agent 11: Master Orchestrator — Planner → Executor agents → Reviewer guardrail, with a full trace.

One customer message flows through:
  Channel adapter → Understand intent → Memory (slot filling / lifecycle) →
  Customer context → Knowledge retrieval → Troubleshooting → Escalation
  (pre-action) → Action execution → Escalation (post-action) → Ticket →
  Resolution tracking → Response (sentiment-aware) → CSAT → Reviewer.
"""
from __future__ import annotations

import re
import time

from . import channel_agent, context_agent, intent_agent, memory_agent, response_agent, satisfaction_agent
from .mcp_bus import get_bus
from .shared_agent import ALWAYS_ESCALATE, AUTO_REFUND_LIMIT, CHANNELS, COMPANY, INTENTS, llm_enabled
from .tools import TOOLS


class Tracer:
    """Records every step of a turn; agent steps are MCP tool calls made through the bus."""

    def __init__(self, bus=None):
        self.bus = bus
        self.steps = []

    def tool(self, agent, name, args, summarize=lambda r: ""):
        t0 = time.time()
        try:
            result, rec = self.bus.call(name, args)
        except Exception as exc:
            self.steps.append(dict(agent=agent, status="ERROR", ms=round((time.time() - t0) * 1000, 1), summary=str(exc)[:300],
                                   tool=name, transport=self.bus.transport))
            raise
        self.steps.append(dict(rec, agent=agent, status="OK", summary=summarize(result)))
        return result

    def note(self, agent, summary, status="OK"):
        self.steps.append(dict(agent=agent, status=status, ms=0.0, summary=summary))


def review(reply, decision, actions, ticket, intent, ctx):
    """Reviewer guardrail: verify the turn before anything reaches the customer."""
    checks = [
        ("Reply is non-empty", bool(reply and reply.strip())),
        ("Only successful actions are claimed",
         all(not a["message"] or a["message"] not in reply for a in actions if a["status"] in ("failed", "blocked"))),
        ("AI refunds within limit",
         all(a["data"].get("amount", 0) <= AUTO_REFUND_LIMIT for a in actions
             if a["action"] == "initiate_refund" and a["status"] == "success" and a["actor"] == "AI")),
        ("Escalation carries a complete ticket",
         decision != "escalate" or bool(ticket and ticket.get("conversation_summary") and ticket.get("team") and ticket.get("investigation") is not None)),
        ("Mandatory escalations honoured", intent not in ALWAYS_ESCALATE or decision == "escalate"),
        ("No PII leakage", not re.search(r"\b\d{12,19}\b", reply or "") and not (ctx and ctx.get("email") and ctx["email"] in (reply or ""))),
    ]
    rows = [dict(check=c, passed=bool(p)) for c, p in checks]
    return dict(approved=all(r["passed"] for r in rows), checks=rows, rejected=[r["check"] for r in rows if not r["passed"]])


def _issue_understanding(understanding, issue, mode):
    """Score the issue with the confidence of the message that opened it, not of a bare 'ORD12345' reply."""
    if not issue or mode == "new":
        return understanding
    conf = issue.get("confidence", understanding["confidence"])
    if understanding["intent"] == issue["intent"]:
        conf = max(conf, understanding["confidence"])
    return dict(understanding, intent=issue["intent"], category=issue["category"], confidence=conf,
                priority=issue.get("priority", understanding["priority"]))


def handle_message(store, text=None, conversation_id=None, customer_id=None, channel="web", payload=None, use_llm=True,
                   attachments=None, contact_email=None):
    """Process one inbound customer message end-to-end and return the full, traceable turn.

    Every agent runs as an MCP tool call (see ``mcp_bus.py``); the trace records each call.
    """
    bus = get_bus(store)
    T = Tracer(bus)
    attachments = list(attachments or [])
    T.note("Master Orchestrator (Planner)", f"MCP client · {bus.transport} · plan: understand → memory → context → knowledge "
           "→ troubleshoot → decide → act → ticket → track → media → email → reply → review")

    inbound = T.tool("0 Omnichannel Adapter", "normalize_channel",
                     dict(channel=channel, payload=payload if payload is not None else {"text": text}),
                     lambda r: f"{CHANNELS.get(r['channel'], r['channel'])} · sender {r['sender'] or 'session'}")
    text = inbound["text"]
    if not text:
        raise ValueError("Empty message")
    conv = memory_agent.load_conversation(store, conversation_id, customer_id or inbound["sender"].get("customer_id"), inbound["channel"])
    conv["channel"] = inbound["channel"]
    if contact_email:
        conv["contact_email"] = contact_email
    memory_agent.add_message(conv, "user", text, dict(channel=inbound["channel"], attachments=attachments))
    out = dict(conversation_id=conv["id"], channel=inbound["channel"], inbound=inbound, text=text, attachments=attachments,
               understanding=None, issue=None, mode=None, context=None, articles=[], workflow=None, investigation=None,
               actions=[], decision=None, pre_decision=None, ticket=None, review=None, llm_polished=False, media=[], email=None)

    def finish(reply, ctx=None, decision=None, ticket=None, intent=None, actions=()):
        polished = False
        if use_llm and decision in ("auto_resolve", "escalate") and llm_enabled():
            p = T.tool("Response Composer", "polish_reply",
                       dict(reply=reply, emotion=(out.get("understanding") or {}).get("emotion", "calm")),
                       lambda r: "LLM polish accepted" if r["polished"] else "kept the deterministic wording")
            reply, polished = p["reply"], p["polished"]
        rv = T.tool("Reviewer (guardrail)", "review_reply",
                    dict(reply=reply, decision=decision, actions=list(actions), ticket=ticket, intent=intent, context=ctx),
                    lambda r: "approved" if r["approved"] else "rejected: " + ", ".join(r["rejected"]))
        if not rv["approved"]:
            reply = ("I want to make sure this is handled correctly, so I've passed your request to a specialist who will "
                     "contact you shortly." + (f" Your reference is {ticket['id']}." if ticket else ""))
        memory_agent.add_message(conv, "assistant", reply, dict(ticket_id=(ticket or {}).get("id"), decision=decision,
                                                                media=[m["sku"] for m in out["media"]]))
        memory_agent.save(store, conv)
        out.update(reply=reply, reply_channel=channel_agent.format_outbound(
            conv["channel"], reply, (ctx or {}).get("first_name"), (ticket or {}).get("id"),
            (out.get("issue") or {}).get("label")), review=rv, trace=T.steps, llm_polished=polished,
            awaiting=conv.get("awaiting"), conversation=conv, transport=bus.transport)
        return out

    # ---- CSAT reply ----------------------------------------------------------
    if conv.get("awaiting") == "csat":
        rating, comment = satisfaction_agent.parse_rating(text)
        if rating:
            tid = conv.get("awaiting_ticket")
            rec = T.tool("10 Customer Satisfaction", "record_csat",
                         dict(ticket_id=tid, rating=rating, comment=comment, conversation_id=conv["id"]),
                         lambda r: f"CSAT {r['rating']}/5 · quality {r['resolution_quality']}")
            t = store.get("tickets", tid)
            conv["awaiting"] = None
            if conv.get("active_issue"):
                memory_agent.set_status(conv["active_issue"], "closed", f"CSAT {rating}/5")
            ctx = context_agent.customer_context(store, conv.get("customer_id"))
            reply = ("Thank you for the feedback! 🙏 " if rating >= 4 else
                     "Thank you for the honest feedback — I've shared it with our quality team so we can do better. ")
            reply += "Is there anything else I can help you with?"
            out["csat"] = rec
            return finish(reply, ctx, decision="csat", ticket=t)

    # ---- 1 understand + 6 memory ----------------------------------------------
    understanding = T.tool("1 Conversation Understanding", "understand_message", dict(text=text, use_llm=use_llm),
                           lambda r: f"{r['intent']} · {r['category']} · {r['priority']} · {r['emotion']} · conf {r['confidence']:.0%}")
    out["understanding"] = understanding
    conv["sentiment_trail"].append(understanding["emotion"])

    def memory(force_new=False):
        res = T.tool("6 Conversation Memory", "track_conversation",
                     dict(conv=conv, understanding=understanding, text=text, force_new=force_new),
                     lambda r: f"{r['mode']} · issue {(r['conv'].get('active_issue') or {}).get('intent', '—') if r['has_issue'] else '—'}"
                               f" · turn {len(r['conv']['messages'])}")
        conv.clear()
        conv.update(res["conv"])  # keep the same dict object, re-bound to the tool's result
        return (conv["active_issue"] if res["has_issue"] else None), res["mode"]

    issue, mode = memory()
    out.update(issue=issue, mode=mode)

    # ---- 2 customer context ---------------------------------------------------
    ents = dict((issue or {}).get("slots", {}), **understanding["entities"])
    cres = T.tool("2 Customer Context", "customer_context",
                  dict(customer_id=conv.get("customer_id"), entities=ents, sender=inbound["sender"]),
                  lambda r: ((r["context"]["headline"] + (f" (identified by {r['method']})" if r["method"] and r["method"] != "session" else ""))
                             if r["context"] else "customer not identified"))
    if cres["customer_id"] and not conv.get("customer_id"):
        conv["customer_id"] = cres["customer_id"]
    ctx = cres["context"] if conv.get("customer_id") == cres["customer_id"] else \
        context_agent.customer_context(store, conv.get("customer_id"))
    out["context"] = ctx

    intent = understanding["intent"]
    # ---- conversational / meta turns -------------------------------------------
    if mode == "meta" or (issue is None and intent_agent.is_conversational(intent)):
        if intent == "greeting":
            name = f", {ctx['first_name']}" if ctx else ""
            return finish(f"Hi{name}! 👋 I'm the {COMPANY} support assistant. "
                          "Tell me what's going on — orders, payments, refunds, login, subscriptions or your NovaFiber connection — "
                          "and I'll investigate and fix it.", ctx)
        if intent in ("thanks", "deny"):
            if conv.get("awaiting") == "csat":
                return finish("You're welcome! Before you go — " + satisfaction_agent.CSAT_PROMPT, ctx)
            return finish("You're welcome! Have a great day. 😊", ctx)
        if intent == "affirm":
            return finish("Great! Tell me what else I can help with.", ctx)
        if intent == "ticket_status":
            if not ctx:
                return finish(response_agent.SLOT_QUESTIONS["customer_identity"], ctx)
            st = T.tool("9 Resolution Tracking", "ticket_status",
                        dict(customer_id=ctx["customer_id"], ticket_id=understanding["entities"].get("ticket_id")),
                        lambda r: f"status of {r['ticket']['id']}" if r["ticket"] else "no open ticket")
            return finish(st["message"], ctx, ticket=st["ticket"])
        if intent == "human_agent" and issue is None:
            issue, mode = memory(force_new=True)
            out.update(issue=issue, mode="new")
    human_requested = intent == "human_agent"
    if issue is None:
        issue, mode = memory(force_new=True)
        out.update(issue=issue, mode="new")
    iu = _issue_understanding(understanding, issue, mode)
    memory_agent.set_status(issue, "investigating")

    # ---- 3 knowledge ----------------------------------------------------------------
    kres = T.tool("3 Knowledge Retrieval", "search_knowledge",
                  dict(query=f"{issue.get('first_message') or ''} {text}", intent=issue["intent"], category=issue["category"], top_k=4),
                  lambda r: ", ".join(f"{a['id']} ({a['relevance']:.0%})" for a in r["articles"][:3]) or "no match")
    articles, workflow = kres["articles"], kres["workflow"]
    out.update(articles=articles, workflow=workflow)

    # ---- 4 troubleshooting --------------------------------------------------------
    if issue["intent"] == "human_agent":
        investigation = dict(workflow="human_agent", steps=[dict(check="Collect context", status="info",
                             detail="Customer asked for a human; full context attached to the ticket")],
                             diagnosis="Customer requested a human agent.", resolvable=False, confidence=0.9,
                             recommended_actions=[], missing_slots=[], team="Tier-1 Support", facts={})
    else:
        investigation = T.tool("4 Troubleshooting", "troubleshoot",
                               dict(intent=issue["intent"], context=ctx, slots=issue["slots"], articles=articles,
                                    kb_confidence=kres["kb_confidence"], attachments=attachments),
                               lambda r: f"{len(r['steps'])} checks · {r['diagnosis'][:90]} · conf {r['confidence']:.0%}")
        issue["slots"].update(investigation.get("slots") or {})
    out["investigation"] = investigation

    # ---- 7 escalation (pre) → 5 actions → 7 escalation (post) ------------------------
    def decide(actions=None, label="7 Escalation Decision (pre-action)", summary=None):
        return T.tool(label, "decide_escalation",
                      dict(understanding=iu, investigation=investigation, context=ctx, issue=issue, actions=actions,
                           human_requested=human_requested, clarification_count=conv.get("clarification_count", 0)),
                      summary or (lambda r: f"{r['decision']} · conf {r['confidence']:.0%} vs threshold {r['threshold']:.0%}"))

    pre = decide()
    out["pre_decision"] = pre
    planned = investigation["recommended_actions"] if pre["decision"] == "auto_resolve" else (
        [a for a in investigation["recommended_actions"] if a.get("protective")] if pre["decision"] == "escalate" else [])
    actions = []
    if planned:
        actions = T.tool("5 Action Execution", "execute_actions",
                         dict(actions=[dict(action=a["action"], params=a["params"]) for a in planned], context=ctx,
                              conversation_id=conv["id"]),
                         lambda r: ", ".join(f"{a['label']}={a['status']}" for a in r))
    decision = pre
    if actions:
        decision = decide(actions, "7 Escalation Decision (post-action)",
                          lambda r: f"{r['decision']}" + (" · escalated after failed action" if r["decision"] != pre["decision"] else ""))
    out.update(actions=actions, decision=decision)

    def reply_for(ticket):
        return T.tool("Response Composer", "compose_reply",
                      dict(decision=decision, understanding=iu, investigation=investigation, actions=actions, context=ctx,
                           ticket=ticket, issue=issue, articles=articles, channel=conv["channel"]),
                      lambda r: f"{len(r['reply'])} chars · tone for {iu.get('emotion', 'calm')} customer")["reply"]

    # ---- clarify ----------------------------------------------------------------------
    if decision["decision"] == "clarify":
        conv["clarification_count"] = conv.get("clarification_count", 0) + 1
        issue["missing_slots"] = investigation["missing_slots"]
        memory_agent.set_status(issue, "awaiting_customer", "Asked for " + ", ".join(investigation["missing_slots"]))
        return finish(reply_for(None), ctx, decision="clarify", intent=issue["intent"], actions=actions)
    issue["missing_slots"] = []

    # ---- 8 ticket + 9 tracking -----------------------------------------------------------
    summary = memory_agent.summarize(conv, issue, investigation, actions)
    ticket = T.tool("8 Ticket Generation", "create_ticket",
                    dict(conv=conv, issue=issue, understanding=iu, context=ctx, investigation=investigation, actions=actions,
                         decision=decision, summary=summary, articles=articles, attachments=attachments),
                    lambda r: f"{r['id']} · {r['status']} · {r['team']} · {r['priority']}")
    issue["ticket_id"] = ticket["id"]
    for a in actions:
        a["ticket_id"] = ticket["id"]
    tr = T.tool("9 Resolution Tracking", "track_resolution",
                dict(ticket_id=ticket["id"], decision=decision["decision"], actions=actions, context=ctx,
                     channel=conv["channel"], conversation_id=conv["id"]),
                lambda r: f"{r['ticket_id']} {r['status']} · SLA {r['sla'].get('state')}")
    if decision["decision"] == "auto_resolve":
        memory_agent.set_status(issue, "resolved", investigation["diagnosis"])
        conv["awaiting"], conv["awaiting_ticket"] = "csat", ticket["id"]
    else:
        memory_agent.set_status(issue, "escalated", f"{ticket['id']} → {ticket['team']}")
    out["ticket"] = store.get("tickets", ticket["id"]) or ticket
    out["tracking"] = tr

    # ---- media + email ------------------------------------------------------------------
    if "select_media" in TOOLS:
        out["media"] = T.tool("Media Agent", "select_media",
                              dict(intent=issue["intent"], investigation=investigation, context=ctx, slots=issue["slots"]),
                              lambda r: ", ".join(f"{m['role']}: {m['name']}" for m in r) or "no product images")
    reply = reply_for(out["ticket"])
    if decision["decision"] == "auto_resolve":
        T.note("10 Customer Satisfaction", "CSAT requested")
        reply += "\n\n" + satisfaction_agent.CSAT_PROMPT
    if "send_email" in TOOLS and conv.get("contact_email"):
        out["email"] = T.tool("Email Agent", "send_email",
                              dict(to=conv["contact_email"], kind="ticket", ticket_id=ticket["id"], reply=reply,
                                   media=[m["sku"] for m in out["media"]], customer_name=(ctx or {}).get("first_name")),
                              lambda r: f"{r['status']} → {r['to_masked']}")
    return finish(reply, ctx, decision=decision["decision"], ticket=out["ticket"], intent=issue["intent"], actions=actions)


def run_conversation(store, messages, customer_id=None, channel="web", use_llm=False):
    """Run several customer messages through one conversation (used by evaluation, tests and MCP)."""
    conv_id, turns = None, []
    for m in messages:
        r = handle_message(store, m, conv_id, customer_id, channel, use_llm=use_llm)
        conv_id = r["conversation_id"]
        turns.append(r)
    return turns


def label(intent):
    return INTENTS.get(intent, {}).get("label", intent)


def summarize_turn(r):
    """JSON-friendly, compact view of one orchestrated turn (MCP responses, evaluation, logs)."""
    issue, inv, dec, t = r.get("issue") or {}, r.get("investigation") or {}, r.get("decision"), r.get("ticket")
    return dict(
        reply=r["reply"], reply_for_channel=r.get("reply_channel"), conversation_id=r["conversation_id"], channel=r["channel"],
        awaiting=r.get("awaiting"),
        understanding={k: (r.get("understanding") or {}).get(k) for k in
                       ("intent", "category", "priority", "sentiment", "emotion", "confidence", "entities")},
        issue=dict(intent=issue.get("intent"), status=issue.get("status"), slots=issue.get("slots"),
                   lifecycle=[f"{x['status']}: {x['note']}" for x in issue.get("lifecycle", [])]) if issue else None,
        customer=context_agent.compact(r.get("context")),
        knowledge=[dict(id=a["id"], title=a["title"], relevance=a["relevance"]) for a in r.get("articles", [])[:3]],
        workflow=(r.get("workflow") or {}).get("title"),
        investigation=dict(steps=[f"[{s['status']}] {s['check']}: {s['detail']}" for s in inv.get("steps", [])],
                           diagnosis=inv.get("diagnosis"), confidence=inv.get("confidence"),
                           resolvable=inv.get("resolvable")) if inv else None,
        actions=[dict(id=a["id"], action=a["action"], status=a["status"], message=a["message"]) for a in r.get("actions", [])],
        decision=dict(decision=dec["decision"], confidence=dec["confidence"], threshold=dec["threshold"], team=dec["team"],
                      reasons=dec["reasons"]) if isinstance(dec, dict) else dec,
        ticket=dict(id=t["id"], title=t["title"], status=t["status"], team=t.get("team"), priority=t["priority"],
                    assignee=t.get("assignee")) if t else None,
        review=r.get("review"), trace=r.get("trace"))
