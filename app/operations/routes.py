"""Flask routes for the operations snapshot/brief/queue/workflow-state
product surface, mounted under /api/operations (see app/main.py's
create_app(), which registers `operations_bp`).

Every route here reads from (or mutates workflow state on top of) the
deterministic evidence layer (app/operations/snapshot.py, brief.py,
queue.py, state.py, handoff.py) -- no LLM call. Responses never include a
subscription id, endpoint URL, token, or credential in the executive
brief (see app.operations.brief's evidence sanitization); other routes
here follow the rest of this app's existing convention of surfacing
subscription/resource ids for engineers who already have Azure access
(see e.g. /api/scan/overview in app/main.py).
"""

import traceback

from flask import Blueprint, jsonify, request

from app import azure_data
from app.config import settings
from app.operations import brief as brief_service
from app.operations import demo_fixture as demo_fixture_service
from app.operations import handoff as handoff_service
from app.operations import queue as queue_service
from app.operations.config import OperationsConfig, OperationsConfigError
from app.operations.errors import OperationsCollectionError
from app.operations.finding_lookup import bounded_evidence_view, find_finding_item
from app.operations.snapshot import get_default_state_store, get_snapshot
from app.operations.state import OperationsStateError, WORKFLOW_ACTIONS

operations_bp = Blueprint("operations", __name__, url_prefix="/api/operations")


def _parse_sub_ids() -> list:
    """Same ?subs= semantics as app.main._parse_sub_ids ("all" / a
    comma-separated list / unset -> the configured default subscription)
    -- kept as its own copy (not imported from app.main) so
    app/operations/ never depends on app.main, avoiding a circular
    import."""
    subs_param = request.args.get("subs", "").strip()
    if subs_param == "all":
        all_subs = azure_data.list_subscriptions()
        return [s["id"] for s in all_subs if s.get("state") == "Enabled"]
    if subs_param:
        return [s.strip() for s in subs_param.split(",") if s.strip()]
    return [settings.subscription_id]


def _parse_refresh() -> bool:
    return request.args.get("refresh", "").strip().lower() in ("1", "true", "yes", "on")


def _capacity_full_collect_kwargs(config: OperationsConfig) -> dict:
    """`run_full_collection`'s `locations`/`openai_locations` kwargs,
    derived from `OperationsConfig.capacity_locations` (CAPACITY_LOCATIONS
    -- an explicit, bounded, operator-configured region list, never an
    unbounded query-string parameter). Empty when CAPACITY_LOCATIONS is
    unset, so capacity collection reports its documented `not_configured`
    state exactly as it does when calling run_full_collection directly."""
    if not config.capacity_locations:
        return {}
    locations = list(config.capacity_locations)
    return {"locations": locations, "openai_locations": locations}


def _build_snapshot():
    """Shared by every route below: resolve ?subs=/?refresh=, then get
    (or build) the cached OperationsSnapshot. Raises ValueError (bad
    subs) or OperationsCollectionError/OperationsConfigError (propagated
    to the route's own error handling) -- never silently returns an
    empty/fake snapshot. Builds one explicit OperationsConfig.from_env()
    per request and forwards it (plus CAPACITY_LOCATIONS, see
    _capacity_full_collect_kwargs) into get_snapshot, so every route that
    calls this (snapshot/brief/queue/handoff/evidence) actually runs
    capacity collection whenever an operator has set CAPACITY_LOCATIONS
    -- no ?locations= query-string plumbing. The snapshot cache key stays
    correct because this configuration is process-static: the same
    CAPACITY_LOCATIONS value is forwarded for every request within a
    process's lifetime, so a cached snapshot never mixes region
    selections."""
    sub_ids = _parse_sub_ids()
    if not sub_ids or not any(sub_ids):
        raise ValueError("no subscription configured -- pass ?subs=<id> or configure AZURE_SUBSCRIPTION_ID")
    config = OperationsConfig.from_env()
    return get_snapshot(
        sub_ids, config=config, force_refresh=_parse_refresh(),
        full_collect_kwargs=_capacity_full_collect_kwargs(config),
    )


