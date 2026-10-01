"""Minimal Azure SRE Agent data-plane client: hand a detection to the agent as a new thread.

Settings:
  SRE_AGENT_ENDPOINT  -- the agent's data-plane URL (ARM ``properties.agentEndpoint``).
  SRE_AGENT_SUBAGENT  -- custom agent to address (blank = ``zeroops-triage``; ``none`` = the default agent).

The app identity needs the **SRE Agent Standard User** role on the agent resource.
When the endpoint is not set, callers get an explicit ``not_configured`` status.
"""
import os

import requests

SCOPE = "https://azuresre.ai/.default"
DEFAULT_SUBAGENT = "zeroops-triage"


class SreAgentError(Exception):
    pass


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


def start_thread(message: str) -> dict:
    """Open a new SRE Agent thread with ``message``; returns ``{status, thread_id, title}``."""
    base = endpoint()
    if not base:
        return {"status": "not_configured", "hint": "set SRE_AGENT_ENDPOINT to the agent's agentEndpoint"}
    if not message or not message.strip():
        raise ValueError("message is required")
    name = subagent()
    text = f"/agent {name} {message}" if name else message
    try:
        resp = requests.post(f"{base}/api/v1/threads", json={"StartMessage": {"text": text[:8000]}},
                             headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    except requests.RequestException as exc:
        raise SreAgentError(f"SRE Agent unreachable: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise SreAgentError(f"SRE Agent rejected the thread ({resp.status_code}): {resp.text[:300]}")
    data = resp.json() if resp.content else {}
    thread_id = data.get("id") or data.get("threadId")
    if not thread_id:
        raise SreAgentError("SRE Agent response had no thread id")
    return {"status": "ok", "thread_id": thread_id, "title": data.get("title", ""), "subagent": name}
