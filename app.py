import json

import pandas as pd
import plotly.express as px
import streamlit as st

import agents as A
from agent_flow import render_flow
from agents.email_agent import email_configured
from telegram_bot import start_in_background as start_telegram
from agents.shared_agent import fmt_ts, humanize_minutes

st.set_page_config(page_title="SupportPilot - Agentic Customer Support", page_icon=":material/support_agent:", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem;}
.sp-subtitle {color: var(--text-color); opacity: .78; margin-top: 0; margin-bottom: 1.25rem;}
[data-testid="stMetric"] {background: var(--secondary-background-color); border: 1px solid rgba(128, 128, 128, .28); border-radius: 12px; padding: .75rem;}
[data-testid="stMetricLabel"], [data-testid="stMetricValue"], [data-testid="stMetricDelta"] {color: var(--text-color) !important;}
[data-testid="stDataFrame"] {border: 1px solid rgba(128, 128, 128, .28); border-radius: 10px;}
.sp-card-title {color: var(--text-color); font-size: 1.05rem; font-weight: 650; margin-bottom: .5rem;}
.sp-muted {opacity: .7; font-size: .85rem;}
</style>
""", unsafe_allow_html=True)

# Reference categorical palette (fixed order, one hue per ticket category) — see dataviz palette.
CATEGORY_ORDER = ["Billing", "Orders", "Account", "Subscription", "Connectivity", "Security", "Legal", "General"]
CATEGORY_COLORS = dict(zip(CATEGORY_ORDER, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]))
SERIES_1 = "#2a78d6"
STEP_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "info": "ℹ️"}
PRIORITY_COLOR = {"critical": "red", "high": "orange", "medium": "blue", "low": "gray"}
SLA_ICON = {"ok": "🟢 on track", "at_risk": "🟠 at risk", "breached": "🔴 breached", "met": "✅ met", "missed": "❌ missed", "n/a": "—"}
PERSONAS = {
    None: "Guest (not logged in)",
    "CUST1001": "Aarav Sharma · Premium — wrong item delivered",
    "CUST1002": "Priya Nair · Plus — payment failed, money deducted",
    "CUST1003": "Rohan Mehta · Standard — account locked",
    "CUST1004": "John Doe · Premium — subscription sync failure",
    "CUST1005": "Sneha Iyer · Premium — subscription pending (fixable)",
    "CUST1006": "Vikram Singh · Standard — NovaFiber area outage",
    "CUST1007": "Ananya Gupta · Plus — NovaFiber hardware fault",
    "CUST1008": "Karan Malhotra · Standard — suspicious transaction",
    "CUST1009": "Meera Reddy · Premium — late delivery / cancellable order",
    "CUST1010": "Arjun Verma · Standard — charged twice",
    "CUST1011": "Fatima Khan · Plus — refund in progress",
    "CUST1012": "Dev Patel · Premium — damaged ₹54,999 laptop",
}
SCENARIOS = [
    ("Wrong item → auto replacement", "CUST1001", "My order was delivered but I received the wrong item"),
    ("Payment failed (multi-turn)", "CUST1002", "My payment failed but money was deducted"),
    ("Locked login → unlock", "CUST1003", "I can't login, it says my account is locked"),
    ("Premium not activated → escalation", "CUST1004", "I paid for Premium yesterday but my subscription is still not activated"),
    ("Premium not activated → auto-fix", "CUST1005", "My premium membership is not activated even after payment"),
    ("Area outage", "CUST1006", "My internet is not working since morning"),
    ("Hardware fault → technician", "CUST1007", "Internet keeps disconnecting and speed is slow"),
    ("Fraud report → security hold", "CUST1008", "There's a ₹18,499 transaction I did not make. Is this fraud?"),
    ("Late delivery (Premium)", "CUST1009", "Where is my order? It's 2 days late"),
    ("Cancel order", "CUST1009", "Please cancel my order ORD12352"),
    ("Charged twice", "CUST1010", "I was charged twice for my order"),
    ("Refund status", "CUST1011", "Where is my refund?"),
    ("High-value damage → approval", "CUST1012", "My laptop arrived damaged, the screen is cracked"),
    ("Angry + wants human", "CUST1009", "This is the WORST service, my order is late AGAIN!!! I want to talk to a human"),
    ("Legal / data deletion", "CUST1001", "Delete my data under the DPDP act or I will send a legal notice"),
    ("FAQ as a guest", None, "What are the delivery charges for cash on delivery?"),
]


@st.cache_resource
def get_store(code_version):
    return A.Store()


# Keyed on the Store class: when a deploy hot-reloads agents/store.py, the class object changes and a fresh
# Store (same database file) is created, instead of reusing an instance built from the old code.
store = get_store(id(A.Store))


# one long-polling bot per server process (start_telegram de-duplicates); None until TELEGRAM_BOT_TOKEN is set
tg_bot = start_telegram(store, use_llm=A.llm_enabled())
ss = st.session_state
ss.setdefault("conv_id", None)
ss.setdefault("customer_id", None)
ss.setdefault("channel", "web")
ss.setdefault("turns", [])
ss.setdefault("pending", None)
ss.setdefault("persona_select", None)


def new_conversation():
    ss.conv_id, ss.turns = None, []


def queue(text):
    ss.pending = text


def on_chat_submit():
    if ss.get("chat_box"):
        queue(ss.chat_box)


def on_persona_change():
    ss.customer_id = ss.persona_select
    new_conversation()


def run_scenario(cid, text):
    ss.customer_id = cid
    ss.persona_select = cid
    new_conversation()
    queue(text)


def on_rating(key):
    value = ss.get(key)
    if value is not None:
        queue(str(value + 1))


# ---- sidebar -------------------------------------------------------------------------------
with st.sidebar:
    st.title("SupportPilot")
    st.caption("Autonomous customer support · PS-04")
    st.success("11-agent workflow · MCP ready", icon=":material/check_circle:")
    st.selectbox("Customer (simulated login)", list(PERSONAS), format_func=lambda c: PERSONAS[c], key="persona_select",
                 on_change=on_persona_change)
    st.selectbox("Channel", list(A.CHANNELS), format_func=lambda c: A.CHANNELS[c], key="channel")
    st.button("New conversation", icon=":material/add_comment:", on_click=new_conversation, width="stretch")
    with st.expander("Demo scenarios", icon=":material/play_circle:", expanded=True):
        for i, (label, cid, text) in enumerate(SCENARIOS):
            st.button(label, key=f"sc{i}", on_click=run_scenario, args=(cid, text), width="stretch")
    use_llm = st.toggle("Groq LLM polish", value=A.llm_enabled(), disabled=not A.llm_enabled(),
                        help="Optional. Set GROQ_API_KEY in .env or .streamlit/secrets.toml. All decisions stay deterministic.")
    st.caption("📧 Email agent: ready (Gmail SMTP)" if email_configured() else
               "📧 Email agent: not configured — set SMTP_USER and SMTP_PASSWORD")
    if not A.llm_enabled():
        st.caption("🧠 Deterministic mode. Optional: set GROQ_API_KEY for LLM tie-breaking and reply polish.")
    with st.popover("Reset demo data", icon=":material/restart_alt:", width="stretch"):
        st.write("Wipe tickets, conversations and actions and re-seed the synthetic world (timestamps anchored to now).")
        if st.button("Confirm reset", type="primary"):
            store.reset()
            new_conversation()
            st.rerun()

# ---- process a pending message before anything renders, so every panel is up to date ------------
error = None
if ss.pending:
    text, ss.pending = ss.pending, None
    try:
        turn = A.handle_message(store, text, ss.conv_id, ss.customer_id, ss.channel, use_llm=use_llm)
        ss.conv_id = turn["conversation_id"]
        if turn["conversation"].get("customer_id") and not ss.customer_id:
            ss.customer_id = turn["conversation"]["customer_id"]
        turn.pop("conversation", None)
        ss.turns.append(dict(text=text, result=turn))
    except Exception as exc:  # keep the demo alive and show the problem
        error = f"{type(exc).__name__}: {exc}"

last = ss.turns[-1]["result"] if ss.turns else None
conv = store.get("conversations", ss.conv_id) if ss.conv_id else None

st.header("Agentic Customer Support")
st.title("SupportPilot")
st.markdown('<div class="sp-subtitle">Understand → gather context → investigate → act → resolve or escalate with a complete '
            'ticket → track → learn from CSAT. Deterministic agents, optional LLM, exposed over MCP.</div>', unsafe_allow_html=True)
if error:
    st.error(error)

tabs = st.tabs([":material/forum: Customer chat", ":material/send: Telegram live", ":material/account_tree: Agent trace", ":material/confirmation_number: Tickets",
                ":material/monitoring: Supervisor dashboard", ":material/menu_book: Knowledge base", ":material/hub: Omnichannel",
                ":material/schema: Architecture & MCP"])


def step_lines(steps):
    for s in steps:
        st.markdown(f"{STEP_ICON.get(s['status'], '•')} **{s['check']}** — {s['detail']}")


THEME = getattr(st.context.theme, "type", None) or "light"


# ---- 1 chat ------------------------------------------------------------------------------------------
with tabs[0]:
    left, right = st.columns([1.25, 1], gap="large")
    with left:
        who = PERSONAS[ss.customer_id].split(" — ")[0]
        st.markdown(f'<div class="sp-card-title">{A.CHANNELS[ss.channel]} · {who}</div>', unsafe_allow_html=True)
        box = st.container(height=560, border=True)
        with box:
            msgs = (conv or {}).get("messages", [])
            if not msgs:
                st.caption("Start typing, or pick a demo scenario in the sidebar. Try a multi-turn flow: “My payment failed” "
                           "→ the agent asks for the Order ID → reply “ORD12346”.")
            for m in msgs:
                with st.chat_message("user" if m["role"] == "user" else "assistant",
                                     avatar=":material/person:" if m["role"] == "user" else ":material/support_agent:"):
                    st.markdown(m["text"])
                    tid = (m.get("meta") or {}).get("ticket_id")
                    if tid and m["role"] == "assistant":
                        st.caption(f"🎫 {tid} · {(m['meta'] or {}).get('decision') or ''}")
        if conv and conv.get("awaiting") == "csat":
            k = f"fb_{conv['id']}_{len(conv['messages'])}"
            st.caption("Rate this support experience")
            st.feedback("stars", key=k, on_change=on_rating, args=(k,))
        st.chat_input("Describe your issue…", key="chat_box", on_submit=on_chat_submit)

    with right:
        if not last:
            st.info("Send a message and watch every agent pick it up, stage by stage, right here.", icon=":material/psychology:")
        else:
            render_flow(ss.turns[-1]["text"], last, theme=THEME, height=860, store=store)
            with st.expander("Detailed agent outputs", icon=":material/data_object:"):
                u = last.get("understanding")
                d = last.get("decision")
                ctx = last.get("context")
                if u:
                    with st.container(border=True):
                        st.markdown('<div class="sp-card-title">1 · Conversation understanding</div>', unsafe_allow_html=True)
                        with st.container(horizontal=True):
                            st.badge(u["intent"], color="violet")
                            st.badge(u["category"], color="blue")
                            st.badge(f"priority {u['priority']}", color=PRIORITY_COLOR.get(u["priority"], "gray"))
                            st.badge(f"{u['emotion']} ({u['sentiment_score']:+.2f})",
                                     color="red" if u["emotion"] == "angry" else ("orange" if u["sentiment"] == "negative" else "green"))
                            st.badge(f"conf {u['confidence']:.0%}", color="gray")
                        st.code(json.dumps({"intent": u["intent"], "category": u["category"], "priority": u["priority"],
                                            "sentiment": u["sentiment"], "entities": u["entities"]}), language="json")
                        if last.get("issue"):
                            iss = last["issue"]
                            st.caption(f"Memory: **{last['mode']}** · issue {iss['intent']} · status **{iss['status']}** · "
                                       f"slots {iss.get('slots') or '{}'}")
                if ctx:
                    with st.container(border=True):
                        st.markdown('<div class="sp-card-title">2 · Customer context</div>', unsafe_allow_html=True)
                        st.markdown(f"**{ctx['name']}** — {ctx['headline']}")
                        if ctx["flags"]:
                            st.caption(" · ".join(ctx["flags"]))
                if last.get("investigation"):
                    inv = last["investigation"]
                    with st.container(border=True):
                        st.markdown('<div class="sp-card-title">3–4 · Knowledge & troubleshooting</div>', unsafe_allow_html=True)
                        if last.get("articles"):
                            st.caption("KB: " + " · ".join(f"{a['id']} {a['title']} ({a['relevance']:.0%})" for a in last["articles"][:3]))
                        if last.get("workflow"):
                            st.caption(f"Selected workflow: **{last['workflow']['title']}** (reliability {last['workflow']['reliability']:.0%})")
                        step_lines(inv["steps"])
                        st.markdown(f"**Diagnosis:** {inv['diagnosis']}")
                if last.get("actions"):
                    with st.container(border=True):
                        st.markdown('<div class="sp-card-title">5 · Actions executed</div>', unsafe_allow_html=True)
                        for a in last["actions"]:
                            icon = {"success": "✅", "skipped": "↩️", "blocked": "⛔", "failed": "❌"}[a["status"]]
                            st.markdown(f"{icon} **{a['label']}** ({a['status']}) — {a['message']}")
                if isinstance(d, dict):
                    with st.container(border=True):
                        st.markdown('<div class="sp-card-title">7 · Escalation decision</div>', unsafe_allow_html=True)
                        label = {"auto_resolve": "✅ Auto-resolved", "escalate": "🧑‍💼 Escalated", "clarify": "💬 Clarifying"}[d["decision"]]
                        st.markdown(f"**{label}** — confidence **{d['confidence']:.0%}** vs threshold **{d['threshold']:.0%}**"
                                    + (" (angry customer → stricter)" if d.get("sentiment_adjusted") else ""))
                        st.progress(min(d["confidence"], 1.0))
                        for r in d["reasons"]:
                            st.caption(f"• {r}")
                if last.get("ticket"):
                    t = last["ticket"]
                    with st.container(border=True):
                        st.markdown('<div class="sp-card-title">8–9 · Ticket & tracking</div>', unsafe_allow_html=True)
                        st.markdown(f"🎫 **{t['id']}** — {t['title']} · **{t['status']}** · {t.get('assignee')} ({t.get('team')})")
                        with st.expander("Full ticket (what the human agent sees)"):
                            st.markdown(A.render_ticket(store.get("tickets", t["id"]) or t))
                if last.get("reply_channel") and ss.channel != "web":
                    with st.expander(f"Reply as delivered on {A.CHANNELS[ss.channel]}"):
                        st.code(last["reply_channel"], language=None, wrap_lines=True)

# ---- Telegram live -------------------------------------------------------------------------------------
TG_SETUP = """
**Connect a real Telegram bot (≈2 minutes):**
1. In Telegram, open **@BotFather** → send `/newbot` → choose a name and a username ending in `bot`.
2. Copy the token BotFather gives you.
3. Add it to this app's secrets — Streamlit Cloud: *⋮ → Settings → Secrets*; locally: `.env`:
   ```toml
   TELEGRAM_BOT_TOKEN = "123456:ABC..."
   ```
