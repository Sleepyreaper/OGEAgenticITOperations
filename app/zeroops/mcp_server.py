"""MCP server for the Azure SRE Agent -> OGE squad hand-off.

Stateless Streamable HTTP transport (MCP 2025-03-26 / 2025-06-18): each
POST /mcp carries one JSON-RPC 2.0 message and gets one
``application/json`` response. No SSE stream or session is required, which
keeps it compatible with Gunicorn sync workers and App Service scale-out.

Auth: ``X-API-Key: <key>`` or ``Authorization: Bearer <key>`` compared in
constant time against ``MCP_API_KEY``. When ``MCP_API_KEY`` is unset the
endpoint is disabled (503) -- it never runs open.

Add it to the SRE Agent as a Streamable-HTTP MCP connector named
``ogeops``; the tools appear as ``ogeops_<tool>`` (see sre-agent/).
"""
import hmac
import json
import os
import traceback

from flask import Blueprint, jsonify, request

from app import __version__ as APP_VERSION
from app.agents import backend as agent_backend
from app.agents import tools as agent_tools
from app.agents.catalog import build_agent_catalog
from app.config import settings
from app.operations.models import FindingCategory
from app.zeroops import cost as cost_model
from app.zeroops import scenarios as scenario_mod
from app.zeroops import service

mcp_bp = Blueprint("zeroops_mcp", __name__)

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "ogeops"

_SEVERITIES = ["critical", "high", "medium", "low", "informational"]
_CATEGORIES = sorted(member.value for member in FindingCategory)


def _tool(name, description, properties, required=(), read_only=True):
    return {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
    }


TOOL_DEFINITIONS = [
    _tool(
        "escalate",
        "Escalate an incident the SRE Agent cannot safely resolve to the OGE agent squad. The squad routes it to "
        "specialists (cost, reliability, security, policy, resilience), optionally debates, and returns a grounded "
        "root cause, business impact, recommended actions and token cost. Returns an escalation_id; if status is "
        "'received' or 'analyzing', poll get_escalation. Never changes Azure resources.",
        {
            "question": {"type": "string", "description": "What the squad should decide or explain. Optional when scenario_id is set."},
            "sre_summary": {"type": "string", "description": "Your triage so far: signals, evidence, what you ruled out and why you are escalating."},
            "incident_ref": {"type": "string", "description": "Alert/incident id or URL."},
            "scenario_id": {"type": "string", "description": "Known demo scenario id, if this incident matches one (see list_scenarios)."},
            "severity": {"type": "string", "enum": _SEVERITIES},
            "category": {"type": "string", "enum": _CATEGORIES, "description": "Optional finding category to focus the squad on."},
            "debate": {"type": "boolean", "description": "Force a cross-specialist debate round (more tokens, better for trade-offs)."},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": service.MAX_WAIT_SECONDS},
        },
        read_only=False,
    ),
    _tool("get_escalation", "Status and result of an escalation: conclusion, impact, actions, remediation script, cost.",
          {"escalation_id": {"type": "string"}}, ["escalation_id"]),
    _tool("propose_fix", "Turn a finished escalation into a human-approval proposal (ADO work item when configured). "
          "Nothing is executed until a human approves.",
          {"escalation_id": {"type": "string"}}, ["escalation_id"], read_only=False),
    _tool("list_findings", "Priority-ordered operations findings from the deterministic evidence layer.",
          {"category": {"type": "string"}, "severity": {"type": "string"}, "status": {"type": "string"},
           "page_size": {"type": "integer", "minimum": 1, "maximum": 50}}),
    _tool("get_evidence", "Bounded evidence for exactly one finding id.", {"finding_id": {"type": "string"}}, ["finding_id"]),
    _tool("agent_catalog", "Who is on the OGE squad, what each agent handles and how.", {}),
    _tool("list_scenarios", "ZeroOps demo scenarios and whether each is SRE-solo or squad-escalated.", {}),
    _tool("cost_model", "SRE Agent AAU + squad token cost model used for per-incident cost.", {}),
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOL_DEFINITIONS}