@operations_bp.route("/snapshot", methods=["GET"])
def get_operations_snapshot():
    """GET /api/operations/snapshot?subs=...&refresh=true"""
    try:
        snapshot = _build_snapshot()
        return jsonify(snapshot.to_dict())
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary; never leak a raw traceback to the client
        traceback.print_exc()
        return jsonify({"error": "failed to build operations snapshot", "detail": str(exc)}), 500


@operations_bp.route("/brief", methods=["GET"])
def get_operations_brief():
    """GET /api/operations/brief?subs=...&refresh=true"""
    try:
        snapshot = _build_snapshot()
        return jsonify(brief_service.build_brief(snapshot))
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to build executive brief", "detail": str(exc)}), 500


@operations_bp.route("/queue", methods=["GET"])
def get_operations_queue():
    """GET /api/operations/queue?subs=...&refresh=true&status=&category=&severity=&owner=&page=1&page_size=25"""
    try:
        snapshot = _build_snapshot()
        page = int(request.args.get("page", "1"))
        page_size = int(request.args.get("page_size", str(queue_service.DEFAULT_PAGE_SIZE)))
        result = queue_service.build_queue(
            snapshot.findings,
            status=request.args.get("status", "").strip() or None,
            category=request.args.get("category", "").strip() or None,
            severity=request.args.get("severity", "").strip() or None,
            owner=request.args.get("owner", "").strip() or None,
            page=page,
            page_size=page_size,
        )
        return jsonify(result)
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to build operations queue", "detail": str(exc)}), 500


_REQUIRED_ACTION_FIELDS = {
    "snooze": ("snooze_until",),
    "assign": ("owner",),
}


@operations_bp.route("/findings/<finding_id>", methods=["PATCH"])
def patch_operations_finding(finding_id):
    """PATCH /api/operations/findings/<finding_id>

    Body (JSON, strictly validated): {
      "action": "acknowledge"|"start"|"resolve"|"dismiss"|"snooze"|"assign",
      "actor": str (required),
      "owner": str (required for "assign"),
      "snooze_until": ISO-8601 str (required for "snooze"),
      "reason": str (optional -- disposition reason for resolve/dismiss),
      "first_seen": ISO-8601 str (optional -- only used the first time
        this finding id is ever touched; otherwise the persisted value
        is kept)
    }
    """
    if not request.is_json:
        return jsonify({"error": "request body must be JSON"}), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    action = body.get("action")
    if not isinstance(action, str) or action not in WORKFLOW_ACTIONS:
        return jsonify({"error": f"action is required and must be one of {sorted(WORKFLOW_ACTIONS)}"}), 400

    actor = body.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        return jsonify({"error": "actor is required"}), 400

    for field_name in _REQUIRED_ACTION_FIELDS.get(action, ()):
        value = body.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return jsonify({"error": f"{field_name} is required for action {action!r}"}), 400

    reason = body.get("reason", "")
    if reason is not None and not isinstance(reason, str):
        return jsonify({"error": "reason must be a string"}), 400
    owner = body.get("owner")
    if owner is not None and not isinstance(owner, str):
        return jsonify({"error": "owner must be a string"}), 400
    snooze_until = body.get("snooze_until")
    if snooze_until is not None and not isinstance(snooze_until, str):
        return jsonify({"error": "snooze_until must be an ISO-8601 string"}), 400
    first_seen = body.get("first_seen")
    if first_seen is not None and not isinstance(first_seen, str):
        return jsonify({"error": "first_seen must be an ISO-8601 string"}), 400

    try:
        store = get_default_state_store()
        record = store.apply_action(
            finding_id, action, actor=actor.strip(), reason=reason or "", owner=owner,
            snooze_until=snooze_until, first_seen=first_seen,
        )
        return jsonify(record.to_dict())
    except OperationsStateError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to apply workflow action", "detail": str(exc)}), 500


