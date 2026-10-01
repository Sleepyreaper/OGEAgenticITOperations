"""ZeroOps REST API for the UI: scenarios, simulated escalations, ledger, human gate."""
import traceback

from flask import Blueprint, jsonify, request

from app.zeroops import cost as cost_model
from app.zeroops import scenarios as scenario_mod
from app.zeroops import service
from app.zeroops.ledger import get_ledger
from app.zeroops.mcp_server import api_key_configured

zeroops_bp = Blueprint("zeroops", __name__, url_prefix="/api/zeroops")


def _body() -> dict:
    body = request.get_json(silent=True) if request.is_json else None
    return body if isinstance(body, dict) else {}


@zeroops_bp.route("/overview", methods=["GET"])
def overview():
    config = scenario_mod.DemoConfig.from_env()
    return jsonify({
        "mcp": {"endpoint": "/mcp", "enabled": api_key_configured(), "server_name": "ogeops"},
        "demo": config.public(),
        "scenarios": scenario_mod.list_scenarios(config),
        "cost_model": cost_model.monthly_baseline(),
        "ledger": get_ledger().summary(),
    })


@zeroops_bp.route("/scenarios", methods=["GET"])
def scenarios():
    return jsonify({"scenarios": scenario_mod.list_scenarios()})


@zeroops_bp.route("/scenarios/<scenario_id>/<action>", methods=["POST"])
def scenario_action(scenario_id, action):
    if scenario_id not in scenario_mod.SCENARIOS_BY_ID:
        return jsonify({"error": f"unknown scenario {scenario_id!r}"}), 404
    if action in ("inject", "cleanup"):
        try:
            return jsonify(scenario_mod.run(scenario_id, action))
        except scenario_mod.ScenarioError as exc:
            return jsonify({"error": str(exc)}), 409
    if action == "escalate":
        body = _body()
        try:
            record = service.escalate(
                scenario_id=scenario_id, source="simulated",
                incident_ref=str(body.get("incident_ref") or f"demo-{scenario_id}"),
                sre_summary=str(body.get("sre_summary") or "Simulated hand-off from the ZeroOps demo console."),
                debate=bool(body.get("debate", False)), wait_seconds=int(body.get("wait_seconds", 0) or 0),
            )
            return jsonify(record), 202
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify({"error": "action must be inject, cleanup or escalate"}), 400


@zeroops_bp.route("/escalations", methods=["GET"])
def list_escalations():
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    ledger = get_ledger()
    return jsonify({"escalations": ledger.list(limit=limit), "summary": ledger.summary()})


@zeroops_bp.route("/escalations", methods=["POST"])
def create_escalation():
    body = _body()
    try:
        record = service.escalate(
            question=str(body.get("question") or ""), source="api", incident_ref=str(body.get("incident_ref") or ""),
            sre_summary=str(body.get("sre_summary") or ""), scenario_id=str(body.get("scenario_id") or ""),
            severity=str(body.get("severity") or ""), category=str(body.get("category") or ""),
            debate=bool(body.get("debate", False)), wait_seconds=int(body.get("wait_seconds", 0) or 0),
        )
        return jsonify(record), 202
    except KeyError as exc:
        return jsonify({"error": f"unknown scenario {exc}"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@zeroops_bp.route("/escalations/<esc_id>", methods=["GET"])
def get_escalation(esc_id):
    record = get_ledger().get(esc_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(record)


@zeroops_bp.route("/escalations/<esc_id>/propose", methods=["POST"])
def propose(esc_id):
    try:
        return jsonify(service.propose_fix(esc_id, requested_by="ops-console"))
    except KeyError:
        return jsonify({"error": "not found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409


@zeroops_bp.route("/escalations/<esc_id>/decision", methods=["POST"])
def decision(esc_id):
    body = _body()
    try:
        return jsonify(service.decide(esc_id, decision=str(body.get("decision") or ""),
                                      by=str(body.get("by") or "ops-user"), reason=str(body.get("reason") or "")))
    except KeyError:
        return jsonify({"error": "not found"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 -- route boundary
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500
