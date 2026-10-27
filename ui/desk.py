"""Support desk: talk to SupportPilot as a customer (web chat) or watch the live Telegram bot."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import agents as A
from agent_flow import render_flow
from agents.shared_agent import fmt_ts

from .common import ACTION_ICON, customers, step_lines, theme, where, workspace

EXAMPLES = ["", "My order was delivered but I received the wrong item", "My payment failed but money was deducted",
            "I can't login, it says my account is locked", "Where is my order? It's late",
            "I paid for Premium but my subscription is still not activated", "My internet is not working since morning",
            "There's a transaction I did not make", "I want a refund for my laptop, order ORD12353",
            "What are the delivery charges for cash on delivery?", "I want to talk to a human"]


def _state():
    ss = st.session_state
    for k, v in dict(conv_id=None, customer_id=None, channel="web", turns=[], pending=None).items():
        ss.setdefault(k, v)
    return ss


def _new():
    st.session_state.conv_id, st.session_state.turns = None, []


def _on_customer():
    st.session_state.customer_id = st.session_state.desk_customer
    _new()


def _on_example():
    text = st.session_state.get("desk_example")
    if text:
        st.session_state.pending = text
        st.session_state.desk_example = ""


def _on_submit():
    if st.session_state.get("chat_box"):
        st.session_state.pending = st.session_state.chat_box


def _on_rating(key):
    value = st.session_state.get(key)
    if value is not None:
        st.session_state.pending = str(value + 1)


def _sources(store, turn):
    """Where this reply's facts came from: KB articles, company-system reads, actions, LLM."""
    run = store.get("workflow_runs", turn.get("run_id")) if turn.get("run_id") else None
    arts = [a for a in turn.get("articles", []) if a.get("audience", "customer") == "customer"][:3]
    if arts:
        st.markdown("**Knowledge used**")
        for a in arts:
            st.caption(f"📄 {a['id']} · {a['title']} — v{a.get('version', 1)} in database `kb_articles` "
                       f"(source `{a.get('source', '')}`)")
    reads = [c for c in (run or {}).get("tool_calls", []) if c["kind"] == "read"]
    if reads:
        st.markdown(f"**Company records read** · {store.adapter.name}" + (" (simulated)" if store.adapter.simulated else ""))
        st.dataframe(pd.DataFrame([dict(Tool=c["tool"].split(".", 1)[1], Query=", ".join(f"{k}={v}" for k, v in c["args"].items()),
                                        Result=c["status"] if c["status"] != "ok" else (
                                            f"{c['result'].get('count')} record(s)" if "count" in c["result"] else "found"))
                                   for c in reads]), hide_index=True, width="stretch", height=min(38 * (len(reads) + 1), 260))
    acts = [a for a in turn.get("actions", []) if a["action"] != "notify_customer"]
    if acts:
        st.markdown("**Actions**")
        for a in acts:
            au = a.get("authorization") or {}
            st.caption(f"{ACTION_ICON.get(a['status'], '•')} {a['label']} — {a['status']} · policy: {au.get('outcome')} "
                       f"({au.get('rule')}) · executed in {a.get('adapter')}{' (simulated)' if a.get('simulated') else ''}")
    lm = turn.get("llm") or {}
    st.caption(f"🧠 LLM: **{lm.get('mode', '—')}** — {lm.get('reason', '')} · saved as workflow run "
               f"**{turn.get('run_id', '—')}** in `workflow_runs`")


