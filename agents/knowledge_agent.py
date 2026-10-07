"""Agent 3: Knowledge Base Retrieval — FAQs, product docs, internal policies and troubleshooting guides.

Lightweight RAG without external services: BM25 ranking, boosted by intent and
category match and weighted by each article's reliability, so the *most
reliable* workflow wins. Also implements Auto Knowledge Base Expansion: when a
human resolves a novel issue, a new FAQ article is written automatically.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter

from .shared_agent import INTENTS, ROOT, iso, tokenize

KB_PATH = os.path.join(ROOT, "docs", "support_kb.json")
_BASE_KB = None


def load_base_kb(path=None):
    global _BASE_KB
    if _BASE_KB is None or path:
        with open(path or KB_PATH, "r", encoding="utf-8") as f:
            _BASE_KB = json.load(f)
    return _BASE_KB


def load_kb(store=None):
    kb = list(load_base_kb())
    if store is not None:
        kb += [a for a in store.list("kb_auto") if a.get("status") == "published"]
    return kb


def _doc_text(a):
    return f"{a['title']} {a['title']} {a.get('content', '')} {' '.join(a.get('steps', []))} {' '.join(a.get('intents', []))}"


def search(query, intent=None, category=None, top_k=4, store=None, kb=None):
    """Rank articles for a query. Returns dicts with score, relevance (0-1) and why it was chosen."""
    kb = kb or load_kb(store)
    docs = [tokenize(_doc_text(a)) for a in kb]
    q = tokenize(query) + (tokenize(intent.replace("_", " ")) if intent else [])
    if not q:
        q = tokenize(INTENTS.get(intent, {}).get("label", "")) if intent else []
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
        if intent and intent in a.get("intents", []):
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
                                steps=a.get("steps", []), auto_generated=a.get("auto_generated", False), why=why))
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
                 for a in load_base_kb() if a["type"] == "troubleshooting" and intent in a.get("intents", [])]
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
    if intent in INTENTS and top.get("why") and "intent match" in top["why"]:
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
    article = dict(id=f"KB-AUTO-{seq:03d}", type="faq", category=ticket.get("category", "General"),
                   title=f"{question} — how it was resolved", intents=[ticket.get("intent")], reliability=0.75,
                   updated=iso()[:10], auto_generated=True, status="published", source_ticket=ticket["id"],
                   content=f"Q: {question}. Investigation: {'; '.join(ticket.get('investigation', [])[:4])}. "
                           f"A: {resolution_note.strip()}",
                   novelty=novelty, created_at=iso())
    store.put("kb_auto", article["id"], article)
    return dict(created=True, article=article, novelty=novelty)
