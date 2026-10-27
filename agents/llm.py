"""LLM gateway — the single place SupportPilot talks to a language model.

Provider: Groq's OpenAI-compatible chat API (the existing setup). Settings, from the environment, ``.env`` or
Streamlit secrets: ``GROQ_API_KEY`` (required to enable), ``GROQ_MODEL`` (default llama-3.3-70b-versatile),
``GROQ_BASE_URL`` (default https://api.groq.com/openai/v1, any OpenAI-compatible endpoint works),
``GROQ_FALLBACK_MODELS`` (comma-separated; tried when the configured model is retired or unavailable) and
``SUPPORTPILOT_DISABLE_LLM=1`` to force the deterministic fallback.

Every call returns an ``LLMResult`` (never raises): ``ok`` plus the text, or the reason it failed — missing key,
HTTP status, timeout, invalid JSON. Callers fall back to deterministic rules on any failure, and each call is
recorded on the current workflow run so the UI and API can show exactly when the fallback was used.
The LLM never chooses or authorizes actions: it only reads the customer's message and words the reply.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass

from . import runlog
from .shared_agent import get_secret, iso

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_FALLBACK_MODELS = "openai/gpt-oss-120b,openai/gpt-oss-20b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
TIMEOUT_S = 20

_lock = threading.Lock()
_last = dict(ok=None, error=None, at=None, model=None)
_unavailable = set()  # models the provider said do not exist (per process)


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    error: str = ""
    model: str = ""
    ms: float = 0.0
    purpose: str = ""

    def json(self):
        """Parse the reply as a JSON object; None when it is not one."""
        if not self.ok:
            return None
        try:
            data = json.loads(self.text)
        except Exception:
            match = re.search(r"\{.*\}", self.text, re.S)
            try:
                data = json.loads(match.group(0)) if match else None
            except Exception:
                data = None
        return data if isinstance(data, dict) else None


def disabled_by_env():
    return os.getenv("SUPPORTPILOT_DISABLE_LLM", "").lower() in ("1", "true", "yes")


def api_key():
    return get_secret("GROQ_API_KEY")


def configured_model():
    return get_secret("GROQ_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL


def candidate_models():
    fallbacks = (get_secret("GROQ_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS) or "").split(",")
    seen = []
    for m in [configured_model()] + [f.strip() for f in fallbacks]:
        if m and m not in seen and m not in _unavailable:
            seen.append(m)
    return seen


def model_name():
    """The model calls will use: the configured one, unless the provider reported it unavailable."""
    models = candidate_models()
    return models[0] if models else configured_model()


def enabled():
    return not disabled_by_env() and bool(api_key())


def status():
    """Configuration and health of the LLM, for the dashboard, the API and each turn's record."""
    if disabled_by_env():
        mode, reason = "fallback", "disabled by SUPPORTPILOT_DISABLE_LLM"
    elif not api_key():
        mode, reason = "fallback", "GROQ_API_KEY not configured"
    elif _last["ok"] is False:
        mode, reason = "degraded", f"last call failed: {_last['error']}"
    else:
        mode, reason = "llm", "ready" if _last["ok"] is None else "last call succeeded"
    with _lock:
        last = dict(_last)
    note = (f"configured model {configured_model()} is unavailable; using {model_name()}"
            if configured_model() in _unavailable else None)
    return dict(mode=mode, reason=reason, provider="Groq (OpenAI-compatible)", model=model_name(), configured=bool(api_key()),
                configured_model=configured_model(), model_note=note, last_call=last)


def chat(prompt, system_msg=None, max_tokens=400, json_mode=False, purpose="chat", temperature=0.2):
    """One chat completion. Never raises; the outcome is recorded on the active workflow run."""
    if disabled_by_env():
        return _record(LLMResult(False, error="disabled", model=model_name(), purpose=purpose))
    key = api_key()
    if not key:
        return _record(LLMResult(False, error="not configured", model=model_name(), purpose=purpose))
    t0 = time.time()
    res = LLMResult(False, error="no model available", model=configured_model())
    for model in candidate_models():
        res = _call(key, model, prompt, system_msg, max_tokens, json_mode, temperature)
        if res.ok or not res.error.startswith("HTTP 404"):
            break
        _unavailable.add(model)  # retired / not enabled for this key: try the next candidate
    res.ms = round((time.time() - t0) * 1000, 1)
    res.purpose = purpose
    return _record(res)


def _call(key, model, prompt, system_msg, max_tokens, json_mode, temperature):
    reasoning = model.startswith("openai/gpt-oss")
    payload = dict(model=model, temperature=temperature, max_tokens=max_tokens + (600 if reasoning else 0), messages=[
        dict(role="system", content=system_msg or "You are a careful customer support assistant. Never invent facts."),
        dict(role="user", content=prompt)])
    if reasoning:
        payload["reasoning_effort"] = "low"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    base = (get_secret("GROQ_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    try:
        import requests
        resp = requests.post(f"{base}/chat/completions", json=payload, timeout=TIMEOUT_S,
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")[:120]
            except Exception:
                pass
            return LLMResult(False, error=f"HTTP {resp.status_code}" + (f": {detail}" if detail else ""), model=model)
        text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        return LLMResult(bool(text), text=text, error="" if text else "empty response", model=model)
    except Exception as exc:  # timeout, DNS, malformed body … — all mean "use the fallback"
        name = type(exc).__name__
        return LLMResult(False, error="timeout" if "Timeout" in name else f"{name}: {str(exc)[:120]}", model=model)


def chat_json(prompt, system_msg=None, max_tokens=300, purpose="json"):
    res = chat(prompt, system_msg=system_msg, max_tokens=max_tokens, json_mode=True, purpose=purpose)
    data = res.json()
    if res.ok and data is None:
        res.ok, res.error = False, "invalid JSON"
        runlog.update_last_llm(ok=False, error=res.error)
    return res, data


def _record(res):
    if res.error not in ("disabled", "not configured"):
        with _lock:
            _last.update(ok=res.ok, error=res.error or None, at=iso(), model=res.model)
    runlog.record_llm(dict(asdict(res), text=None))
    return res
