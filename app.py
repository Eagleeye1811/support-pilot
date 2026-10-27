"""SupportPilot dashboard — `streamlit run app.py`.

Pages: Support desk · Tickets & approvals · Live operations · Customer intelligence · Knowledge base · Data explorer ·
Settings. Every page labels where its data is stored (support database, company system, analytics warehouse, files).
"""
import hmac

import streamlit as st

import agents as A
from agents.shared_agent import get_secret
from telegram_bot import start_in_background as start_telegram
from ui import data_explorer, desk, intelligence, knowledge, live_ops, operations, settings
from ui.common import workspace

st.set_page_config(page_title="SupportPilot", page_icon=":material/support_agent:", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem;}
[data-testid="stMetric"] {background: var(--secondary-background-color); border: 1px solid rgba(128, 128, 128, .28); border-radius: 12px; padding: .75rem;}
[data-testid="stDataFrame"] {border: 1px solid rgba(128, 128, 128, .28); border-radius: 10px;}
</style>
""", unsafe_allow_html=True)

# Optional login: set SUPPORTPILOT_DASHBOARD_PASSWORD whenever anyone but you can reach the dashboard.
_pw = get_secret("SUPPORTPILOT_DASHBOARD_PASSWORD")
if _pw and not st.session_state.get("dash_ok"):
    st.title("SupportPilot")
    entered = st.text_input("Dashboard password", type="password")
    if entered and hmac.compare_digest(entered.encode(), _pw.encode()):
        st.session_state["dash_ok"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()

store = workspace()
# one long-polling Telegram bot per server process (de-duplicated); None until TELEGRAM_BOT_TOKEN is set
st.session_state["_tg_bot"] = start_telegram(store, use_llm=A.llm_enabled())

with st.sidebar:
    st.markdown("### SupportPilot")
    st.caption(store.tenant["name"])
    if store.adapter.simulated:
        st.caption(f"🏢 {store.adapter.name} · **simulated** company data")
    else:
        st.caption(f"🏢 {store.adapter.name} · live")
    lm = A.llm.status()
    st.caption(f"🧠 LLM `{lm['model']}` · {lm['mode']}" if lm["mode"] != "fallback" else f"⚙️ Rules only · {lm['reason']}")
    st.toggle("Use LLM", value=A.llm_enabled(), disabled=not A.llm_enabled(), key="use_llm",
              help="Off = deterministic rules only. Actions are always decided by rules, policy and people.")
    pending = len(A.approvals.pending(store))
    if pending:
        st.caption(f"⏳ {pending} action(s) waiting for approval")

nav = st.navigation({
    "Support": [st.Page(desk.page, title="Support desk", icon=":material/forum:", url_path="desk", default=True),
                st.Page(operations.page, title="Tickets & approvals", icon=":material/confirmation_number:", url_path="tickets")],
    "Insights": [st.Page(live_ops.page, title="Live operations", icon=":material/monitoring:", url_path="operations"),
                 st.Page(intelligence.page, title="Customer intelligence", icon=":material/insights:", url_path="intelligence")],
    "Data": [st.Page(knowledge.page, title="Knowledge base", icon=":material/menu_book:", url_path="knowledge"),
             st.Page(data_explorer.page, title="Data explorer", icon=":material/database:", url_path="data")],
    "Admin": [st.Page(settings.page, title="Settings", icon=":material/settings:", url_path="settings")],
})
nav.run()
