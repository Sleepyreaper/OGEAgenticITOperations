"""Sanitized, read-only Activity Proof API."""

import sqlite3
import traceback
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app.activity.store import ActivityStoreError, ActivityValidationError, get_activity_store

activity_bp = Blueprint("activity", __name__, url_prefix="/api/activity")


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_limit(default: int, maximum: int) -> int:
    raw = request.args.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ActivityValidationError("limit must be an integer") from exc
    if value < 1 or value > maximum:
        raise ActivityValidationError(f"limit must be from 1 to {maximum}")
    return value


@activity_bp.route("", methods=["GET"])
def list_activity():
    try:
        result = get_activity_store().list_public_investigations(
            limit=_parse_limit(20, 50),
            cursor=request.args.get("cursor"),
        )
        return jsonify({
            "schema_version": 1,
            "generated_at": _generated_at(),
            "items": result["items"],
            "next_cursor": result["next_cursor"],
        })
    except ActivityValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except (ActivityStoreError, OSError, sqlite3.Error):
        traceback.print_exc()
        return jsonify({"error": "activity store unavailable"}), 503
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "activity request failed"}), 500


@activity_bp.route("/<investigation_id>", methods=["GET"])
def get_activity(investigation_id):
    try:
        event_after_raw = request.args.get("event_after", "0")
        try:
            event_after = int(event_after_raw)
        except (TypeError, ValueError) as exc:
            raise ActivityValidationError("event_after must be an integer") from exc
        result = get_activity_store().get_public_investigation(
            investigation_id,
            limit=_parse_limit(50, 100),
            event_after=event_after,
        )
        if result is None:
            return jsonify({"error": "activity investigation not found"}), 404
        return jsonify({
            "schema_version": 1,
            "investigation": result["investigation"],
            "runs": result["runs"],
            "events": result["events"],
            "artifacts": result["artifacts"],
            "next_event_cursor": result["next_event_cursor"],
        })
    except ActivityValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except (ActivityStoreError, OSError, sqlite3.Error):
        traceback.print_exc()
        return jsonify({"error": "activity store unavailable"}), 503
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "activity request failed"}), 500
