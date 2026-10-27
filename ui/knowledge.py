"""Knowledge base: the company help centre the assistant answers from — browse, search, edit with history."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import agents as A
from agents import knowledge_agent as K
from agents.shared_agent import fmt_ts

from .common import where, workspace

SECTION_NAMES = {"orders-and-delivery": "Orders & delivery", "returns-and-refunds": "Returns & refunds", "payments": "Payments",
                 "account-and-security": "Account & security", "nova-membership": "Nova Plus & Premium",
                 "novafiber-broadband": "NovaFiber broadband", "help-and-support": "Help & support",
                 "agent-playbooks": "Agent playbooks (internal)"}


def page():
    store = workspace()
    st.title("Knowledge base")
    kb = A.load_kb(store)
    imp = store.get("meta", "kb_import") or {}
    st.caption(f"{len(kb)} articles. The assistant answers only from these, and cites the ones it used on the Support desk.")
    where("files", "docs/help-center/<section>/<article>.md — the shipped source", store)
    where("support", f"kb_articles (imported {fmt_ts(imp.get('at'))}; edits are versioned in kb_history) · kb_auto "
                     "(learned from resolved tickets)", store)

    left, right = st.columns([1, 2.2], gap="large")
    with left:
        q = st.text_input("Search", placeholder="e.g. refund to my card", key="kb_q")
        sections = ["All"] + [s for s in SECTION_NAMES if any(a.get("section") == s for a in kb)] + (
            ["Learned from tickets"] if any(a.get("auto_generated") for a in kb) else [])
        sec = st.radio("Section", sections, format_func=lambda s: SECTION_NAMES.get(s, s), key="kb_sec")
        if q.strip():
            ids = [r["id"] for r in A.search_kb(q, None, None, 8, store)]
            items = [a for i in ids for a in kb if a["id"] == i]
        else:
            items = [a for a in kb if sec == "All" or a.get("section") == sec or (sec == "Learned from tickets" and a.get("auto_generated"))]
        if not items:
            st.caption("No matching articles.")
            return
        choice = st.radio("Article", [a["id"] for a in items], key="kb_pick", label_visibility="collapsed",
                          format_func=lambda i: next(f"{a['title']}" for a in items if a["id"] == i))
    a = next(x for x in kb if x["id"] == choice)
    with right:
        with st.container(horizontal=True):
            st.badge(a["id"], color="gray")
            st.badge(a["type"], color="violet")
            st.badge("internal — never shown to customers" if a.get("audience") == "internal" else "customer-facing",
                     color="orange" if a.get("audience") == "internal" else "green")
            st.badge(f"owner: {a.get('owner') or '—'}", color="blue")
            st.badge(f"v{a.get('version', 1)}", color="gray")
        st.markdown(f"## {a['title']}")
        st.markdown(a["content"])
        if a.get("steps"):
            st.markdown("**Agent steps:** " + " → ".join(a["steps"]))
        if a.get("auto_generated"):
            st.caption(f"Learned from ticket {a.get('source_ticket')} · stored in `kb_auto`")
        else:
            st.caption(f"Source file `{a.get('source_file')}` · last reviewed {a.get('last_reviewed')} · database copy "
                       f"`kb_articles/{a['id']}` v{a.get('version', 1)}"
                       + (f", edited by {a['edited_by']} {fmt_ts(a.get('edited_at'))}" if a.get("edited_by") else ""))
            with st.expander("Edit this article", icon=":material/edit:"):
                with st.form(f"edit_{a['id']}"):
                    editor = st.text_input("Your name")
                    title = st.text_input("Title", a["title"])
                    content = st.text_area("Content (Markdown)", a["content"], height=320)
                    if st.form_submit_button("Save new version", type="primary"):
                        try:
                            K.save_article(store, a["id"], editor, title=title, content=content)
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
                st.caption("Saving changes the database copy (used immediately). The file in docs/help-center is not changed.")
            hist = K.history(store, a["id"])
            if hist:
                with st.expander(f"Version history ({len(hist)})", icon=":material/history:"):
                    st.dataframe(pd.DataFrame([dict(Version=h.get("version"), Title=h["title"], EditedBy=h.get("edited_by") or "import",
                                                    Archived=fmt_ts(h.get("archived_at"))) for h in hist]), hide_index=True, width="stretch")
