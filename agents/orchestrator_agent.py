"""Agent 11: Master Orchestrator — the workflow that coordinates every agent, with a persisted run record.

One customer message flows through:
  Channel adapter → Understand (rules + LLM) → Memory (slot filling / lifecycle) →
  Customer context → Knowledge retrieval → Troubleshooting → Escalation
  (pre-action) → [customer confirmation, if the plan came from an LLM-only reading] →
  Authorize + execute actions → Escalation (post-action) → Ticket (+ approvals) →
  Resolution tracking → Response (LLM-written, fact-validated) → CSAT → Reviewer.

Each run is stored in ``workflow_runs`` with its steps, every tool call against the
company system, every LLM call (and whether the fallback was used) and the outcome.
Company data is reached only through the tenant Workspace (``agents/workspace.py``).
"""
from __future__ import annotations

import re
import time

from integrations.base import AdapterError

from . import (action_agent, approvals, channel_agent, context_agent, email_agent, escalation_agent, intent_agent,
               knowledge_agent, llm, media, memory_agent, response_agent, runlog, satisfaction_agent, ticket_agent,
               tracking_agent, troubleshooting_agent)
from .policy import SENSITIVE_ACTIONS
from .shared_agent import ALWAYS_ESCALATE, AUTO_REFUND_LIMIT, CHANNELS, COMPANY, INTENTS, iso
from .workspace import as_workspace

PHOTO_INTENTS = {"wrong_item", "damaged_item", "return_request"}
MAX_MESSAGE_CHARS = 4000
ACTION_LABELS = {k: v[1] for k, v in action_agent.ACTIONS.items()}


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


def review(reply, decision, actions, ticket, intent, ctx, refund_limit=AUTO_REFUND_LIMIT, intent_source="rules",
           confirmed=False):
    """Reviewer guardrail: verify the turn before anything reaches the customer."""
    checks = [
        ("Reply is non-empty", bool(reply and reply.strip())),
        ("Only successful actions are claimed",
         all(not a["message"] or a["message"] not in reply for a in actions if a["status"] in ("failed", "blocked"))),
        ("AI refunds within limit",
         all(a["data"].get("amount", 0) <= refund_limit for a in actions
             if a["action"] == "initiate_refund" and a["status"] == "success" and a["actor"].startswith("AI"))),
        ("No sensitive action on an unconfirmed LLM-only reading",
         intent_source != "llm" or confirmed or
         not any(a["action"] in SENSITIVE_ACTIONS and a["status"] == "success" for a in actions)),
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
                priority=issue.get("priority", understanding["priority"]), source=issue.get("source", understanding.get("source")))


