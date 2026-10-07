"""SupportPilot — website.

One page: live status, the live agent flow (every agent is an MCP tool), a single
"try it" box for visitors without Telegram, and a calm view of the data store.
Customers normally talk to the desk from Telegram on their phone.
"""
import pandas as pd
import plotly.express as px
import streamlit as st

import agents as A
from agents.email_agent import email_configured
from agents.mcp_bus import get_bus
from agents.media import attachment_data_uri, product_data_uri, product_image_path, save_attachment
from agents.shared_agent import fmt_ts, humanize_minutes
from data.generate_data import CATALOG
from telegram_bot import start_in_background as start_telegram
from ui.flow_component import build_view, render

st.set_page_config(page_title="SupportPilot · MCP agent desk", page_icon=":material/support_agent:", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""
<style>
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] {display: none;}
.block-container {padding-top: 1.4rem; padding-bottom: 3rem; max-width: 1500px;}
.sp-hero h1 {font-size: 2.1rem; margin: 0; letter-spacing: -.02em;}
.sp-hero p {margin: .2rem 0 0; opacity: .75; font-size: 1.02rem;}
.sp-chips {display: flex; flex-wrap: wrap; gap: .45rem; margin: .8rem 0 .4rem;}
.sp-chip {display: inline-flex; align-items: center; gap: .35rem; font-size: .84rem; padding: .28rem .7rem; border-radius: 999px;
          border: 1px solid rgba(128,128,128,.3); background: var(--secondary-background-color);}
