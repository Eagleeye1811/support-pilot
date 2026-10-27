"""Agent 3: Knowledge Base Retrieval — the company help centre, policies and internal agent playbooks.

Where the knowledge lives:
  * source of truth for the shipped content: Markdown articles in ``docs/help-center/<section>/<slug>.md``
    (front matter: id, title, type, category, audience, intents, owner, reliability, last_reviewed, steps);
  * at runtime: the tenant's SupportPilot database, collection ``kb_articles`` (imported from the files on first use,
    editable in the dashboard, every edit versioned in ``kb_history``), plus ``kb_auto`` for articles learned from
    resolved tickets.
Retrieval is BM25 boosted by intent/category and weighted by reliability. Articles with ``audience: internal``
(agent playbooks) guide troubleshooting but are never shown to customers.
"""
from __future__ import annotations

import glob
import math
import os
import re
from collections import Counter

from .shared_agent import INTENTS, ROOT, iso, tokenize

HELP_CENTER = os.path.join(ROOT, "docs", "help-center")
_FILES = None


def parse_article(path):
    """One Markdown help-centre article → dict."""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    m = re.match(r"---\n(.*?)\n---\n(.*)", raw, re.S)
    if not m:
        raise ValueError(f"{path}: missing front matter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = re.sub(r"^# .*\n+", "", m.group(2).strip()).strip()
    first = next((p for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith(("|", "#"))), body)
    return dict(
        id=meta["id"], title=meta["title"], type=meta.get("type", "faq"), category=meta.get("category", "General"),
        audience=meta.get("audience", "customer"), intents=[i.strip() for i in meta.get("intents", "").split(",") if i.strip()],
        owner=meta.get("owner"), reliability=float(meta.get("reliability", 0.8)), last_reviewed=meta.get("last_reviewed"),
        steps=[x.strip() for x in meta.get("steps", "").split("|") if x.strip()], content=body,
        summary=re.sub(r"\*\*|\*", "", " ".join(first.split())), section=os.path.basename(os.path.dirname(path)),
        source_file=os.path.relpath(path, ROOT))


def load_help_center(path=None):
    """All articles from the Markdown files (cached per process)."""
    global _FILES
    if _FILES is None or path:
        files = sorted(glob.glob(os.path.join(path or HELP_CENTER, "*", "*.md")))
        _FILES = [parse_article(f) for f in files]
    return _FILES


load_base_kb = load_help_center  # back-compat name


def ensure_kb(store):
    """Import the help centre into the store the first time it is used (or after a reset)."""
    if store.get("meta", "kb_import"):
        return False
    for a in load_help_center():
        if store.get("kb_articles", a["id"]) is None:
            store.put("kb_articles", a["id"], dict(a, version=1, status="published", imported_at=iso(), edited_by=None))
    store.put("meta", "kb_import", dict(id="kb_import", at=iso(), articles=len(load_help_center()), source="docs/help-center"))
    return True


def load_kb(store=None):
    if store is None:
        return list(load_help_center())
    ensure_kb(store)
    kb = [a for a in store.list("kb_articles") if a.get("status") == "published"]
    kb += [a for a in store.list("kb_auto") if a.get("status") == "published"]
    return sorted(kb, key=lambda a: a["id"])


def save_article(store, article_id, editor, title=None, content=None, reliability=None, status=None):
    """Edit an article in the database; the previous version goes to ``kb_history``."""
    from . import audit
    if not editor or not str(editor).strip():
        raise ValueError("An editor name is required")
    ensure_kb(store)
    a = store.get("kb_articles", article_id)
    if a is None:
        raise KeyError(article_id)
    store.put("kb_history", f"{article_id}-v{a.get('version', 1)}", dict(a, archived_at=iso()))
    if title is not None:
        a["title"] = title.strip()
    if content is not None:
        a["content"] = content.strip()
        first = next((p for p in re.split(r"\n\s*\n", a["content"]) if p.strip() and not p.lstrip().startswith(("|", "#"))), a["content"])
        a["summary"] = re.sub(r"\*\*|\*", "", " ".join(first.split()))
    if reliability is not None:
        a["reliability"] = float(reliability)
    if status is not None:
        a["status"] = status
    a.update(version=a.get("version", 1) + 1, edited_by=editor, edited_at=iso())
    store.put("kb_articles", article_id, a)
    audit.log(store, "kb.article_edited", editor, article_id=article_id, version=a["version"])
    return a


def history(store, article_id):
    return sorted(store.list("kb_history", id=article_id), key=lambda h: h.get("version", 0), reverse=True)


def reimport(store, actor):
    """Reload every article from docs/help-center (database edits are archived in kb_history first)."""
    from . import audit
    for a in store.list("kb_articles"):
        store.put("kb_history", f"{a['id']}-v{a.get('version', 1)}", dict(a, archived_at=iso()))
        store.delete("kb_articles", a["id"])
    store.delete("meta", "kb_import")
    global _FILES
    _FILES = None
    ensure_kb(store)
    audit.log(store, "kb.reimported", actor, articles=len(load_help_center()))


def customer_facing(articles):
    return [a for a in articles if a.get("audience", "customer") != "internal"]


def _stem(word):
    """Light suffix stripping so 'exchanging' / 'exchanges' / 'exchange' meet in the index."""
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            word = word[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


def _terms(text):
    return [_stem(t) for t in tokenize(text)]


def _doc_text(a):
    return f"{a['title']} {a['title']} {a.get('content', '')} {' '.join(a.get('steps', []))} {' '.join(a.get('intents', []))}"


def search(query, intent=None, category=None, top_k=4, store=None, kb=None):
    """Rank articles for a query. Returns dicts with score, relevance (0-1) and why it was chosen."""
    kb = kb or load_kb(store)
    docs = [_terms(_doc_text(a)) for a in kb]
    q = _terms(query) + (_terms(intent.replace("_", " ")) if intent else [])
    if not q:
        q = _terms(INTENTS.get(intent, {}).get("label", "")) if intent else []
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / max(n, 1)
    df = Counter(t for d in docs for t in set(d))
    k1, b = 1.4, 0.75
    results = []
    for a, d in zip(kb, docs):
        tf = Counter(d)
        bm25 = 0.0
        for t in set(q):
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            bm25 += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
        why = []
        boost = 1.0
        if intent and intent != "general_inquiry" and intent in a.get("intents", []):  # the catch-all intent boosts nothing
            boost *= 1.8
            why.append("intent match")
        if category and a.get("category") == category:
            boost *= 1.15
            why.append("category match")
        reliability = float(a.get("reliability", 0.8))
        score = (bm25 + (1.5 if "intent match" in why else 0)) * boost * (0.5 + 0.5 * reliability)
        if score > 0:
            results.append(dict(id=a["id"], title=a["title"], type=a["type"], category=a.get("category"),
                                reliability=reliability, score=round(score, 3), content=a.get("content", ""),
                                summary=a.get("summary") or a.get("content", "")[:300], audience=a.get("audience", "customer"),
                                intents=a.get("intents", []),
                                steps=a.get("steps", []), auto_generated=a.get("auto_generated", False), why=why,
                                source=a.get("source_file") or ("learned from " + a["source_ticket"] if a.get("source_ticket") else "database"),
                                version=a.get("version", 1)))
    results.sort(key=lambda r: -r["score"])
    top = results[0]["score"] if results else 1
    for r in results:
        r["relevance"] = round(r["score"] / top, 2) if top else 0
    return results[:top_k]


def select_workflow(intent, articles):
    """Pick the most reliable troubleshooting workflow among retrieved articles for this intent."""
    flows = [a for a in articles if a["type"] == "troubleshooting"]
    if not flows:
        flows = [dict(id=a["id"], title=a["title"], reliability=a["reliability"], relevance=0.5, steps=a.get("steps", []))
                 for a in load_help_center() if a["type"] == "troubleshooting" and intent in a.get("intents", [])]
    if not flows:
        return None
    best = max(flows, key=lambda a: a["reliability"] * (0.6 + 0.4 * a.get("relevance", 0.5)))
    return dict(id=best["id"], title=best["title"], reliability=best["reliability"], steps=best.get("steps", []))


def kb_confidence(articles, intent):
    """How well the KB alone answers the question (used for general inquiries)."""
    if not articles:
        return 0.3
    top = articles[0]
    raw = 1 - math.exp(-top["score"] / 6)
    if intent in INTENTS and intent in top.get("intents", []):  # the best article is written for this kind of question
        raw += 0.1
    return round(min(0.95, raw * (0.6 + 0.4 * top["reliability"])), 2)


def _jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def expand_kb(store, ticket, resolution_note, novelty_threshold=0.55):
    """Auto Knowledge Base Expansion: turn a human resolution into a reusable FAQ when it is new knowledge."""
    note_tokens = tokenize(resolution_note)
    if len(note_tokens) < 5:
        return dict(created=False, reason="Resolution note too short to become an article")
    kb = load_kb(store)
    best = max(kb, key=lambda a: _jaccard(note_tokens, tokenize(a.get("content", ""))))
    similarity = _jaccard(note_tokens, tokenize(best.get("content", "")))
    novelty = round(1 - similarity, 2)
    if novelty < novelty_threshold:
        return dict(created=False, reason=f"Already covered by {best['id']} ({best['title']})", novelty=novelty)
    seq = store.next_seq("kb_auto", 1)
    question = ticket.get("title") or INTENTS.get(ticket.get("intent"), {}).get("label", "Customer issue")
    article = dict(id=f"KB-AUTO-{seq:03d}", type="faq", category=ticket.get("category", "General"), audience="customer",
                   title=f"{question} — how it was resolved", intents=[ticket.get("intent")], reliability=0.75,
                   updated=iso()[:10], auto_generated=True, status="published", source_ticket=ticket["id"],
                   content=f"Q: {question}. Investigation: {'; '.join(ticket.get('investigation', [])[:4])}. "
                           f"A: {resolution_note.strip()}",
                   novelty=novelty, created_at=iso())
    store.put("kb_auto", article["id"], article)
    return dict(created=True, article=article, novelty=novelty)