def _clean_text(text):
    """Input validation: strip control characters and cap the length of a customer message."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or ""))
    return text.strip()[:MAX_MESSAGE_CHARS]


def _llm_summary(llm_calls, use_llm):
    if not use_llm:
        return dict(mode="off", reason="LLM switched off for this turn", calls=[])
    if not llm_calls:
        st = llm.status()
        return dict(mode="fallback" if st["mode"] != "llm" else "not needed",
                    reason=st["reason"] if st["mode"] != "llm" else "no LLM step needed for this turn", calls=[])
    ok = [c for c in llm_calls if c.get("ok")]
    bad = [c for c in llm_calls if not c.get("ok")]
    mode = "llm" if ok and not bad else ("partial" if ok else "fallback")
    reason = "; ".join(f"{c['purpose']}: {c.get('error')}" for c in bad) or "all LLM steps succeeded"
    return dict(mode=mode, reason=reason, model=llm_calls[0].get("model"),
                calls=[{k: c.get(k) for k in ("purpose", "ok", "error", "ms", "model")} for c in llm_calls])


def handle_message(store, text=None, conversation_id=None, customer_id=None, channel="web", payload=None, use_llm=True,
                   contact_email=None, attachments=None, defer_csat=False):
    """Process one inbound customer message end-to-end; the run is persisted in ``workflow_runs``."""
    ws = as_workspace(store)
    t0 = time.time()
    run = dict(id=f"RUN-{ws.next_seq('run', 1):06d}", tenant=ws.tenant["id"], channel=channel, started_at=iso(),
               status="running", adapter=ws.adapter.name, simulated=ws.adapter.simulated, conversation_id=conversation_id)
    with runlog.recording() as rec:
        try:
            out = _handle(ws, run, text, conversation_id, customer_id, channel, payload, use_llm, contact_email,
                          attachments, defer_csat)
        except AdapterError as exc:  # the company system is down: say so honestly, change nothing
            out = _degraded(ws, run, exc)
        except Exception as exc:
            run.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300], finished_at=iso(),
                       ms=round((time.time() - t0) * 1000, 1), tool_calls=rec.tool_calls,
                       llm=_llm_summary(rec.llm_calls, use_llm))
            run.pop("_conv", None)
            ws.put("workflow_runs", run["id"], run)
            raise
        out["llm"] = _llm_summary(rec.llm_calls, use_llm)
        dec = out.get("decision")
        run.pop("_conv", None)
        run.update(status="degraded" if out.get("degraded") else "completed", finished_at=iso(),
                   ms=round((time.time() - t0) * 1000, 1), conversation_id=out["conversation_id"],
                   customer_id=(out.get("context") or {}).get("customer_id"), message=out.get("inbound", {}).get("text", "")[:500],
                   intent=(out.get("issue") or out.get("understanding") or {}).get("intent"),
                   intent_source=(out.get("understanding") or {}).get("source"),
                   decision=dec["decision"] if isinstance(dec, dict) else dec, ticket_id=(out.get("ticket") or {}).get("id"),
                   action_ids=[a["id"] for a in out.get("actions", [])],
                   approval_ids=[a["data"]["approval_id"] for a in out.get("actions", []) if a["status"] == "pending_approval"],
                   reply=(out.get("reply") or "")[:2000], review=out.get("review"), steps=out.get("trace", []),
                   tool_calls=rec.tool_calls, llm=out["llm"], llm_calls=rec.llm_calls)
        ws.put("workflow_runs", run["id"], run)
    out["run_id"] = run["id"]
    return out


def _degraded(ws, run, exc):
    conv = run.get("_conv")
    reply = ("I'm sorry — I can't reach our order and account systems right now, so I can't check this safely. "
             "Nothing has been changed on your account. Please try again in a few minutes.")
    if conv:
        memory_agent.add_message(conv, "assistant", reply, dict(decision="system_unavailable", run_id=run["id"]))
        memory_agent.save(ws, conv)
    return dict(conversation_id=(conv or {}).get("id") or run.get("conversation_id"), channel=run["channel"], reply=reply,
                reply_channel=reply, decision="system_unavailable", degraded=True, error=str(exc), actions=[],
                understanding=None, issue=None, context=None, ticket=None, review=None, trace=[], conversation=conv,
                awaiting=None, inbound=dict(text=""), articles=[], attachments=[], llm_polished=False, email=None)


def _confirm_question(issue, planned, ctx):
    name = f", {ctx['first_name']}" if ctx and ctx.get("first_name") else ""
    labels = [ACTION_LABELS.get(a["action"], a["action"]) + (f" for {a['params']['order_id']}" if a["params"].get("order_id") else "")
              for a in planned if a["action"] != "notify_customer"]
    return (f"Before I change anything{name}: I understood this as **{issue.get('label', label(issue['intent']))}**. "
            "Here's what I'd do:\n" + "\n".join(f"- {x}" for x in labels) + "\n\nShall I go ahead? (yes / no)")


def _handle(ws, run, text, conversation_id, customer_id, channel, payload, use_llm, contact_email, attachments, defer_csat):
    store = ws
    T = Tracer()
    plan = ["channel", "understand", "memory", "context", "knowledge", "troubleshoot", "escalate?", "authorize+act", "escalate",
            "ticket", "track", "respond", "csat", "review"]
    T.note("Master Orchestrator (Planner)", "Plan: " + " → ".join(plan) + f" · company system: {ws.adapter.name}"
           + (" (simulated)" if ws.adapter.simulated else ""))

    inbound = T.run("0 Omnichannel Adapter",
                    lambda: channel_agent.normalize_inbound(channel, payload if payload is not None else {"text": text}),
                    lambda r: f"{CHANNELS.get(r['channel'], r['channel'])} · sender {r['sender'] or 'session'}")
    text = inbound["text"] = _clean_text(inbound["text"])
    if not text:
        raise ValueError("Empty message")
    conv = memory_agent.load_conversation(store, conversation_id, customer_id or inbound["sender"].get("customer_id"), inbound["channel"])
    run["_conv"], run["conversation_id"] = conv, conv["id"]
    conv["channel"] = inbound["channel"]
    if contact_email:  # an address the customer gave us: the Email agent sends ticket emails there
        conv["contact_email"] = contact_email
    attachments = list(attachments or [])
    memory_agent.add_message(conv, "user", text, dict(channel=inbound["channel"], attachments=attachments, run_id=run["id"]))
    out = dict(conversation_id=conv["id"], channel=inbound["channel"], inbound=inbound, understanding=None, issue=None,
               mode=None, context=None, articles=[], workflow=None, investigation=None, actions=[], decision=None,
               pre_decision=None, ticket=None, review=None, llm_polished=False, email=None, attachments=attachments)
    gate = dict(source="rules", confirmed=False)

    def finish(reply, ctx=None, decision=None, ticket=None, intent=None, actions=(), generated=False):
        polished, note = False, None
        if use_llm and not generated and decision in ("auto_resolve", "escalate", "clarify", "confirm"):
            reply, polished, note = response_agent.generate(reply, (out.get("understanding") or {}).get("emotion", "calm"), decision)
            T.note("Response Generation", ("LLM wrote the reply · " if polished else "deterministic reply · ") + (note or ""),
                   status="OK")
        rv = T.run("Reviewer (guardrail)", lambda: review(reply, decision, list(actions), ticket, intent, ctx,
                                                          ws.policy.auto_refund_limit, gate["source"], gate["confirmed"]),
                   lambda r: "approved" if r["approved"] else "rejected: " + ", ".join(r["rejected"]))
        if not rv["approved"]:
            reply = ("I want to make sure this is handled correctly, so I've passed your request to a specialist who will "
                     "contact you shortly." + (f" Your reference is {ticket['id']}." if ticket else ""))
        memory_agent.add_message(conv, "assistant", reply, dict(ticket_id=(ticket or {}).get("id"), decision=decision,
                                                                run_id=run["id"]))
        memory_agent.save(store, conv)
        out.update(reply=reply, reply_channel=channel_agent.format_outbound(
            conv["channel"], reply, (ctx or {}).get("first_name"), (ticket or {}).get("id"),
            (out.get("issue") or {}).get("label")), review=rv, trace=T.steps, llm_polished=polished or generated,
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

    # ---- confirmation of actions planned from an LLM-only reading ----------------
    confirmed_issue, pending = None, None
    if conv.get("awaiting") == "confirm_actions":
        pending = conv.pop("pending_confirmation", None) or {}
        conv["awaiting"] = None
        active = conv.get("active_issue")
        answer = intent_agent.yes_no(text)
        if active and active["id"] == pending.get("issue_id") and answer:
            if answer == "deny":
                memory_agent.set_status(active, "closed", "Customer declined the proposed actions")
                T.note("6 Conversation Memory", "customer declined the proposed actions — nothing executed")
                ctx = context_agent.customer_context(store, conv.get("customer_id"))
                out.update(understanding=pending.get("understanding"), issue=active, mode="declined")
                return finish("No problem — I haven't changed anything. Tell me what you'd like me to do instead, "
                              "or ask for a human agent.", ctx, decision="declined")
            active["customer_confirmed"] = True
            confirmed_issue = active

    # ---- 1 understand + 6 memory ----------------------------------------------
    if confirmed_issue:
        understanding = pending["understanding"]
        issue, mode = confirmed_issue, "confirmed"
        T.note("1 Conversation Understanding", f"customer confirmed: {issue['intent']} (understood earlier by {issue.get('source')})")
        T.note("6 Conversation Memory", f"confirmed · issue {issue['intent']} · turn {len(conv['messages'])}")
    else:
        active = conv.get("active_issue")
        understanding = T.run("1 Conversation Understanding",
                              lambda: intent_agent.understand(text, use_llm, context=(active or {}).get("label")),
                              lambda r: f"{r['intent']} · {r['category']} · {r['priority']} · {r['emotion']} · conf {r['confidence']:.0%}"
                                        f" · by {r.get('source', 'rules')}" + (f" (LLM: {r['llm']['status']})" if r.get("llm", {}).get("status") not in (None, "off") else ""))
        conv["sentiment_trail"].append(understanding["emotion"])
        issue, mode = T.run("6 Conversation Memory", lambda: memory_agent.track_issue(conv, understanding, text),
                            lambda r: f"{r[1]} · issue {r[0]['intent'] if r[0] else '—'} · turn {len(conv['messages'])}")
        if issue and mode == "follow_up" and understanding.get("source") in ("rules", "rules+llm"):
            issue["source"] = "rules+llm" if issue.get("source") == "llm" else issue.get("source", "rules")
    out.update(understanding=understanding, issue=issue, mode=mode)

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
            return finish(f"Hi{name}! 👋 I'm the {ws.tenant.get('name', COMPANY)} support assistant. "
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
    human_requested = intent == "human_agent" or bool(understanding.get("wants_human"))
    if issue is None:
        issue = memory_agent.open_issue(conv, understanding, text)
        out.update(issue=issue, mode="new")
    iu = _issue_understanding(understanding, issue, mode)
    gate.update(source=issue.get("source", iu.get("source", "rules")), confirmed=bool(issue.get("customer_confirmed")))
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

    # ---- 7 escalation (pre) → confirm? → 5 authorize + act → 7 escalation (post) -------------
    thresholds = satisfaction_agent.learned_thresholds(store)
    thresholds.update(ws.policy.threshold_overrides)  # human-approved analytics recommendations take precedence
    pre = T.run("7 Escalation Decision (pre-action)", lambda: escalation_agent.decide(
        iu, investigation, ctx, issue, None, thresholds, human_requested, conv.get("clarification_count", 0)),
        lambda r: f"{r['decision']} · conf {r['confidence']:.0%} vs threshold {r['threshold']:.0%}")
    out["pre_decision"] = pre
    recommended = investigation["recommended_actions"]
    planned = recommended if pre["decision"] == "auto_resolve" else (
        [a for a in recommended if a.get("protective") or a.get("approval")] if pre["decision"] == "escalate" else [])
    sensitive = [a for a in planned if a["action"] in SENSITIVE_ACTIONS]
    if (sensitive and ws.policy.confirm_llm_only_actions and gate["source"] == "llm" and not gate["confirmed"]
            and pre["decision"] == "auto_resolve"):
        T.note("5 Action Execution", "held: " + ", ".join(a["action"] for a in sensitive)
               + " planned from an LLM-only reading — asking the customer to confirm first")
        conv["awaiting"] = "confirm_actions"
        conv["pending_confirmation"] = dict(issue_id=issue["id"], understanding=iu, actions=[a["action"] for a in planned])
        memory_agent.set_status(issue, "awaiting_customer", "Asked the customer to confirm the proposed actions")
        out["decision"] = dict(pre, decision="confirm", reasons=["Sensitive action planned from an LLM-only reading"])
        return finish(_confirm_question(issue, planned, ctx), ctx, decision="confirm", intent=issue["intent"])
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
    waiting = [a["data"]["approval_id"] for a in actions if a["status"] == "pending_approval"]
    if waiting:
        approvals.link_ticket(store, waiting, ticket["id"])
        ticket["approvals"] = list(dict.fromkeys((ticket.get("approvals") or []) + waiting))
        ticket["human_status"] = f"Needs {ticket.get('team')} approval ({', '.join(waiting)})"
        ticket["recommended_next_step"] = (f"Review and approve or reject {', '.join(waiting)} in the Approvals queue; "
                                           "on approval SupportPilot runs the action and notifies the customer.")
        store.put("tickets", ticket["id"], ticket)
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

    kb_answer, generated = None, False
    if issue["intent"] == "general_inquiry" and articles and use_llm and decision["decision"] == "auto_resolve":
        kb_answer, note = response_agent.answer_from_kb(text, articles)
        T.note("3 Knowledge Retrieval", note)
    reply = response_agent.compose(decision, iu, investigation, actions, ctx, ticket, issue, articles, conv["channel"],
                                   kb_answer=kb_answer)
    if kb_answer:  # already written by the LLM from the articles and validated
        generated = True
    if decision["decision"] == "auto_resolve":
        T.note("10 Customer Satisfaction", "CSAT requested")
    if use_llm and not generated:
        reply, generated, note = response_agent.generate(reply, iu.get("emotion", "calm"), decision["decision"])
        T.note("Response Generation", ("LLM wrote the reply · " if generated else "deterministic reply · ") + (note or ""))
    if decision["decision"] == "auto_resolve" and not defer_csat:  # guided flows ask for the rating after the email step
        reply += "\n\n" + satisfaction_agent.CSAT_PROMPT
    if conv.get("contact_email"):
        out["email"] = T.run("Email Agent", lambda: email_agent.send_email(
            store, conv["contact_email"], "ticket", ticket["id"], reply,
            images=media.ticket_images(store, issue["intent"], issue.get("slots")), customer_name=(ctx or {}).get("first_name")),
            lambda r: f"{r['status']} → {r['to_masked']}")
    return finish(reply, ctx, decision=decision["decision"], ticket=ticket, intent=issue["intent"], actions=actions,
                  generated=True)  # generation (LLM or deterministic) already happened above


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