.sp-chip i {width: .5rem; height: .5rem; border-radius: 50%; display: inline-block;}
.sp-chip a {color: inherit;}
.sp-section {margin: 1.6rem 0 .2rem; font-size: 1.3rem; font-weight: 700;}
.sp-muted {opacity: .7; font-size: .9rem;}
[data-testid="stMetric"] {background: var(--secondary-background-color); border: 1px solid rgba(128,128,128,.25); border-radius: 14px; padding: .7rem .9rem;}
.sp-tag {font-size: .72rem; padding: .1rem .45rem; border-radius: 6px; background: rgba(37,99,235,.12); color: #2563eb; margin-left: .3rem;}
</style>
""", unsafe_allow_html=True)

CATEGORY_ORDER = ["Billing", "Orders", "Account", "Subscription", "Connectivity", "Security", "Legal", "General"]
CATEGORY_COLORS = dict(zip(CATEGORY_ORDER, ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]))
SLA_ICON = {"ok": "🟢 on track", "at_risk": "🟠 at risk", "breached": "🔴 breached", "met": "✅ met", "missed": "❌ missed", "n/a": "—"}
EMAIL_ICON = {"sent": "✅ sent", "not_configured": "⚙️ not configured", "rate_limited": "⏳ rate limited", "blocked": "⛔ blocked",
              "failed": "❌ failed"}
SAMPLE = ("CUST1001", "My order was delivered but I received the wrong item")


@st.cache_resource
def get_store(code_version):
    return A.Store()


# Keyed on the Store class: when a deploy hot-reloads agents/store.py, the class object changes and a fresh
# Store (same database file) is created, instead of reusing an instance built from the old code.
store = get_store(id(A.Store))
bus = get_bus(store)
tg_bot = start_telegram(store, use_llm=A.llm_enabled())  # one long-polling bot per server process; None without a token
THEME = getattr(st.context.theme, "type", None) or "light"
ss = st.session_state

if not A.latest_seq(store):  # first visit after a (re)start: show a real sample turn instead of an empty stage
    A.record_turn(store, "sample", SAMPLE[1], A.handle_message(store, SAMPLE[1], customer_id=SAMPLE[0], use_llm=False))


# ---- header -----------------------------------------------------------------------------------
def chip(color, html_text):
    return f'<span class="sp-chip"><i style="background:{color}"></i>{html_text}</span>'


status = store.get("meta", "telegram") or {}
tools = bus.list_tools()
chips = []
if tg_bot and status.get("running") and status.get("username"):
    chips.append(chip("#12a150", f'Telegram <a href="https://t.me/{status["username"]}" target="_blank">@{status["username"]}</a>'))
elif tg_bot:
    chips.append(chip("#d97706", "Telegram connecting…" if not status.get("error") else "Telegram error"))
else:
    chips.append(chip("#98a2b3", "Telegram not connected"))
chips.append(chip("#12a150" if email_configured() else "#98a2b3", "Email agent ready" if email_configured() else "Email not configured"))
chips.append(chip("#2563eb" if bus.transport == "mcp" else "#d97706", f"MCP · {len(tools)} agent tools · {bus.transport}"))
chips.append(chip("#7c3aed" if A.llm_enabled() else "#98a2b3", "LLM polish on" if A.llm_enabled() else "Deterministic (no LLM)"))

head, cta = st.columns([4, 1], vertical_alignment="center")
with head:
    st.markdown('<div class="sp-hero"><h1>SupportPilot</h1><p>An autonomous customer-support desk. Every agent is a real '
                '<b>MCP tool</b>; message the bot on Telegram and watch them work here, live.</p></div>'
                f'<div class="sp-chips">{"".join(chips)}</div>', unsafe_allow_html=True)
with cta:
    if status.get("username"):
        st.link_button("Message the bot", f"https://t.me/{status['username']}", icon=":material/send:", type="primary", width="stretch")
if status.get("error") and tg_bot:
    st.caption(f"⚠️ Telegram: {status['error']}")


# ---- live agent flow --------------------------------------------------------------------------------
@st.fragment(run_every="2s")
def watch_for_new_turns():
    """Cheap poll: rerun the page only when a new turn arrives (from Telegram or the website)."""
    seq = A.latest_seq(store)  # one counter read — no turn documents are loaded
    if ss.get("latest_seq") is None:
        ss.latest_seq = seq
    elif seq != ss.latest_seq:
        ss.latest_seq = seq
        st.rerun()


watch_for_new_turns()
turns = A.recent_turns(store, 40)[::-1]


def turn_label(t):
    r = t["result"]
    chat = store.get("telegram_chats", t["chat_id"]) if t.get("chat_id") else None
    who = (r.get("context") or {}).get("first_name") or (chat or {}).get("first_name") or "Guest"
    src = {"telegram": "Telegram", "website": "Website", "sample": "Sample"}.get(t["source"], t["source"])
    return f"{fmt_ts(t['ts'])} · {src} · {who}: {t['text'][:70]}"


# no widget key: when a new turn arrives the options change and the newest turn is selected again
turn = st.selectbox("Conversation turn", turns, format_func=turn_label, label_visibility="collapsed")
render(build_view(store, turn), theme=THEME, height=800)
if turn["source"] == "sample":
    st.caption("This is a sample replay. Message the bot on Telegram, or use *Try it here* below, and your own turn appears "
               "in this view within a couple of seconds.")


# ---- try it here (the only input on the site) --------------------------------------------------------
with st.expander("No Telegram? Try it here", icon=":material/keyboard:"):
    customers = sorted(store.list("customers"), key=lambda c: c["id"])
    names = {None: "Guest (not logged in)"} | {c["id"]: f"{c['name']} · {c['tier']}" for c in customers[:12]}
    with st.form("try", clear_on_submit=True, border=False):
        c1, c2 = st.columns([1, 2])
        who = c1.selectbox("Customer", list(names), format_func=names.get)
        text = c2.text_input("Message", placeholder="e.g. My laptop arrived damaged, the screen is cracked")
        c3, c4 = st.columns([1, 2])
        email = c3.text_input("Email for updates (optional)", placeholder="you@gmail.com")
        photo = c4.file_uploader("Photo of the item (optional)", type=["jpg", "jpeg", "png"])
        sent = st.form_submit_button("Send to the agents", type="primary", icon=":material/send:")
    if sent and text.strip():
        if ss.get("web_customer", "unset") != who:  # switching customer starts a new conversation
            ss.web_customer, ss.web_conv = who, None
        atts = []
        if photo is not None:
            try:
                atts.append(save_attachment(store, photo.getvalue(), photo.type or "image/jpeg", "website", text)["id"])
            except ValueError as exc:
                st.error(str(exc))
        try:
            r = A.handle_message(store, text.strip(), ss.get("web_conv"), who, "web", use_llm=A.llm_enabled(),
                                 attachments=atts, contact_email=email.strip().lower() or None)
            ss.web_conv = r["conversation_id"]
            A.record_turn(store, "website", text.strip(), r)
            st.rerun()
        except Exception as exc:  # keep the page alive and show the problem
            st.error(f"{type(exc).__name__}: {exc}")
    st.caption("Messages here run through exactly the same MCP agents as Telegram. Everything on this site is public.")


# ---- data store --------------------------------------------------------------------------------------
st.markdown('<div class="sp-section">Inside the data store</div>', unsafe_allow_html=True)
st.markdown('<div class="sp-muted">What the agents read and write: one SQLite document store shared by this website, the '
            'Telegram bot and the MCP server.</div>', unsafe_allow_html=True)
tab_over, tab_tickets, tab_customers, tab_products, tab_emails, tab_mcp = st.tabs(
    [":material/monitoring: Overview", ":material/confirmation_number: Tickets", ":material/group: Customers & orders",
     ":material/inventory_2: Products", ":material/mail: Emails", ":material/hub: MCP tools"])

with tab_over:
    m, cs = A.supervisor_metrics(store), A.csat_summary(store)
    with st.container(horizontal=True):
        st.metric("Open tickets", m["open"])
        st.metric("Resolved by AI", f"{m['automation_rate']:.0%}", help="Share of all tickets resolved without a human")
        st.metric("Avg resolution", humanize_minutes(m["avg_resolution_min"] or 0))
        st.metric("CSAT", f"{cs['avg']:.2f}/5" if cs["avg"] else "—")
        st.metric("SLA at risk / breached", f"{m['sla_at_risk']} / {m['sla_breached']}")
        st.metric("Emails sent", sum(e["status"] == "sent" for e in store.list("emails")))
    for inc in A.detect_incidents(store)[:2]:
        sev = "🔴" if inc["severity"] == "critical" else "🟠"
        st.warning(f"{sev} **Root cause detected: {inc['hypothesis']}** — {inc['recommended_action']}", icon=":material/troubleshoot:")
    vol = pd.DataFrame(A.daily_volume(store))
    if not vol.empty:
        fig = px.bar(vol, x="date", y="tickets", color="category", category_orders={"category": CATEGORY_ORDER},
                     color_discrete_map=CATEGORY_COLORS, title="Tickets per day by category (21 days)")
        fig.update_layout(bargap=0.25, legend_title_text="", margin=dict(t=50, l=10, r=10, b=10), xaxis_title=None,
                          yaxis_title=None, hovermode="x unified", height=320)
        st.plotly_chart(fig, width="stretch")

with tab_tickets:
    tickets = sorted(store.list("tickets"), key=lambda t: t["created_at"], reverse=True)
    live = st.segmented_control("Show", ["Live (Telegram & website)", "All, incl. 21-day history"],
                                default="Live (Telegram & website)", label_visibility="collapsed")
    rows = [t for t in tickets if live != "Live (Telegram & website)" or not t.get("historical")]
    if not rows:
        st.info("No live tickets yet — message the bot and one will appear here.", icon=":material/inbox:")
    else:
        df = pd.DataFrame([dict(Ticket=t["id"], Title=t["title"], Customer=t["customer"]["name"], Priority=t["priority"],
                                Status=t["status"], Team=t.get("team"), Channel=A.CHANNELS.get(t.get("channel"), t.get("channel")),
                                SLA=SLA_ICON[A.sla_status(t).get("state", "n/a")], Created=fmt_ts(t["created_at"]))
                           for t in rows[:300]])
        ev = st.dataframe(df, hide_index=True, width="stretch", height=280, on_select="rerun", selection_mode="single-row",
                          key="tkt_table")
        sel = ev.selection.rows if ev and ev.selection else []
        if sel:
            t = store.get("tickets", df.iloc[sel[0]]["Ticket"])
            a, b = st.columns([1.7, 1], gap="large")
            with a:
                photos = [attachment_data_uri(store, x) for x in t.get("attachments") or []]
                order = store.get("orders", (t.get("slots") or {}).get("order_id", "")) if t.get("slots") else None
                pics = [(f"Customer photo {i + 1}", p) for i, p in enumerate(photos) if p]
                if order:
                    pics += [(f"Ordered: {i['name']}", product_data_uri(i["sku"])) for i in order["items"][:2]
                             if product_image_path(i["sku"])]
                if pics:
                    cols = st.columns(min(4, len(pics)))
                    for col, (cap, src) in zip(cols, pics):
                        col.image(src, caption=cap, width="stretch")
                st.markdown(A.render_ticket(t))
            with b:
                s = A.sla_status(t)
                st.metric("SLA", SLA_ICON[s.get("state", "n/a")],
                          f"{humanize_minutes(s['minutes_left'])} {'left' if s['minutes_left'] >= 0 else 'overdue'}"
                          if "minutes_left" in s else None, delta_color="off")
                for e in t.get("events", []):
                    st.caption(f"{fmt_ts(e['ts'])} · **{e['status']}** · {e['actor']}" + (f" — {e['note']}" if e.get("note") else ""))
                allowed = sorted(A.TRANSITIONS.get(t["status"], []))
                if allowed:
                    with st.form(f"upd_{t['id']}"):
                        st.markdown("**Update as a human agent** — the customer is notified on Telegram (and by email).")
                        target = st.selectbox("Move to", ["Reopened" if x == "Opened" and t["status"] == "Resolved" else x
                                                          for x in allowed])
                        note = st.text_input("Note to the customer")
                        if st.form_submit_button("Apply", type="primary"):
                            try:
                                A.transition(store, t["id"], target, actor="Human Agent", note=note)
                                st.toast("Updated — the bot delivers the update within ~10 seconds.")
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))

with tab_customers:
    customers = sorted(store.list("customers"), key=lambda c: c["id"])[:12]
    orders = store.list("orders")
    cols = st.columns(3, gap="medium")
    for i, c in enumerate(customers):
        mine = sorted([o for o in orders if o["customer_id"] == c["id"]], key=lambda o: o["placed_at"], reverse=True)
        with cols[i % 3].container(border=True):
            st.markdown(f"**{c['name']}** <span class='sp-tag'>{c['tier']}</span> <span class='sp-muted'>· {c.get('city', '')} · "
                        f"{c['id']}</span>", unsafe_allow_html=True)
            if mine:
                thumbs = st.columns(4)
                for col, o in zip(thumbs, mine[:4]):
                    sku = o["items"][0]["sku"]
                    if product_image_path(sku):
                        col.image(product_image_path(sku), caption=f"{o['id']} · {o['status']}", width="stretch")
            else:
                st.caption("No orders — subscription / connectivity customer")

with tab_products:
    inv = {p["sku"]: p for p in store.list("inventory")}
    cols = st.columns(7, gap="small")
    for i, (sku, name, category, price) in enumerate(CATALOG):
        with cols[i % 7]:
            st.image(product_image_path(sku), width="stretch")
            stock = (inv.get(sku) or {}).get("stock", 0)
            st.caption(f"**{name}**  \n{A.inr(price)} · {category} · " + ("out of stock" if not stock else f"{stock} in stock"))

with tab_emails:
    emails = sorted(store.list("emails"), key=lambda e: e["created_at"], reverse=True)
    if not email_configured():
        st.info("Email sending is not configured: add `SMTP_USER` (your Gmail) and `SMTP_PASSWORD` (a Google App Password) "
                "to the app's secrets. Until then the Email agent still renders and logs every email.", icon=":material/mail_lock:")
    if not emails:
        st.caption("No emails yet. After a ticket is created, the Telegram bot asks for an email address.")
    else:
        e = st.selectbox("Email", emails, format_func=lambda e: f"{fmt_ts(e['created_at'])} · {EMAIL_ICON.get(e['status'], e['status'])}"
                                                              f" · {e['to_masked']} · {e['subject']}")
        if e.get("error"):
            st.caption(f"Reason: {e['error']}")
        body = e.get("html", "")
        for sku in e.get("media", []):  # inline images travel as cid: attachments; show them here as data URIs
            body = body.replace(f"cid:{sku}", product_data_uri(sku) or "")
        st.iframe(body, height=720)

with tab_mcp:
    st.markdown(f"The orchestrator is an **MCP client**: every agent below is a tool on an MCP server, called over JSON-RPC "
                f"(`{bus.transport}` transport, in-process). The same tools are published to Claude Desktop by `mcp_server.py`.")
    st.dataframe(pd.DataFrame([dict(Tool=t["name"], Agent=t["description"].split("]")[0].strip("["),
                                    Description=t["description"].split("] ", 1)[-1]) for t in tools]),
                 hide_index=True, width="stretch")
    st.code('{\n  "mcpServers": {\n    "supportpilot": {\n      "command": "python",\n'
            '      "args": ["/path/to/SupportPilot/mcp_server.py"]\n    }\n  }\n}', language="json")
    st.caption("Add this to Claude Desktop's `claude_desktop_config.json` to let Claude call the same agents.")

st.caption("SupportPilot · synthetic demo data (resets when the server restarts) · built on the Model Context Protocol")
