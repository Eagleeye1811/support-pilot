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

from . import (action_agent, channel_agent, context_agent, email_agent, escalation_agent, intent_agent, knowledge_agent, media,
               memory_agent, response_agent, satisfaction_agent, ticket_agent, tracking_agent, troubleshooting_agent)
from .shared_agent import ALWAYS_ESCALATE, AUTO_REFUND_LIMIT, CHANNELS, COMPANY, INTENTS

PHOTO_INTENTS = {"wrong_item", "damaged_item", "return_request"}


class Tracer:
    def __init__(self):
        self.steps = []

    def run(self, agent, fn, summarize=lambda r: ""):
        t0 = time.time()
        try:
            result = fn()
            status = "OK"
        except Exception as exc:
            self.steps.append(dict(agent=agent, status="ERROR", ms=round((time.time() - t0) * 1000, 1), summary=str(exc)))
            raise
        self.steps.append(dict(agent=agent, status=status, ms=round((time.time() - t0) * 1000, 1), summary=summarize(result)))
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
                   contact_email=None, attachments=None, defer_csat=False):
    """Process one inbound customer message end-to-end and return the full, traceable turn."""
    T = Tracer()
    plan = ["channel", "understand", "memory", "context", "knowledge", "troubleshoot", "escalate?", "act", "escalate",
            "ticket", "track", "respond", "csat", "review"]
    T.note("Master Orchestrator (Planner)", "Plan: " + " → ".join(plan))

    inbound = T.run("0 Omnichannel Adapter",
                    lambda: channel_agent.normalize_inbound(channel, payload if payload is not None else {"text": text}),
                    lambda r: f"{CHANNELS.get(r['channel'], r['channel'])} · sender {r['sender'] or 'session'}")
    text = inbound["text"]
    if not text:
        raise ValueError("Empty message")
    conv = memory_agent.load_conversation(store, conversation_id, customer_id or inbound["sender"].get("customer_id"), inbound["channel"])
    conv["channel"] = inbound["channel"]
    if contact_email:  # an address the customer gave us: the Email agent sends ticket emails there
        conv["contact_email"] = contact_email
    attachments = list(attachments or [])
    memory_agent.add_message(conv, "user", text, dict(channel=inbound["channel"], attachments=attachments))
    out = dict(conversation_id=conv["id"], channel=inbound["channel"], inbound=inbound, understanding=None, issue=None,
               mode=None, context=None, articles=[], workflow=None, investigation=None, actions=[], decision=None,
               pre_decision=None, ticket=None, review=None, llm_polished=False, email=None, attachments=attachments)

    def finish(reply, ctx=None, decision=None, ticket=None, intent=None, actions=()):
        polished = False
        if use_llm and decision in ("auto_resolve", "escalate"):
            reply, polished = response_agent.polish(reply, (out.get("understanding") or {}).get("emotion", "calm"))
        rv = T.run("Reviewer (guardrail)", lambda: review(reply, decision, list(actions), ticket, intent, ctx),
                   lambda r: "approved" if r["approved"] else "rejected: " + ", ".join(r["rejected"]))
        if not rv["approved"]:
            reply = ("I want to make sure this is handled correctly, so I've passed your request to a specialist who will "
                     "contact you shortly." + (f" Your reference is {ticket['id']}." if ticket else ""))
        memory_agent.add_message(conv, "assistant", reply, dict(ticket_id=(ticket or {}).get("id"), decision=decision))
        memory_agent.save(store, conv)
        out.update(reply=reply, reply_channel=channel_agent.format_outbound(
            conv["channel"], reply, (ctx or {}).get("first_name"), (ticket or {}).get("id"),
            (out.get("issue") or {}).get("label")), review=rv, trace=T.steps, llm_polished=polished,
            awaiting=conv.get("awaiting"), conversation=conv)
        return out

    # ---- CSAT reply ----------------------------------------------------------
    if conv.get("awaiting") == "csat":
        rating, comment = satisfaction_agent.parse_rating(text)
        if rating:
            tid = conv.get("awaiting_ticket")
            rec = T.run("10 Customer Satisfaction", lambda: satisfaction_agent.record_csat(store, tid, rating, comment, conv["id"]),
                        lambda r: f"CSAT {r['rating']}/5 · quality {r['resolution_quality']}")
            t = store.get("tickets", tid)
            if t and t["status"] == "Resolved":
                tracking_agent.transition(store, t, "Closed", actor="Customer", note="Customer confirmed via CSAT", notify=False)
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
    understanding = T.run("1 Conversation Understanding", lambda: intent_agent.understand(text, use_llm),
                          lambda r: f"{r['intent']} · {r['category']} · {r['priority']} · {r['emotion']} · conf {r['confidence']:.0%}")
    out["understanding"] = understanding
    conv["sentiment_trail"].append(understanding["emotion"])
    issue, mode = T.run("6 Conversation Memory", lambda: memory_agent.track_issue(conv, understanding, text),
                        lambda r: f"{r[1]} · issue {r[0]['intent'] if r[0] else '—'} · turn {len(conv['messages'])}")
    out.update(issue=issue, mode=mode)

    # ---- 2 customer context ---------------------------------------------------
    ents = dict((issue or {}).get("slots", {}), **understanding["entities"])
    cid, method = context_agent.resolve_customer_id(store, conv.get("customer_id"), ents, inbound["sender"])
    if cid and conv.get("customer_id") != cid and not conv.get("customer_id"):
        conv["customer_id"] = cid
    ctx = T.run("2 Customer Context", lambda: context_agent.customer_context(store, conv.get("customer_id")),
                lambda r: (r["headline"] + (f" (identified by {method})" if method and method != "session" else "")) if r else "customer not identified")
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
            t, msg = T.run("9 Resolution Tracking", lambda: tracking_agent.status_reply(store, ctx["customer_id"], understanding["entities"].get("ticket_id")),
                           lambda r: f"status of {r[0]['id']}" if r[0] else "no open ticket")
            return finish(msg, ctx, ticket=t)
        if intent == "human_agent" and issue is None:
            issue = memory_agent.open_issue(conv, understanding, text)
            out.update(issue=issue, mode="new")
    human_requested = intent == "human_agent"
    if issue is None:
        issue = memory_agent.open_issue(conv, understanding, text)
        out.update(issue=issue, mode="new")
    iu = _issue_understanding(understanding, issue, mode)
    memory_agent.set_status(issue, "investigating")

    # ---- 3 knowledge ----------------------------------------------------------------
    query = f"{issue.get('first_message') or ''} {text}"
    articles = T.run("3 Knowledge Retrieval", lambda: knowledge_agent.search(query, issue["intent"], issue["category"], 4, store),
                     lambda r: ", ".join(f"{a['id']} ({a['relevance']:.0%})" for a in r[:3]) or "no match")
    workflow = knowledge_agent.select_workflow(issue["intent"], articles)
    kb_conf = knowledge_agent.kb_confidence(articles, issue["intent"])
    out.update(articles=articles, workflow=workflow)

    # ---- 4 troubleshooting --------------------------------------------------------
    if issue["intent"] == "human_agent":
        investigation = dict(workflow="human_agent", steps=[dict(check="Collect context", status="info",
                             detail="Customer asked for a human; full context attached to the ticket")],
                             diagnosis="Customer requested a human agent.", resolvable=False, confidence=0.9,
                             recommended_actions=[], missing_slots=[], team="Tier-1 Support", facts={})
    else:
        investigation = T.run("4 Troubleshooting", lambda: troubleshooting_agent.investigate(store, issue["intent"], ctx, issue["slots"], articles, kb_conf=kb_conf),
                              lambda r: f"{len(r['steps'])} checks · {r['diagnosis'][:90]} · conf {r['confidence']:.0%}")
    if attachments and issue["intent"] in PHOTO_INTENTS:  # the customer's photo is evidence for the claim
        investigation["steps"].insert(0, dict(check="Photo evidence", status="pass",
                                              detail=f"{len(attachments)} customer photo(s) received and attached to the case"))
        if not investigation.get("missing_slots"):
            investigation["confidence"] = round(min(0.99, investigation.get("confidence", 0) + 0.05), 2)
    out["investigation"] = investigation

    # ---- 7 escalation (pre) → 5 actions → 7 escalation (post) ------------------------
    thresholds = satisfaction_agent.learned_thresholds(store)
    pre = T.run("7 Escalation Decision (pre-action)", lambda: escalation_agent.decide(
        iu, investigation, ctx, issue, None, thresholds, human_requested, conv.get("clarification_count", 0)),
        lambda r: f"{r['decision']} · conf {r['confidence']:.0%} vs threshold {r['threshold']:.0%}")
    out["pre_decision"] = pre
    planned = investigation["recommended_actions"] if pre["decision"] == "auto_resolve" else (
        [a for a in investigation["recommended_actions"] if a.get("protective")] if pre["decision"] == "escalate" else [])
    actions = []
    if planned:
        def run_actions():
            done = []
            for a in planned:
                done += action_agent.execute(store, a["action"], a["params"], ctx, conversation_id=conv["id"])
            return done
        actions = T.run("5 Action Execution", run_actions,
                        lambda r: ", ".join(f"{a['label']}={a['status']}" for a in r))
    decision = pre
    if actions:
        decision = T.run("7 Escalation Decision (post-action)", lambda: escalation_agent.decide(
            iu, investigation, ctx, issue, actions, thresholds, human_requested, conv.get("clarification_count", 0)),
            lambda r: f"{r['decision']}" + (" · escalated after failed action" if r["decision"] != pre["decision"] else ""))
    out.update(actions=actions, decision=decision)

    # ---- clarify ----------------------------------------------------------------------
    if decision["decision"] == "clarify":
        conv["clarification_count"] = conv.get("clarification_count", 0) + 1
        issue["missing_slots"] = investigation["missing_slots"]
        memory_agent.set_status(issue, "awaiting_customer", "Asked for " + ", ".join(investigation["missing_slots"]))
        reply = response_agent.compose(decision, iu, investigation, actions, ctx, None, issue, articles, conv["channel"])
        return finish(reply, ctx, decision="clarify", intent=issue["intent"], actions=actions)
    issue["missing_slots"] = []

    # ---- 8 ticket + 9 tracking -----------------------------------------------------------
    summary = memory_agent.summarize(conv, issue, investigation, actions)
    ticket = T.run("8 Ticket Generation", lambda: ticket_agent.create_or_update(store, conv, issue, iu, ctx, investigation, actions,
                                                                                 decision, summary, articles),
                   lambda r: f"{r['id']} · {r['status']} · {r['team']} · {r['priority']}")
    for a in actions:
        a["ticket_id"] = ticket["id"]
        store.put("actions", a["id"], a)
    if attachments:  # keep the customer's photos with the ticket for the human agent
        ticket["attachments"] = list(dict.fromkeys((ticket.get("attachments") or []) + attachments))
        store.put("tickets", ticket["id"], ticket)

    def track():
        sla = tracking_agent.sla_status(ticket)
        if decision["decision"] == "auto_resolve":
            msgs = [a["customer_message"] for a in actions if a["status"] == "success" and a.get("customer_message")]
            if msgs and ctx:
                action_agent.execute(store, "notify_customer", dict(customer_id=ctx["customer_id"], channel=conv["channel"],
                                     ticket_id=ticket["id"], message=f"{ticket['id']} resolved: " + " ".join(msgs), kind="resolution"),
                                     ctx, conversation_id=conv["id"])
            memory_agent.set_status(issue, "resolved", investigation["diagnosis"])
            conv["awaiting"], conv["awaiting_ticket"] = "csat", ticket["id"]
        else:
            memory_agent.set_status(issue, "escalated", f"{ticket['id']} → {ticket['team']}")
        return sla
    T.run("9 Resolution Tracking", track, lambda r: f"lifecycle {issue['status']} · SLA {r.get('state')}")
    out["ticket"] = ticket

    reply = response_agent.compose(decision, iu, investigation, actions, ctx, ticket, issue, articles, conv["channel"])
    if decision["decision"] == "auto_resolve":
        T.note("10 Customer Satisfaction", "CSAT requested")
        if not defer_csat:  # guided flows ask for the rating themselves, after the email step
            reply += "\n\n" + satisfaction_agent.CSAT_PROMPT
    if conv.get("contact_email"):
        out["email"] = T.run("Email Agent", lambda: email_agent.send_email(
            store, conv["contact_email"], "ticket", ticket["id"], reply,
            images=media.ticket_images(store, issue["intent"], issue.get("slots")), customer_name=(ctx or {}).get("first_name")),
            lambda r: f"{r['status']} → {r['to_masked']}")
    return finish(reply, ctx, decision=decision["decision"], ticket=ticket, intent=issue["intent"], actions=actions)


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