def _chat(store, use_llm):
    ss = _state()
    ids, labels = customers(store)
    c1, c2, c3, c4 = st.columns([2.2, 1.1, 2.6, 1.0], vertical_alignment="bottom")
    c1.selectbox("Customer", ids, format_func=lambda c: "Guest (not logged in)" if c is None else labels.get(c, c),
                 key="desk_customer", index=ids.index(ss.customer_id) if ss.customer_id in ids else 0, on_change=_on_customer,
                 help="Read from the company system's customers")
    c2.selectbox("Channel", list(A.CHANNELS), format_func=lambda c: A.CHANNELS[c], key="channel")
    c3.selectbox("Try an example", EXAMPLES, key="desk_example", on_change=_on_example,
                 format_func=lambda t: "Pick an example message…" if not t else t)
    c4.button("New chat", icon=":material/add_comment:", on_click=_new, width="stretch")

    error = None
    if ss.pending:
        text, ss.pending = ss.pending, None
        try:
            turn = A.handle_message(store, text, ss.conv_id, ss.customer_id, ss.channel, use_llm=use_llm)
            ss.conv_id = turn["conversation_id"]
            if turn["conversation"] and turn["conversation"].get("customer_id") and not ss.customer_id:
                ss.customer_id = turn["conversation"]["customer_id"]
            turn.pop("conversation", None)
            ss.turns.append(dict(text=text, result=turn))
        except Exception as exc:  # keep the desk usable and show the problem
            error = f"{type(exc).__name__}: {exc}"
    if error:
        st.error(error)
    conv = store.get("conversations", ss.conv_id) if ss.conv_id else None
    last = ss.turns[-1]["result"] if ss.turns else None

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        box = st.container(height=600, border=True)
        with box:
            msgs = (conv or {}).get("messages", [])
            if not msgs:
                st.caption("Write as the customer, or pick an example above. Conversations are saved in the support "
                           "database and can be continued over several messages.")
            for m in msgs:
                with st.chat_message("user" if m["role"] == "user" else "assistant",
                                     avatar=":material/person:" if m["role"] == "user" else ":material/support_agent:"):
                    st.markdown(m["text"])
                    meta = m.get("meta") or {}
                    if m["role"] == "assistant" and meta.get("ticket_id"):
                        st.caption(f"🎫 {meta['ticket_id']} · {meta.get('decision') or ''}")
        if conv and conv.get("awaiting") == "csat":
            k = f"fb_{conv['id']}_{len(conv['messages'])}"
            st.caption("Rate this support experience")
            st.feedback("stars", key=k, on_change=_on_rating, args=(k,))
        st.chat_input("Message SupportPilot…", key="chat_box", on_submit=_on_submit)
        if conv:
            where("support", f"conversations / {conv['id']}", store)
    with right:
        if not last:
            st.info("Each message is handled by the agent workflow. Its steps, the data it read and the actions it took "
                    "appear here.", icon=":material/psychology:")
            return
        render_flow(ss.turns[-1]["text"], last, theme=theme(), height=700, store=store)
        with st.expander("Where this answer came from", icon=":material/source:", expanded=True):
            _sources(store, last)
        with st.expander("Decision details", icon=":material/rule:"):
            u, d = last.get("understanding") or {}, last.get("decision")
            if u:
                st.markdown(f"**Understood:** {u.get('label', u.get('intent'))} · priority {u.get('priority')} · "
                            f"{u.get('emotion')} · confidence {u.get('confidence', 0):.0%} · read by {u.get('source', 'rules')}")
            if last.get("investigation"):
                step_lines(last["investigation"]["steps"])
                st.markdown(f"**Diagnosis:** {last['investigation']['diagnosis']}")
            if isinstance(d, dict):
                st.markdown(f"**Decision:** {d['decision']} — confidence {d['confidence']:.0%} vs threshold {d['threshold']:.0%}")
                for r in d.get("reasons", []):
                    st.caption("• " + r)
            if last.get("review"):
                st.caption("Reviewer: " + " · ".join(f"{'✅' if c['passed'] else '❌'} {c['check']}" for c in last["review"]["checks"]))
            if last.get("ticket"):
                st.markdown(A.render_ticket(store.get("tickets", last["ticket"]["id"]) or last["ticket"]))


TG_SETUP = """
**Connect a Telegram bot:** create one with **@BotFather** (`/newbot`), put the token in `.env` or Streamlit secrets as
`TELEGRAM_BOT_TOKEN`, and restart the app. Customers then chat with SupportPilot from the Telegram app.
"""


def _telegram(store, bot):
    @st.fragment(run_every="3s")
    def live():
        if not bot:
            st.info("No Telegram bot connected.", icon=":material/link_off:")
            st.markdown(TG_SETUP)
            return
        status = store.get("meta", "telegram") or {}
        user = status.get("username") or bot.username
        with st.container(horizontal=True, vertical_alignment="center"):
            if status.get("running") and user:
                st.badge(f"Connected · @{user}", icon=":material/check_circle:", color="green")
                st.link_button("Open bot in Telegram", f"https://t.me/{user}", icon=":material/open_in_new:")
            else:
                st.badge("Connecting…" if not status.get("error") else "Not connected", color="orange")
            if status.get("last_poll"):
                st.caption(f"Last checked {fmt_ts(status['last_poll'])}")
        if status.get("error"):
            st.caption(f"⚠️ Last error: {status['error']}")
        chats = sorted(store.list("telegram_chats"), key=lambda c: c.get("updated_at", ""), reverse=True)
        if not chats:
            st.info(f"Open t.me/{user or 'your_bot'}, press Start and describe an issue — it appears here live.",
                    icon=":material/smartphone:")
            return

        def label(c):
            who = (store.get("customers", c["customer_id"]) or {}).get("name") if c.get("customer_id") else "guest"
            return f"{c.get('first_name', 'Telegram user')} (as {who})"
        left, right = st.columns([1, 1.15], gap="large")
        with left:
            chat = st.selectbox("Telegram chat", chats, format_func=label)
            conv = store.get("conversations", chat["conv_id"]) if chat.get("conv_id") else None
            with st.container(height=560, border=True):
                for m in (conv or {}).get("messages", []):
                    with st.chat_message("user" if m["role"] == "user" else "assistant",
                                         avatar=":material/smartphone:" if m["role"] == "user" else ":material/support_agent:"):
                        st.markdown(m["text"])
            where("support", "telegram_chats, conversations", store)
        with right:
            turns = sorted(store.list("telegram_turns", chat_id=chat["chat_id"]), key=lambda t: t["seq"])
            if turns:
                turn = st.selectbox("Message", turns[::-1], format_func=lambda t: f"{fmt_ts(t['ts'])} · {t['text'][:60]}")
                render_flow(turn["text"], turn["result"], theme=theme(), height=760, store=store, ts=turn["ts"])
    live()


def page():
    store = workspace()
    st.title("Support desk")
    use_llm = st.session_state.get("use_llm", A.llm_enabled())
    web, tg = st.tabs([":material/forum: Web chat", ":material/send: Telegram"])
    with web:
        _chat(store, use_llm)
    with tg:
        _telegram(store, st.session_state.get("_tg_bot"))