@operations_bp.route("/handoff", methods=["GET"])
def get_operations_handoff():
    """GET /api/operations/handoff?subs=...&refresh=true -- builds (but
    does not persist) the current handoff view."""
    try:
        snapshot = _build_snapshot()
        store = get_default_state_store()
        handoff = handoff_service.build_handoff(snapshot, state_store=store)
        return jsonify(handoff)
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to build handoff", "detail": str(exc)}), 500


@operations_bp.route("/handoff", methods=["POST"])
def post_operations_handoff():
    """POST /api/operations/handoff

    Body (JSON, optional): { "subs": [<subscription id>, ...],
    "created_by": str (required) }. Builds the current handoff (same
    computation as GET) and persists a bounded marker (timestamp, actor,
    integrity hash, open finding ids only -- see
    app.operations.state.OperationsStateStore.record_handoff)."""
    body = request.get_json(silent=True) if request.is_json else {}
    body = body if isinstance(body, dict) else {}

    created_by = body.get("created_by")
    if not isinstance(created_by, str) or not created_by.strip():
        return jsonify({"error": "created_by is required"}), 400

    subs = body.get("subs")
    if subs is not None and (not isinstance(subs, list) or not all(isinstance(s, str) for s in subs)):
        return jsonify({"error": "subs must be a list of subscription id strings"}), 400

    try:
        sub_ids = subs if subs else _parse_sub_ids()
        if not sub_ids or not any(sub_ids):
            return jsonify({"error": "no subscription configured -- pass subs or configure AZURE_SUBSCRIPTION_ID"}), 400
        config = OperationsConfig.from_env()
        snapshot = get_snapshot(
            sub_ids, config=config, force_refresh=_parse_refresh(),
            full_collect_kwargs=_capacity_full_collect_kwargs(config),
        )
        store = get_default_state_store()
        handoff = handoff_service.build_handoff(snapshot, state_store=store)
        persisted = handoff_service.persist_handoff(handoff, state_store=store, created_by=created_by.strip())
        return jsonify({"handoff": handoff, "persisted": persisted}), 201
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to record handoff", "detail": str(exc)}), 500


@operations_bp.route("/evidence/<finding_id>", methods=["GET"])
def get_operations_evidence(finding_id):
    """GET /api/operations/evidence/<finding_id>?subs=...

    Returns bounded evidence metadata only for one finding (id, title,
    category, severity, source, confidence, and its evidence
    references) -- never the full queue/snapshot item. 404 if
    `finding_id` isn't present in the current snapshot for the resolved
    subscription selection.
    """
    try:
        snapshot = _build_snapshot()
    except (OperationsCollectionError, OperationsConfigError) as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary
        traceback.print_exc()
        return jsonify({"error": "failed to build operations snapshot", "detail": str(exc)}), 500

    item = find_finding_item(snapshot, finding_id)
    if item is not None:
        return jsonify(bounded_evidence_view(item["finding"]))
    return jsonify({"error": f"finding {finding_id!r} not found in the current snapshot"}), 404


@operations_bp.route("/demo", methods=["GET"])
def get_operations_demo():
    """GET /api/operations/demo -- the ONE centralized Demo-mode fixture
    for the product UI's Executive Brief / Operations Center surfaces
    (see app/operations/demo_fixture.py). Returns
    `{"meta", "snapshot", "brief", "queue", "handoff", "analysis_example",
    "briefing_example"}` -- the exact same brief/queue/handoff schemas
    the live routes above return, so templates/index.html renders Demo
    and Live data with the same functions; never scattered hardcoded
    fake values in the frontend. Ignores ?subs=/?refresh= (there is no
    subscription to select in Demo mode) and never calls Azure or an
    LLM, consistent with this blueprint's "no LLM call" invariant."""
    try:
        return jsonify(demo_fixture_service.build_demo_payload())
    except Exception as exc:  # noqa: BLE001 -- last-resort route boundary, same convention as the routes above
        traceback.print_exc()
        return jsonify({"error": "failed to build demo fixture", "detail": str(exc)}), 500
