"""Per-tenant analytics warehouse: a separate SQLite file of cleaned, derived tables.

It is rebuilt by the pipeline and can be deleted at any time; it holds no decisions (those live in the tenant's
support store). Live support never reads it, so a slow or failed analytics run cannot affect customers.
Path: ``data/analytics/<tenant>.db`` (``SUPPORTPILOT_ANALYTICS_DIR`` to relocate).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

import pandas as pd

from agents.shared_agent import ROOT

TIME_COLUMNS = {"created_at", "resolved_at", "first_response_at", "started_at", "ts", "start", "end"}
BOOL_COLUMNS = {"escalated", "reopened", "sla_breached", "resolved", "complaint", "simulated"}  # SQLite stores 0/1


def analytics_path(tenant_id):
    base = os.getenv("SUPPORTPILOT_ANALYTICS_DIR") or os.path.join(ROOT, "data", "analytics")
    return os.path.join(base, f"{tenant_id}.db")


class Warehouse:
    def __init__(self, path):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)

    def write(self, table, df):
        if len(df.columns) == 0:  # nothing to store (e.g. no gateway stats for this tenant): drop any stale table
            with self._lock, self.conn:
                self.conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            return
        out = df.copy()
        for c in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[c]):
                out[c] = out[c].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            elif out[c].dtype == object and out[c].map(lambda v: isinstance(v, (dict, list))).any():
                out[c] = out[c].map(lambda v: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
            elif out[c].dtype == object:
                out[c] = out[c].map(lambda v: v.isoformat() if hasattr(v, "isoformat") else v)
        with self._lock:
            out.to_sql(table, self.conn, if_exists="replace", index=False)

    def read(self, table):
        with self._lock:
            try:
                df = pd.read_sql_query(f'SELECT * FROM "{table}"', self.conn)
            except Exception:
                return pd.DataFrame()
        for c in df.columns:
            if c in TIME_COLUMNS:
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce", format="ISO8601")
        for c in BOOL_COLUMNS & set(df.columns):
            df[c] = df[c].fillna(0).astype(bool)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def put_json(self, key, value):
        with self._lock, self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value, default=str)))

    def get_json(self, key, default=None):
        with self._lock:
            try:
                row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            except sqlite3.OperationalError:
                return default
        return json.loads(row[0]) if row else default

    def tables(self):
        with self._lock:
            return [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
