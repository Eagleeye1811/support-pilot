"""MCP tool bus — the orchestrator's MCP client.

Every agent call goes through a real Model Context Protocol session: an in-process
``mcp.Client`` talks JSON-RPC to an ``MCPServer`` whose tools are the agents
(``tools.py``). One asyncio loop on a background thread hosts all sessions, so the
synchronous Streamlit script and the Telegram thread can call tools safely.

``SUPPORTPILOT_MCP=0`` (or any MCP failure) falls back to direct in-process calls,
marked ``transport="direct"`` in the trace.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import weakref

from . import tools as T

try:  # mcp >= 2.0
    from mcp import Client
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - mcp 1.x has no in-process Client; use direct calls
    Client = MCPServer = None

SERVER_NAME = "supportpilot-agents"
_loop = None
_loop_lock = threading.Lock()
_buses = weakref.WeakKeyDictionary()


def mcp_enabled():
    return Client is not None and os.getenv("SUPPORTPILOT_MCP", "1").lower() not in ("0", "false", "no")


def _get_loop():
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="mcp-bus", daemon=True).start()
        return _loop


def build_server(store, name=SERVER_NAME, instructions=None):
    """An MCP server exposing every registered agent tool, bound to `store`."""
    server = MCPServer(name, instructions=instructions)
    add_agent_tools(server, store)
    return server


def add_agent_tools(server, store):
    for tool_name, (fn, label) in T.TOOLS.items():
        doc = (fn.__doc__ or "").strip()
        server.add_tool(T.bind(fn, store), name=tool_name, description=f"[{label}] {doc}")


def preview(value, limit=600):
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


class ToolBus:
    """Synchronous facade over one MCP client session for one store."""

    def __init__(self, store):
        self.store = store
        self.client = None
        self.error = None
        self._closed = None
        if mcp_enabled():
            try:
                self._ready = threading.Event()
                asyncio.run_coroutine_threadsafe(self._session(build_server(store)), _get_loop())
                if not self._ready.wait(10):
                    raise TimeoutError("MCP session did not start")
                if self.error:
                    raise RuntimeError(self.error)
            except Exception as exc:  # keep the desk working without MCP
                self.client, self.error = None, f"{type(exc).__name__}: {exc}"

    async def _session(self, server):
        self._closed = asyncio.Event()
        try:
            async with Client(server) as client:
                self.client = client
                self._ready.set()
                await self._closed.wait()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.client = None
            self._ready.set()

    @property
    def transport(self):
        return "mcp" if self.client else "direct"

    def call(self, name, args):
        """Call a tool; returns (result, record) where record describes the call for the trace."""
        t0 = time.perf_counter()
        transport = self.transport
        if transport == "mcp":
            fut = asyncio.run_coroutine_threadsafe(self.client.call_tool(name, T.jsonable(args)), _get_loop())
            res = fut.result(60)
            if res.is_error:
                raise RuntimeError(res.content[0].text if res.content else f"MCP tool {name} failed")
            result = (res.structured_content or {}).get("value")
        else:
            result = T.call_direct(self.store, name, args)
        record = dict(tool=name, transport=transport, ms=round((time.perf_counter() - t0) * 1000, 1),
                      args=preview(args), result=preview(result))
        return result, record

    def list_tools(self):
        if self.client:
            res = asyncio.run_coroutine_threadsafe(self.client.list_tools(), _get_loop()).result(10)
            return [dict(name=t.name, description=t.description or "") for t in res.tools]
        return [dict(name=n, description=f"[{label}] {(fn.__doc__ or '').strip()}") for n, (fn, label) in T.TOOLS.items()]

    def close(self):
        if self._closed is not None and _loop is not None:
            _loop.call_soon_threadsafe(self._closed.set)


def get_bus(store):
    """The bus for `store`, created on first use (one MCP session per store)."""
    bus = _buses.get(store)
    if bus is None:
        bus = ToolBus(store)
        _buses[store] = bus
        weakref.finalize(store, bus.close)
    return bus
