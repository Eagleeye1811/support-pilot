"""Tiny SQLite document store shared by the Streamlit UI, the MCP server, tests and evaluation.

Collections hold JSON documents keyed by id (customers, orders, payments,
refunds, accounts, subscriptions, service_lines, outages, tickets,
conversations, actions, notifications, csat, kb_auto, ...). Using one file
means a ticket created from Claude via MCP shows up instantly on the
Supervisor Dashboard, and vice versa.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading

from .shared_agent import ROOT, iso


def default_db_path():
    return os.getenv("SUPPORTPILOT_DB") or os.path.join(ROOT, "data", "supportpilot.db")


class Store:
    def __init__(self, path=None, seed=True):
        self.path = path or default_db_path()
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock, self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS docs (collection TEXT, id TEXT, data TEXT, updated TEXT,"
                              " PRIMARY KEY (collection, id))")
            self.conn.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER)")
        if seed and not self.get("meta", "seed"):
            self.reset()

    # ---- CRUD -------------------------------------------------------------
    def get(self, collection, doc_id):
        with self._lock:
            row = self.conn.execute("SELECT data FROM docs WHERE collection=? AND id=?", (collection, str(doc_id))).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, collection, doc_id, data):
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO docs (collection, id, data, updated) VALUES (?,?,?,?)",
                              (collection, str(doc_id), json.dumps(data, default=str, ensure_ascii=False), iso()))
        return data

    def put_many(self, collection, docs, key="id"):
        with self._lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO docs (collection, id, data, updated) VALUES (?,?,?,?)",
                                  [(collection, str(d[key]), json.dumps(d, default=str, ensure_ascii=False), iso()) for d in docs])

    def delete(self, collection, doc_id):
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM docs WHERE collection=? AND id=?", (collection, str(doc_id)))

    def list(self, collection, where=None, **equals):
        with self._lock:
            rows = self.conn.execute("SELECT data FROM docs WHERE collection=?", (collection,)).fetchall()
        docs = [json.loads(r[0]) for r in rows]
        if equals:
            docs = [d for d in docs if all(d.get(k) == v for k, v in equals.items())]
        if where:
            docs = [d for d in docs if where(d)]
        return docs

    def update(self, collection, doc_id, **fields):
        doc = self.get(collection, doc_id)
        if doc is None:
            raise KeyError(f"{collection}/{doc_id} not found")
        doc.update(fields)
        return self.put(collection, doc_id, doc)

    def claim(self, collection, doc_id, data=None):
        """Atomically create a document only if it does not exist yet; True for the single caller that wins."""
        with self._lock, self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO docs (collection, id, data, updated) VALUES (?,?,?,?)",
                                    (collection, str(doc_id), json.dumps(data or {}, default=str), iso()))
        return cur.rowcount == 1

    def peek_seq(self, name):
        """Current value of a counter without incrementing it (0 if unused)."""
        with self._lock:
            row = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        return row[0] if row else 0

    def next_seq(self, name, start=1):
        with self._lock, self.conn:
            row = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            value = (row[0] + 1) if row else start
            self.conn.execute("INSERT OR REPLACE INTO counters (name, value) VALUES (?,?)", (name, value))
        return value

    # ---- seeding ------------------------------------------------------------
    def reset(self, world=None):
        """Wipe the store and load a fresh synthetic support world anchored to *now*."""
        if world is None:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from data.generate_data import make
            world = make()
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM docs")
            self.conn.execute("DELETE FROM counters")
        for collection, docs in world.get("collections", {}).items():
            self.put_many(collection, docs)
        for name, value in world.get("counters", {}).items():
            with self._lock, self.conn:
                self.conn.execute("INSERT OR REPLACE INTO counters (name, value) VALUES (?,?)", (name, value))
        self.put("meta", "seed", {"id": "seed", "seeded_at": iso(), "anchor": world.get("anchor")})
        return self

    def stats(self):
        with self._lock:
            rows = self.conn.execute("SELECT collection, COUNT(*) FROM docs GROUP BY collection").fetchall()
        return dict(rows)


_DEFAULT = None


def get_store():
    """Process-wide default store (used by the MCP server and evaluation)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Store()
    return _DEFAULT
