"""Server-enforced PUBLIC_DEMO_MODE gate.

When ``PUBLIC_DEMO_MODE`` is true the app is safe to expose to anonymous
internet users: only a fixed, exact-path, read-only allowlist is served and
every other ``/api/*`` request (live collection, analysis, tools, ZeroOps
mutation, ADO, chaos, handoff, decisions, anything new) gets an explicit
JSON 403. The decision is made centrally in one ``before_request`` hook so
route modules never invent their own checks.

This is NOT operator authentication or RBAC. It removes the live/mutating
surface from public reach; it does not identify callers. Operator access is
a separate Easy Auth / Entra / RBAC feature (see docs/PUBLIC_DEMO_SECURITY.md).
No secret is read, sent to browser code, or logged here.
"""

from __future__ import annotations

import os
import re

from flask import Flask, jsonify, request

ENV_VAR = "PUBLIC_DEMO_MODE"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Exact paths only -- never prefixes. Each is a fixture or static capability
# read that touches neither Azure, a model backend, the ledger nor any store.
PUBLIC_READ_PATHS = frozenset({
    "/api/health",           # configuration-presence booleans only
    "/api/agents",           # derived-from-code agent catalog
    "/api/demos",            # static demo scenarios
    "/api/operations/demo",  # centralized demo fixture (no Azure / LLM calls)
    "/api/activity",         # activity feed (read-only Blueprint)
})

# GET /api/activity/<id>: a single opaque id segment, no separators.
_ACTIVITY_ITEM_RE = re.compile(r"^/api/activity/[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")

_MCP_PATH = "/mcp"  # keeps its own API-key auth and 401/503 semantics
_STATIC_PREFIX = "/static/"


def parse_public_demo_mode(raw: str | None) -> bool:
    """Strictly parse the flag; unset/empty means False. Raises ValueError
    for an unrecognized value so a typo cannot silently disable the gate."""
    value = (raw or "").strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{ENV_VAR}: expected a boolean (true/false/1/0/yes/no/on/off), got {raw!r}."
    )


def public_demo_mode_enabled() -> bool:
    """Current flag value. An unparseable value fails closed (enabled)."""
    try:
        return parse_public_demo_mode(os.environ.get(ENV_VAR))
    except ValueError:
        return True


def _normalized_path(path: str) -> str:
    return re.sub(r"/{2,}", "/", path)


def is_allowed_in_public_mode(method: str, path: str) -> bool:
    """Pure allowlist decision for a request while PUBLIC_DEMO_MODE is true."""
    method = method.upper()
    path = _normalized_path(path)

    if path == _MCP_PATH:
        return True  # MCP enforces its own API key (401/503)

    if path.lower() == "/api" or path.lower().startswith("/api/"):
        if method not in _READ_METHODS:
            return False
        return path in PUBLIC_READ_PATHS or bool(_ACTIVITY_ITEM_RE.match(path))

    # Pages and static assets: read-only navigation stays usable.
    return method in _READ_METHODS


def _deny():
    response = jsonify({
        "error": "forbidden_in_public_demo_mode",
        "message": "This operation is not available on the public demo.",
        "public_demo_mode": True,
    })
    response.status_code = 403
    response.headers["Cache-Control"] = "no-store"
    return response


def install_public_demo_gate(app: Flask) -> None:
    """Validate the flag at startup (fail loudly on a typo) and install the
    central request gate. The flag is re-read per request so a trusted
    deployment (flag false/unset) keeps today's behavior untouched."""
    parse_public_demo_mode(os.environ.get(ENV_VAR))

    @app.before_request
    def _public_demo_gate():
        if not public_demo_mode_enabled():
            return None
        if is_allowed_in_public_mode(request.method, request.path):
            return None
        return _deny()