4. Restart the app, open your bot on your phone and press **Start**.
"""


@st.fragment(run_every="3s")
def telegram_live():
    if not tg_bot:
        st.info("No Telegram bot connected yet.", icon=":material/link_off:")
        st.markdown(TG_SETUP)
        return
    status = store.get("meta", "telegram") or {}
    user = status.get("username") or tg_bot.username
    with st.container(horizontal=True, vertical_alignment="center"):
        if status.get("running") and user:
            st.badge(f"Connected · @{user}", icon=":material/check_circle:", color="green")
            st.link_button("Open bot in Telegram", f"https://t.me/{user}", icon=":material/open_in_new:")
        else:
            st.badge("Connecting…" if not status.get("error") else "Not connected", color="orange")
        if status.get("last_poll"):
            st.caption(f"Last checked {fmt_ts(status['last_poll'])} · refreshes every 3 s")
    if status.get("error"):
        st.caption(f"⚠️ Last error: {status['error']}")
    chats = sorted(store.list("telegram_chats"), key=lambda c: c.get("updated_at", ""), reverse=True)
    if not chats:
        st.info(f"Open **t.me/{user or 'your_bot'}** on your phone, press **Start** and describe an issue — "
                "the conversation and the agents' work appear here live.", icon=":material/smartphone:")
        return
    st.caption("Messages shown here are visible to anyone who opens this website.")

    def chat_label(c):
        who = (store.get("customers", c["customer_id"]) or {}).get("name") if c.get("customer_id") else "guest"
        return f"{c.get('first_name', 'Telegram user')} (as {who})"

    left, right = st.columns([1, 1.15], gap="large")
    with left:
        chat = st.selectbox("Telegram chat", chats, format_func=chat_label)
        conv = store.get("conversations", chat["conv_id"]) if chat.get("conv_id") else None
        box = st.container(height=560, border=True)
        with box:
            msgs = (conv or {}).get("messages", [])
            if not msgs:
                st.caption("Waiting for the first message from this chat…")
            for m in msgs:
                with st.chat_message("user" if m["role"] == "user" else "assistant",
                                     avatar=":material/smartphone:" if m["role"] == "user" else ":material/support_agent:"):
                    st.markdown(m["text"])
                    tid = (m.get("meta") or {}).get("ticket_id")
                    if tid and m["role"] == "assistant":
                        st.caption(f"🎫 {tid} · {(m['meta'] or {}).get('decision') or ''}")
    with right:
        turns = sorted(store.list("telegram_turns", chat_id=chat["chat_id"]), key=lambda t: t["seq"])
        if not turns:
            st.info("The agents' live flow appears here after the next message.", icon=":material/psychology:")
            return
        # no widget key: when a new message arrives the options change and the newest turn is selected again
        turn = st.selectbox("Message", turns[::-1], format_func=lambda t: f"{fmt_ts(t['ts'])} · {t['text'][:60]}")
        render_flow(turn["text"], turn["result"], theme=THEME, height=860, store=store, ts=turn["ts"])


with tabs[1]:
    telegram_live()


# ---- 2 trace -------------------------------------------------------------------------------------------
FLOW = [("0", "Omnichannel\nadapter"), ("1", "Understanding"), ("6", "Memory"), ("2", "Customer\ncontext"), ("3", "Knowledge"),
        ("4", "Troubleshooting"), ("7", "Escalation"), ("5", "Action\nexecution"), ("8", "Ticket"), ("9", "Tracking"),
        ("10", "Satisfaction"), ("R", "Reviewer")]


def flow_dot(trace):
    ran = set()
    for s in trace or []:
        head = s["agent"].split(" ")[0]
        ran.add("R" if s["agent"].startswith("Reviewer") else head)
    nodes = []
    for key, name in FLOW:
        on = key in ran
        style = 'style="rounded,filled", fillcolor="#2a78d6", fontcolor="white", color="#2a78d6"' if on else \
            'style="rounded,dashed", color="#8a8a86", fontcolor="#8a8a86"'
        nodes.append(f'n{key} [label="{name}", shape=box, {style}];')
    edges = " -> ".join(f"n{k}" for k, _ in FLOW)
    return ('digraph G { rankdir=LR; bgcolor="transparent"; node [fontname="Helvetica", fontsize=11]; '
            'edge [color="#8a8a86"]; ' + " ".join(nodes) + f" {edges}; }}")


with tabs[2]:
    if not ss.turns:
        st.info("Send a message in Customer chat — then pick any turn here to replay how the agents handled it.")
    else:
        idx = st.selectbox("Turn", range(len(ss.turns)), index=len(ss.turns) - 1,
                           format_func=lambda i: f"{i + 1}. {ss.turns[i]['text'][:80]}")
        r = ss.turns[idx]["result"]
        render_flow(ss.turns[idx]["text"], r, theme=THEME, height=860, store=store)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="sp-card-title">Reviewer guardrail</div>', unsafe_allow_html=True)
            for c in (r.get("review") or {}).get("checks", []):
                st.markdown(f"{'✅' if c['passed'] else '❌'} {c['check']}")
        with c2:
            if isinstance(r.get("decision"), dict):
                st.markdown('<div class="sp-card-title">Escalation rules evaluated</div>', unsafe_allow_html=True)
                for h in r["decision"]["rule_hits"]:
                    st.markdown(f"{'🔺' if h['triggered'] else '▫️'} {h['rule']}" + (f" — {h['detail']}" if h["triggered"] and h["detail"] else ""))
        with st.expander("Pipeline map and raw timings", icon=":material/table:"):
            st.graphviz_chart(flow_dot(r["trace"]), width="stretch")
            st.caption("Blue = agents that ran on this turn (the planner skips agents the turn does not need).")
            st.dataframe(pd.DataFrame(r["trace"]), hide_index=True, width="stretch",
                         column_config={"ms": st.column_config.NumberColumn("ms", format="%.1f")})
        with st.expander("Structured turn output (same JSON the MCP server returns)"):
            st.json(A.summarize_turn(dict(r, conversation={})), expanded=False)

# ---- 3 tickets -------------------------------------------------------------------------------------------
with tabs[3]:
    tickets = store.list("tickets")
    f1, f2, f3, f4 = st.columns([2, 1.3, 1.3, 1])
    statuses = f1.multiselect("Status", A.STATUS_FLOW, default=["Opened", "Assigned", "Pending Customer", "Resolved"])
    teams = f2.multiselect("Team", sorted({t.get("team") or "—" for t in tickets}))
    prios = f3.multiselect("Priority", ["critical", "high", "medium", "low"])
    live_only = f4.toggle("Live only", help="Hide the 21-day synthetic history")
    rows = [t for t in tickets if (not statuses or t["status"] in statuses) and (not teams or (t.get("team") or "—") in teams)
            and (not prios or t["priority"] in prios) and (not live_only or not t.get("historical"))]
    rows.sort(key=lambda t: t["created_at"], reverse=True)
    df = pd.DataFrame([dict(Ticket=t["id"], Title=t["title"], Customer=t["customer"]["name"], Tier=t["customer"].get("tier"),
                            Priority=t["priority"], Status=t["status"], Team=t.get("team"), Owner=t.get("assignee"),
                            Channel=A.CHANNELS.get(t.get("channel"), t.get("channel")), SLA=SLA_ICON[A.sla_status(t).get("state", "n/a")],
                            Created=fmt_ts(t["created_at"])) for t in rows[:400]])
    st.caption(f"{len(rows)} tickets · select a row to open the human-agent workspace")
    ev = st.dataframe(df, hide_index=True, width="stretch", height=320, on_select="rerun", selection_mode="single-row", key="tkt_table")
    sel = ev.selection.rows if ev and ev.selection else []
    if sel:
        t = store.get("tickets", df.iloc[sel[0]]["Ticket"])
        st.divider()
        a, b = st.columns([1.6, 1], gap="large")
        with a:
            st.markdown(A.render_ticket(t))
            if t.get("transcript"):
                with st.expander("Full transcript (optional — the summary above is designed to replace it)"):
                    for m in t["transcript"]:
                        st.markdown(f"**{'Customer' if m['role'] == 'user' else 'SupportPilot'}:** {m['text']}")
        with b:
            s = A.sla_status(t)
            st.metric("SLA", SLA_ICON[s.get("state", "n/a")],
                      f"{humanize_minutes(s['minutes_left'])} {'left' if s['minutes_left'] >= 0 else 'overdue'} ({s['target']})"
                      if "minutes_left" in s else None, delta_color="off", border=True)
            st.markdown('<div class="sp-card-title">Lifecycle</div>', unsafe_allow_html=True)
            for e in t.get("events", []):
                st.caption(f"{fmt_ts(e['ts'])} · **{e['status']}** · {e['actor']}" + (f" — {e['note']}" if e.get("note") else ""))
            allowed = sorted(A.TRANSITIONS.get(t["status"], []))
            if allowed:
                with st.form(f"upd_{t['id']}"):
                    st.markdown('<div class="sp-card-title">Update ticket (human agent)</div>', unsafe_allow_html=True)
                    target = st.selectbox("Move to", ["Reopened" if x == "Opened" and t["status"] == "Resolved" else x for x in allowed])
                    note = st.text_input("Note to customer")
                    resolution = st.text_area("Resolution note (when resolving — may become a new KB article)")
                    if st.form_submit_button("Apply", type="primary"):
                        try:
                            nt = A.transition(store, t["id"], target, actor="Human Agent", note=note or resolution)
                            if target == "Resolved" and resolution.strip():
                                nt["resolution"] = resolution
                                store.put("tickets", nt["id"], nt)
                                kb = A.expand_kb(store, nt, resolution)
                                st.session_state["kb_msg"] = (f"📚 New KB article {kb['article']['id']} created (novelty {kb['novelty']:.0%})"
                                                              if kb["created"] else f"KB not expanded: {kb['reason']}")
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
            if st.session_state.get("kb_msg"):
                st.success(st.session_state.pop("kb_msg"))
            notes = sorted(store.list("notifications", ticket_id=t["id"]), key=lambda n: n["created_at"])
            if notes:
                st.markdown('<div class="sp-card-title">Customer notifications sent</div>', unsafe_allow_html=True)
                for n in notes:
                    st.caption(f"{fmt_ts(n['created_at'])} · {A.CHANNELS.get(n['channel'], n['channel'])}: {n['message']}")

# ---- 4 supervisor dashboard ------------------------------------------------------------------------------
with tabs[4]:
    m = A.supervisor_metrics(store)
    cs = A.csat_summary(store)
    with st.container(horizontal=True):
        st.metric("Open tickets", m["open"], border=True)
        st.metric("Resolution rate", f"{m['resolution_rate']:.0%}", border=True)
        st.metric("Resolved by AI", f"{m['automation_rate']:.0%}", border=True, help="Share of all tickets resolved without a human")
        st.metric("Escalation rate", f"{m['escalation_rate']:.0%}", border=True)
        st.metric("Avg resolution", humanize_minutes(m["avg_resolution_min"] or 0), border=True,
                  help=f"AI {humanize_minutes(m['avg_resolution_ai_min'] or 0)} · Human {humanize_minutes(m['avg_resolution_human_min'] or 0)}")
        st.metric("CSAT", f"{cs['avg']:.2f}/5" if cs["avg"] else "—", f"{cs['csat_pct']:.0%} satisfied" if cs["csat_pct"] else None,
                  delta_color="off", border=True)
        st.metric("SLA at risk / breached", f"{m['sla_at_risk']} / {m['sla_breached']}", border=True)

    incidents = A.detect_incidents(store)
    if incidents:
        st.markdown('<div class="sp-card-title">Root cause discovery</div>', unsafe_allow_html=True)
        for inc in incidents:
            with st.container(border=True):
                sev = "🔴 CRITICAL" if inc["severity"] == "critical" else "🟠 HIGH"
                stats = (f"{inc['recent_count']} tickets in 24h · {inc['spike_ratio']}× baseline · {inc['dominant_share']:.0%} on "
                         f"{inc['dominant_value']}") if inc["spike_ratio"] else f"{inc['affected_customers']} lines affected"
                st.markdown(f"**{sev} · {inc['hypothesis']}**  \n{stats}  \n➡️ {inc['recommended_action']}")

    c1, c2 = st.columns([1.6, 1], gap="large")
    with c1:
        vol = pd.DataFrame(A.daily_volume(store))
        if not vol.empty:
            fig = px.bar(vol, x="date", y="tickets", color="category", category_orders={"category": CATEGORY_ORDER},
                         color_discrete_map=CATEGORY_COLORS, title="Tickets per day by category")
            fig.update_traces(marker_line_width=1, marker_line_color="rgba(0,0,0,0)")
            fig.update_layout(bargap=0.25, legend_title_text="", margin=dict(t=50, l=10, r=10, b=10), xaxis_title=None,
                              yaxis_title=None, hovermode="x unified")
            st.plotly_chart(fig, width="stretch")
    with c2:
        st.markdown('<div class="sp-card-title">SLA monitor</div>', unsafe_allow_html=True)
        flagged = A.sla_scan(store, auto_escalate=False)
        if flagged:
            st.dataframe(pd.DataFrame([dict(Ticket=f["ticket_id"], Tier=f["tier"], Priority=f["priority"], State=SLA_ICON[f["state"]],
                                            Left=humanize_minutes(f["minutes_left"]) + (" over" if f["minutes_left"] < 0 else ""),
                                            Owner=f["assignee"]) for f in flagged]), hide_index=True, width="stretch")
        else:
            st.success("No open ticket is at SLA risk.", icon=":material/verified:")
        if st.button("Run SLA monitor & auto-escalate", icon=":material/notification_important:"):
            done = [f for f in A.sla_scan(store) if f["escalated_now"]]
            st.toast(f"Auto-escalated {len(done)} ticket(s) to the Escalation Desk" if done else "Nothing new to escalate")
        st.caption("Premium high-priority: 15-min response. At risk = ≥75% of the deadline used.")

    c3, c4, c5 = st.columns(3, gap="large")
    with c3:
        bi = pd.DataFrame([dict(intent=A.INTENTS[k]["label"], avg=v["avg"], n=v["n"]) for k, v in cs["by_intent"].items() if v["n"] >= 3])
        if not bi.empty:
            fig = px.bar(bi.sort_values("avg"), x="avg", y="intent", orientation="h", title="CSAT by issue type (n≥3)",
                         hover_data={"n": True}, color_discrete_sequence=[SERIES_1])
            fig.update_layout(margin=dict(t=50, l=10, r=10, b=10), xaxis=dict(range=[0, 5], title=None), yaxis_title=None, bargap=0.3)
            st.plotly_chart(fig, width="stretch")
    with c4:
        ch = pd.DataFrame([dict(channel=A.CHANNELS.get(k, k), tickets=v) for k, v in m["by_channel"].items()])
        fig = px.bar(ch.sort_values("tickets"), x="tickets", y="channel", orientation="h", title="Omnichannel volume",
                     color_discrete_sequence=[SERIES_1])
        fig.update_layout(margin=dict(t=50, l=10, r=10, b=10), xaxis_title=None, yaxis_title=None, bargap=0.3)
        st.plotly_chart(fig, width="stretch")
    with c5:
        st.markdown('<div class="sp-card-title">Feedback loop: learned escalation thresholds</div>', unsafe_allow_html=True)
        lt = A.learned_thresholds(store)
        st.dataframe(pd.DataFrame([dict(Issue=A.INTENTS[k]["label"], Threshold=f"{v['threshold']:.0%}", Why=v["reason"])
                                   for k, v in lt.items() if v["reason"] != "default"]), hide_index=True, width="stretch")
        st.caption("Low CSAT on AI-resolved tickets raises the bar for automation; high CSAT lowers it.")
        st.markdown('<div class="sp-card-title">Open tickets by team</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([dict(Team=k, Open=v) for k, v in sorted(m["by_team_open"].items(), key=lambda x: -x[1])]),
                     hide_index=True, width="stretch")

    with st.expander("Latest automatic customer notifications", icon=":material/notifications:"):
        for n in sorted(store.list("notifications"), key=lambda n: n["created_at"], reverse=True)[:20]:
            st.caption(f"{fmt_ts(n['created_at'])} · {n.get('ticket_id') or ''} · {A.CHANNELS.get(n['channel'], n['channel'])}: {n['message']}")

# ---- 5 knowledge base ------------------------------------------------------------------------------------
with tabs[5]:
    q1, q2 = st.columns([3, 1])
    query = q1.text_input("Search the knowledge base", "refund for a failed payment")
    intent_f = q2.selectbox("Intent boost", [None] + list(A.INTENTS), format_func=lambda i: "none" if i is None else i)
    for r in A.search_kb(query, intent_f, None, 5, store):
        with st.container(border=True):
            st.markdown(f"**{r['id']} · {r['title']}** · {r['type']} · reliability {r['reliability']:.0%}"
                        + (" · 🤖 auto-generated" if r["auto_generated"] else ""))
            st.progress(r["relevance"], text=f"relevance {r['relevance']:.0%}" + (f" · {', '.join(r['why'])}" if r["why"] else ""))
            st.caption(r["content"])
            if r["steps"]:
                st.caption("Workflow: " + " → ".join(r["steps"]))
    auto = store.list("kb_auto")
    st.markdown(f'<div class="sp-card-title">Auto Knowledge Base Expansion · {len(auto)} article(s) learned</div>', unsafe_allow_html=True)
    if auto:
        st.dataframe(pd.DataFrame([dict(ID=a["id"], Title=a["title"], Source=a["source_ticket"], Novelty=a["novelty"]) for a in auto]),
                     hide_index=True, width="stretch")
    else:
        st.caption("Resolve an escalated ticket with a resolution note in the Tickets tab — if it is new knowledge, a FAQ is written automatically.")
    with st.expander("All articles"):
        st.dataframe(pd.DataFrame([dict(ID=a["id"], Type=a["type"], Category=a["category"], Title=a["title"],
                                        Reliability=a["reliability"]) for a in A.load_kb(store)]), hide_index=True, width="stretch")

# ---- 6 omnichannel ---------------------------------------------------------------------------------------
with tabs[6]:
    st.caption("Every channel's native payload goes through the same adapter and the same agents — one brain, five channels.")
    oc = st.segmented_control("Channel", list(A.CHANNELS), format_func=lambda c: A.CHANNELS[c], default="whatsapp", key="oc_channel")
    oc = oc or "whatsapp"
    raw = st.text_area("Inbound payload (JSON)", json.dumps(A.SAMPLE_PAYLOADS[oc], indent=2, ensure_ascii=False), height=200, key=f"payload_{oc}")
    if st.button("Deliver to SupportPilot", type="primary", icon=":material/send:"):
        try:
            r = A.handle_message(store, channel=oc, payload=json.loads(raw), use_llm=use_llm)
            o1, o2 = st.columns(2, gap="large")
            with o1:
                st.markdown('<div class="sp-card-title">Normalized inbound</div>', unsafe_allow_html=True)
                st.json(r["inbound"])
                ctx = r.get("context")
                st.caption(f"Customer: {ctx['name']} — {ctx['headline']}" if ctx else "Customer not identified yet")
                if isinstance(r.get("decision"), dict):
                    st.caption(f"Decision: {r['decision']['decision']} · ticket {(r.get('ticket') or {}).get('id', '—')}")
            with o2:
                st.markdown(f'<div class="sp-card-title">Reply formatted for {A.CHANNELS[oc]}</div>', unsafe_allow_html=True)
                st.code(r["reply_channel"], language=None, wrap_lines=True)
        except json.JSONDecodeError as exc:
            st.error(f"Invalid JSON: {exc}")

# ---- 7 architecture ----------------------------------------------------------------------------------------
with tabs[7]:
    st.graphviz_chart(flow_dot([dict(agent=f"{k} x") for k, _ in FLOW] + [dict(agent="Reviewer")]), width="stretch")
    st.dataframe(pd.DataFrame(A.AGENTS, columns=["#", "Agent", "Module", "Job"]), hide_index=True, width="stretch")
    a1, a2 = st.columns(2, gap="large")
    with a1:
        st.markdown('<div class="sp-card-title">Actions the system can take</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(A.action_catalog())[["domain", "label", "action"]], hide_index=True, width="stretch")
    with a2:
        st.markdown('<div class="sp-card-title">MCP server</div>', unsafe_allow_html=True)
        st.markdown("`mcp_server.py` exposes 16 tools, 3 resources and a prompt over stdio or streamable HTTP, sharing this "
                    "app's database — tickets created from Claude appear here instantly.")
        st.code("claude mcp add supportpilot -- python mcp_server.py\n# or HTTP\npython mcp_server.py --http --port 8000", language="bash")
        st.code(json.dumps({"mcpServers": {"supportpilot": {"command": "python", "args": ["/path/to/SupportPilot/mcp_server.py"]}}}, indent=2),
                language="json")
        st.markdown('<div class="sp-card-title">Escalation policy</div>', unsafe_allow_html=True)
        st.markdown("- **Always escalate:** fraud, data breach, legal requests\n- **Auto-resolve:** password reset, tracking, "
                    "refund status, returns, plan changes…\n- **Escalate when:** confidence < threshold (70% base, learned per intent), "
                    "action failed/blocked, high-value claim, angry repeat contact, human requested")
    with st.expander("Intent taxonomy"):
        st.dataframe(pd.DataFrame([dict(Intent=k, Label=v["label"], Category=v["category"], Priority=v["priority"], Team=v["team"],
                                        Domain=v["domain"]) for k, v in A.INTENTS.items()]), hide_index=True, width="stretch")
