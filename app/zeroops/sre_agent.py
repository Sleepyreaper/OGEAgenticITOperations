"""Minimal Azure SRE Agent data-plane client: hand a detection to the agent as a new thread.

Settings:
  SRE_AGENT_ENDPOINT  -- the agent's data-plane URL (ARM ``properties.agentEndpoint``).
  SRE_AGENT_SUBAGENT  -- custom agent to address (blank = ``zeroops-triage``; ``none`` = the default agent).

The app identity needs the **SRE Agent Standard User** role on the agent resource.
When the endpoint is not set, callers get an explicit ``not_configured`` status.
"""
import os
import re

import requests

from app.activity import store as activity_store_module

SCOPE = "https://azuresre.ai/.default"
DEFAULT_SUBAGENT = "zeroops-triage"
_SAFE_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_KIND_FALLBACKS = {
    "opened": "investigation_opened",
    "sre_thread_result": "review_requested",
}


class SreAgentError(Exception):
    def __init__(self, message: str, *, investigation_id: str = None):
        super().__init__(message)
        self.investigation_id = investigation_id


def endpoint() -> str:
    return os.environ.get("SRE_AGENT_ENDPOINT", "").strip().rstrip("/")


def subagent() -> str:
    value = os.environ.get("SRE_AGENT_SUBAGENT", "").strip() or DEFAULT_SUBAGENT
    return "" if value.lower() == "none" else value


def configured() -> bool:
    return bool(endpoint())


def _token() -> str:
    from app.azure_data import _credential
    return _credential().get_token(SCOPE).token


def _event_kind(preferred: str) -> str:
    if preferred in activity_store_module.EVENT_KINDS:
        return preferred
    fallback = _EVENT_KIND_FALLBACKS[preferred]
    if fallback in activity_store_module.EVENT_KINDS:
        return fallback
    raise RuntimeError(f"activity store cannot persist event kind {preferred!r}")


def _open_thread_receipt(*, scenario_id: str = "") -> tuple:
    store = activity_store_module.get_activity_store()
    investigation = store.open_investigation(
        origin="sre_handoff",
        scenario_id=scenario_id or None,
    )
    store.append_event(
        investigation["id"],
        actor="system",
        kind=_event_kind("opened"),
        provenance="reported",
        payload={"source": "api"},
    )
    return store, investigation["id"]


def _record_thread_result(
    store,
    investigation_id: str,
    *,
    status: str,
    thread_id: str = "",
    reason_code: str = "",
) -> None:
    payload = {"status": status}
    if thread_id and _SAFE_THREAD_ID.fullmatch(thread_id):
        payload["thread_id"] = thread_id
    if reason_code:
        payload["reason_code"] = reason_code
    store.append_event(
        investigation_id,
        actor="system",
        kind=_event_kind("sre_thread_result"),
        provenance="observed",
        payload=payload,
    )
    store.update_investigation(
        investigation_id,
        phase="opened" if status in ("ok", "not_configured") else "failed",
    )


def start_thread(message: str, *, scenario_id: str = "") -> dict:
    """Open a new SRE Agent thread with ``message``; returns ``{status, thread_id, title}``."""
    if not message or not message.strip():
        raise ValueError("message is required")
    store, investigation_id = _open_thread_receipt(scenario_id=scenario_id)
    base = endpoint()
    if not base:
        _record_thread_result(
            store,
            investigation_id,
            status="not_configured",
            reason_code="endpoint_not_configured",
        )
        return {
            "status": "not_configured",
            "hint": "set SRE_AGENT_ENDPOINT to the agent's agentEndpoint",
            "investigation_id": investigation_id,
        }
    name = subagent()
    text = f"/agent {name} {message}" if name else message
    try:
        resp = requests.post(f"{base}/api/v1/threads", json={"StartMessage": {"text": text[:8000]}},
                             headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    except requests.RequestException as exc:
        _record_thread_result(store, investigation_id, status="error", reason_code="request_failed")
        raise SreAgentError(
            f"SRE Agent unreachable: {type(exc).__name__}",
            investigation_id=investigation_id,
        ) from exc
    if resp.status_code >= 400:
        _record_thread_result(store, investigation_id, status="error", reason_code="provider_rejected")
        raise SreAgentError(
            f"SRE Agent rejected the thread ({resp.status_code}): {resp.text[:300]}",
            investigation_id=investigation_id,
        )
    data = resp.json() if resp.content else {}
    thread_id = data.get("id") or data.get("threadId")
    if not thread_id or not isinstance(thread_id, str) or not _SAFE_THREAD_ID.fullmatch(thread_id):
        _record_thread_result(store, investigation_id, status="error", reason_code="invalid_thread_id")
        raise SreAgentError(
            "SRE Agent response had no public-safe thread id",
            investigation_id=investigation_id,
        )
    _record_thread_result(store, investigation_id, status="ok", thread_id=thread_id)
    return {
        "status": "ok",
        "thread_id": thread_id,
        "title": data.get("title", ""),
        "subagent": name,
        "investigation_id": investigation_id,
    }
