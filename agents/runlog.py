"""Per-run recorder: collects the tool calls and LLM calls made while one workflow run executes.

The orchestrator opens a recorder for each customer message (``with runlog.recording() as rec``). It lives in
a context variable, so concurrent runs — Streamlit sessions, the Telegram thread and API requests — never mix
their records. Outside a run, recording is a no-op.
"""
from __future__ import annotations

import contextlib
import contextvars

_current = contextvars.ContextVar("supportpilot_run", default=None)
MAX_TOOL_CALLS = 200


class Recorder:
    def __init__(self):
        self.tool_calls = []
        self.llm_calls = []


@contextlib.contextmanager
def recording():
    rec = Recorder()
    token = _current.set(rec)
    try:
        yield rec
    finally:
        _current.reset(token)


def record_tool(call):
    rec = _current.get()
    if rec is not None and len(rec.tool_calls) < MAX_TOOL_CALLS:
        rec.tool_calls.append(call)


def record_llm(call):
    rec = _current.get()
    if rec is not None:
        rec.llm_calls.append(call)


def update_last_llm(**fields):
    rec = _current.get()
    if rec is not None and rec.llm_calls:
        rec.llm_calls[-1].update(fields)
