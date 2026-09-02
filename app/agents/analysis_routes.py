"""Flask routes for evidence-grounded agent analysis, mounted under
/api/operations (see app/main.py's create_app(), which registers
``agent_analysis_bp`` alongside app/operations/routes.py's
``operations_bp``).

Unlike app/operations/routes.py (deterministic evidence only, no LLM
call -- see that module's docstring), every route here DOES call a
model backend (app/agents/backend.py) through app/agents/analysis.py.
Kept in a separate blueprint/module specifically so that invariant in
app/operations/routes.py stays true and easy to verify by inspection.
"""

import traceback

from flask import Blueprint, jsonify, request

from app import azure_data
from app.agents import analysis as analysis_service
from app.agents import tools as tools_service
from app.agents.evidence import EvidenceBundleError
from app.config import settings
from app.operations.config import OperationsConfigError
from app.operations.errors import OperationsCollectionError

agent_analysis_bp = Blueprint("agent_analysis", __name__, url_prefix="/api/operations")


def _parse_sub_ids() -> list:
    """Same ?subs= semantics as app.operations.routes._parse_sub_ids."""
    subs_param = request.args.get("subs", "").strip()
    if subs_param == "all":
        all_subs = azure_data.list_subscriptions()
        return [s["id"] for s in all_subs if s.get("state") == "Enabled"]
    if subs_param:
        return [s.strip() for s in subs_param.split(",") if s.strip()]
    return [settings.subscription_id]


def _bool_flag(value, query_name: str) -> bool:
    if isinstance(value, bool):
        return value
    return request.args.get(query_name, "").strip().lower() in ("1", "true", "yes", "on")


def _request_body() -> dict:
    if request.method != "POST" or not request.is_json:
        return {}
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _str_param(body: dict, key: str) -> str:
    return str(body.get(key) or request.args.get(key, "") or "").strip()


def _resolve_subscription_ids(body: dict) -> list:
    subs = body.get("subs")
    if isinstance(subs, list) and all(isinstance(s, str) for s in subs):
        return subs
    return _parse_sub_ids()


def _handle_analysis_error(exc: analysis_service.AnalysisError):
    return jsonify({"error": str(exc)}), 400


def _handle_evidence_bundle_error(exc: EvidenceBundleError):
    status_code = 404 if "not found" in str(exc) else 400
    return jsonify({"error": str(exc)}), status_code


@agent_analysis_bp.route("/analyze", methods=["GET", "POST"])
def analyze():
    """GET/POST /api/operations/analyze

    Query params (GET) or JSON body (POST): question (required), subs,
    category, severity, status, finding_id, agents (list[str]),
    debate (bool), refresh (bool).
    """
    body = _request_body()
    question = _str_param(body, "question")
    subscription_ids = _resolve_subscription_ids(body)
    category = _str_param(body, "category") or None
    severity = _str_param(body, "severity") or None
    status = _str_param(body, "status") or None
    finding_id = _str_param(body, "finding_id") or None
    requested_agents = body.get("agents")
    if not (isinstance(requested_agents, list) and all(isinstance(a, str) for a in requested_agents)):
        requested_agents = None
    force_debate = _bool_flag(body.get("debate"), "debate")
    force_refresh = _bool_flag(body.get("refresh"), "refresh")

    if not question:
        return jsonify({"error": "question is required"}), 400
    if not subscription_ids or not any(subscription_ids):
        return jsonify({"error": "no subscription configured -- pass subs or configure AZURE_SUBSCRIPTION_ID"}), 400

    try:
        result = analysis_service.analyze_operations(
            question=question, subscription_ids=subscription_ids, category=category, severity=severity,
            status=status, finding_id=finding_id, requested_agents=requested_agents,
            force_debate=force_debate, force_refresh=force_refresh,
        )
        return jsonify(result)
    except analysis_service.AnalysisError as exc:
        return _handle_analysis_error(exc)
    except EvidenceBundleError as exc:
        return _handle_evidence_bundle_error(exc)
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except NotImplementedError as exc:
        return jsonify({"error": str(exc)}), 501
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary, same convention as app/operations/routes.py
        traceback.print_exc()
        return jsonify({"error": "failed to analyze operations evidence", "detail": str(exc)}), 500


@agent_analysis_bp.route("/briefing", methods=["GET", "POST"])
def briefing():
    """GET/POST /api/operations/briefing

    Query params (GET) or JSON body (POST): subs, category, severity,
    status, debate (bool), refresh (bool). Always synthesizes one
    coordinator-voice executive briefing -- see
    app/agents/analysis.py::build_briefing.
    """
    body = _request_body()
    subscription_ids = _resolve_subscription_ids(body)
    category = _str_param(body, "category") or None
    severity = _str_param(body, "severity") or None
    status = _str_param(body, "status") or None
    force_debate = _bool_flag(body.get("debate"), "debate")
    force_refresh = _bool_flag(body.get("refresh"), "refresh")

    if not subscription_ids or not any(subscription_ids):
        return jsonify({"error": "no subscription configured -- pass subs or configure AZURE_SUBSCRIPTION_ID"}), 400

    try:
        result = analysis_service.build_briefing(
            subscription_ids=subscription_ids, category=category, severity=severity, status=status,
            force_debate=force_debate, force_refresh=force_refresh,
        )
        return jsonify(result)
    except analysis_service.AnalysisError as exc:
        return _handle_analysis_error(exc)
    except EvidenceBundleError as exc:
        return _handle_evidence_bundle_error(exc)
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except NotImplementedError as exc:
        return jsonify({"error": str(exc)}), 501
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to build executive briefing", "detail": str(exc)}), 500


@agent_analysis_bp.route("/tools", methods=["GET"])
def list_tools():
    """GET /api/operations/tools -- introspection only (definitions +
    JSON schemas), never executes anything."""
    return jsonify({"tools": tools_service.list_tool_definitions()})


@agent_analysis_bp.route("/tools/<name>", methods=["POST"])
def invoke_tool(name):
    """POST /api/operations/tools/<name>

    Body (JSON): { "arguments": {...} (required), "roles": [str, ...]
    (optional -- caller's roles for the tool's required_role check;
    omitted means "no authorization context provided", which this app's
    current no-RBAC state treats as implicitly trusted -- see
    app/agents/tools.py's module docstring) }. Always returns 200 with a
    ToolResult body (status ok/denied/invalid_arguments/timeout/error) --
    a tool's own failure is data, not an HTTP error, EXCEPT an unknown
    tool name, which is a 404.
    """
    if tools_service.get_tool(name) is None:
        return jsonify({"error": f"unknown tool {name!r}"}), 404

    body = request.get_json(silent=True) if request.is_json else {}
    body = body if isinstance(body, dict) else {}
    arguments = body.get("arguments")
    if not isinstance(arguments, dict):
        return jsonify({"error": "arguments must be a JSON object"}), 400
    roles = body.get("roles")
    caller_roles = set(roles) if isinstance(roles, list) and all(isinstance(r, str) for r in roles) else None

    result = tools_service.execute_tool(name, arguments, caller_roles=caller_roles)
    return jsonify(result.to_dict())