class ToolInputError(ValueError):
    pass


def api_key_configured() -> bool:
    return bool(os.environ.get("MCP_API_KEY", "").strip())


def _authorized() -> bool:
    expected = os.environ.get("MCP_API_KEY", "").strip()
    if not expected:
        return False
    supplied = request.headers.get("X-API-Key", "").strip()
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def _rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _text_content(payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str, indent=1)
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(payload, dict) and not is_error:
        result["structuredContent"] = payload
    return result


def _validate(tool: dict, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ToolInputError("arguments must be an object")
    props = tool["inputSchema"]["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolInputError(f"unknown argument(s) {unknown}")
    for key in tool["inputSchema"]["required"]:
        if not str(args.get(key, "")).strip():
            raise ToolInputError(f"{key} is required")
    for key, value in args.items():
        expected = props[key].get("type")
        if expected == "string" and not isinstance(value, str):
            raise ToolInputError(f"{key} must be a string")
        if expected == "boolean" and not isinstance(value, bool):
            raise ToolInputError(f"{key} must be a boolean")
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ToolInputError(f"{key} must be an integer")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise ToolInputError(f"{key} must be one of {props[key]['enum']}")
    return args


def _scoped_tool(tool_name: str, tool_args: dict) -> dict:
    """Run a read-only squad tool scoped to the ZeroOps subscription(s); non-ok results become tool errors."""
    subscription_ids = service._subscription_ids()
    if not subscription_ids:
        raise ToolInputError("no subscription configured (ZEROOPS_SUBSCRIPTION_ID or AZURE_SUBSCRIPTION_ID)")
    out = agent_tools.execute_tool(tool_name, {"subscription_ids": subscription_ids, **tool_args}).to_dict()
    if out.get("status") != "ok":
        raise ToolInputError(f"{tool_name}: {out.get('status')}: {out.get('error')}")
    return out


def _escalation_view(record: dict) -> dict:
    if not record:
        return {}
    result = record.get("result") or {}
    view = {
        "escalation_id": record["id"], "status": record["status"], "scenario_id": record.get("scenario_id"),
        "incident_ref": record.get("incident_ref"), "created_at": record["created_at"], "updated_at": record["updated_at"],
        "conclusion": result.get("conclusion"), "business_impact": result.get("business_impact"),
        "confidence": result.get("confidence"), "recommended_actions": result.get("recommended_actions"),
        "remediation_script": result.get("remediation_script"), "evidence_ids": result.get("valid_evidence_ids"),
        "specialists": result.get("specialists"), "debate_used": result.get("debate_used"),
        "answered_by": result.get("agent"), "synthesis_fallback": bool(result.get("fallback_from")),
        "cost": record.get("cost"), "proposal_id": record.get("proposal_id") or None, "error": record.get("error") or None,
    }
    if record["status"] in ("received", "analyzing"):
        view["next_step"] = "Squad is still reasoning. Call get_escalation with this escalation_id in ~30 seconds."
    elif record["status"] == "proposed":
        view["next_step"] = "Review the result; call propose_fix to raise it for human approval."
    return view


def _tool_result(data: dict):
    return {k: v for k, v in data.items() if v not in (None, "", [])}


def call_tool(name: str, args: dict) -> dict:
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    args = _validate(tool, args or {})
    if name == "escalate":
        record = service.escalate(
            question=args.get("question", ""), source="sre-agent", incident_ref=args.get("incident_ref", ""),
            sre_summary=args.get("sre_summary", ""), scenario_id=args.get("scenario_id", ""),
            severity=args.get("severity", ""), category=args.get("category", ""), debate=args.get("debate", False),
            wait_seconds=args.get("wait_seconds", service.DEFAULT_WAIT_SECONDS),
        )
        return _escalation_view(record)
    if name == "get_escalation":
        record = service.get_escalation(args["escalation_id"])
        if record is None:
            raise ToolInputError(f"escalation {args['escalation_id']!r} not found")
        return _escalation_view(record)
    if name == "propose_fix":
        out = service.propose_fix(args["escalation_id"])
        proposal = out["proposal"] or {}
        return {"escalation": _escalation_view(out["escalation"]),
                "proposal": {k: proposal.get(k) for k in ("id", "status", "title", "proposal_type", "approval_tier", "ado_url") if k in proposal},
                "next_step": "A human must approve this proposal in the OGE ops ZeroOps view (or ADO) before anything changes."}
    if name == "list_findings":
        tool_args = {k: v for k, v in args.items() if k in ("category", "severity", "status", "page_size")}
        return _scoped_tool("list_prioritized_findings", tool_args)
    if name == "get_evidence":
        return _scoped_tool("get_finding_evidence", {"finding_id": args["finding_id"]})
    if name == "agent_catalog":
        health = agent_backend.backend_health()
        agents = build_agent_catalog(settings.agents, backend=health["active_backend"], foundry_agent_prefix=health["foundry_agent_prefix"])
        return {"agents": [{k: a.get(k) for k in ("key", "name", "function", "what", "how", "handles", "tools") if k in a} for a in agents]}
    if name == "list_scenarios":
        return {"scenarios": [{k: s[k] for k in ("id", "title", "tier", "tagline", "why_escalate", "configured")} for s in scenario_mod.list_scenarios()]}
    if name == "cost_model":
        return cost_model.monthly_baseline()
    raise KeyError(name)


def handle_message(msg) -> dict:
    """Dispatch one JSON-RPC message. Returns None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    is_notification = "id" not in msg
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        result = _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": f"{settings.brand.app_name if settings.brand else 'OGE'} agent squad", "version": APP_VERSION},
            "instructions": (
                "Tier-2 escalation for the Azure SRE Agent. Use escalate when an incident is cross-domain, "
                "business-impacting, or has no safe runbook action. Poll get_escalation until status is 'proposed', "
                "then call propose_fix. Humans approve every change."
            ),
        })
    elif method == "ping":
        result = _rpc_result(msg_id, {})
    elif method == "tools/list":
        result = _rpc_result(msg_id, {"tools": TOOL_DEFINITIONS})
    elif method == "tools/call":
        name = params.get("name")
        if name not in _TOOLS_BY_NAME:
            result = _rpc_error(msg_id, -32602, f"Unknown tool: {name}")
        else:
            try:
                result = _rpc_result(msg_id, _text_content(_tool_result(call_tool(name, params.get("arguments") or {}))))
            except (ToolInputError, ValueError, KeyError) as exc:
                result = _rpc_result(msg_id, _text_content(f"{name} failed: {exc}", is_error=True))
            except Exception as exc:  # noqa: BLE001 -- tool boundary: surface as an MCP tool error, never a 500
                traceback.print_exc()
                result = _rpc_result(msg_id, _text_content(f"{name} failed: {exc}", is_error=True))
    else:
        result = _rpc_error(msg_id, -32601, f"Method not found: {method}")
    return None if is_notification else result


@mcp_bp.route("/mcp", methods=["POST", "GET", "DELETE"])
def mcp_endpoint():
    if not api_key_configured():
        return jsonify({"error": "MCP endpoint disabled: set MCP_API_KEY"}), 503
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401, {"WWW-Authenticate": 'Bearer realm="ogeops-mcp"'}
    if request.method == "GET":
        return jsonify({"error": "SSE stream not supported; POST JSON-RPC messages"}), 405, {"Allow": "POST"}
    if request.method == "DELETE":
        return "", 204
    body = request.get_json(silent=True)
    if body is None:
        return jsonify(_rpc_error(None, -32700, "Parse error")), 400
    if isinstance(body, list):
        responses = [r for r in (handle_message(m) for m in body) if r is not None]
        return (jsonify(responses), 200) if responses else ("", 202)
    response = handle_message(body)
    if response is None:
        return "", 202
    return jsonify(response), 200
