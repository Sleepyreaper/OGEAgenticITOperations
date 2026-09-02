"""Structured shift-handoff snapshot.

Builds (`build_handoff`) a bounded, human-scannable handoff view from an
OperationsSnapshot: open items, what's new/changed since the prior
handoff, snoozed items with expiry/reason, a capacity watch, pending
approvals/decisions, recent Azure changes, and any source coverage gaps.

Persistence (`persist_handoff` -> app.operations.state.
OperationsStateStore.record_handoff) stores only a timestamp, actor, an
integrity hash of the computed payload, and the bounded list of open
Finding IDs -- never raw evidence text or secrets. "new/changed since the
prior handoff" is always recomputed LIVE from the current snapshot's
Finding.first_seen / workflow updated_at against the prior handoff's
persisted timestamp -- never from a stored evidence dump, so nothing
sensitive needs to be retained between handoffs for this diff to work.
"""

import hashlib
import json
from datetime import datetime
from typing import Optional

from app.operations.models import FindingCategory, parse_utc_iso, utc_now
from app.operations.snapshot import OperationsSnapshot
from app.operations.state import OperationsStateStore

__all__ = ["build_handoff", "persist_handoff", "capacity_watch", "recent_changes_since"]

_MAX_ITEMS = 25
_CHANGE_WINDOW_HOURS = 24

_OPEN_WORKFLOW_STATUSES_EXCLUDED = ("resolved", "dismissed", "snoozed")


def _bounded_item(item: dict) -> dict:
    finding = item["finding"]
    workflow = item["workflow"]
    return {
        "id": finding["id"],
        "title": finding["title"],
        "category": finding["category"],
        "severity": finding["severity"],
        "first_seen": finding["first_seen"],
        "last_seen": finding["last_seen"],
        "workflow_status": workflow["status"],
        "assigned_owner": workflow["assigned_owner"],
        "approval_required": finding["approval_required"],
        "business_impact": finding["business_impact"],
    }


def _snoozed_item(item: dict) -> dict:
    finding = item["finding"]
    workflow = item["workflow"]
    return {
        "id": finding["id"],
        "title": finding["title"],
        "category": finding["category"],
        "severity": finding["severity"],
        "snooze_until": workflow["snooze_until"],
        "disposition_reason": workflow["disposition_reason"],
        "assigned_owner": workflow["assigned_owner"],
    }


def capacity_watch(envelopes_by_source: dict) -> list:
    """Capacity/quota line items currently in warning or critical
    threshold state, nearest-headroom-first. Public (not a leading-
    underscore helper) because app/agents/tools.py's get_capacity_watch
    tool reuses this exact logic -- see docs/AGENT_INTELLIGENCE.md."""
    envelope = envelopes_by_source.get("capacity")
    if envelope is None or envelope.status != "ok":
        return []
    watch = [s for s in (envelope.summaries or []) if s.threshold_state in ("warning", "critical")]
    watch.sort(key=lambda s: (s.headroom_pct if s.headroom_pct is not None else 0.0))
    return [
        {
            "resource_scope": s.resource_scope, "metric": s.metric, "threshold_state": s.threshold_state,
            "headroom_pct": s.headroom_pct, "forecast_exhaustion_at": s.forecast_exhaustion_at,
        }
        for s in watch[:_MAX_ITEMS]
    ]


def recent_changes_since(envelopes_by_source: dict, *, now: datetime) -> list:
    """Activity Log changes correlated within the last 24h change/health
    window. Public (not a leading-underscore helper) because
    app/agents/tools.py's get_recent_changes tool reuses this exact
    logic -- see docs/AGENT_INTELLIGENCE.md."""
    envelope = envelopes_by_source.get("activity_log_change_health")
    if envelope is None or envelope.status != "ok":
        return []
    recent = [
        f for f in envelope.findings
        if f.category == FindingCategory.CHANGE.value
        and (now - parse_utc_iso(f.last_seen)).total_seconds() <= _CHANGE_WINDOW_HOURS * 3600
    ]
    recent.sort(key=lambda f: f.last_seen, reverse=True)
    return [{"id": f.id, "title": f.title, "occurred_at": f.last_seen, "summary": f.summary} for f in recent[:_MAX_ITEMS]]


def _source_gaps(coverage: dict) -> list:
    gaps = []
    sources_by_status = coverage.get("sources_by_status", {})
    for status in ("error", "not_configured", "not_supported"):
        for source in sources_by_status.get(status, []):
            gaps.append({"source": source, "status": status})
    return gaps


def _content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def build_handoff(
    snapshot: OperationsSnapshot,
    *,
    state_store: Optional[OperationsStateStore] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Build (but do not persist) a shift-handoff view. Pass
    `state_store` to compute "new/changed since the prior handoff" from
    its most recently persisted handoff timestamp; omit it (or when no
    prior handoff exists yet) to treat every currently-open item as
    new."""
    now = now or utc_now()
    envelopes_by_source = {e.source: e for e in snapshot.envelopes}

    open_items = [item for item in snapshot.findings if item["workflow"]["status"] not in _OPEN_WORKFLOW_STATUSES_EXCLUDED]
    snoozed_items = [item for item in snapshot.findings if item["workflow"]["status"] == "snoozed"]
    pending_approvals = [item for item in open_items if item["finding"]["approval_required"]]

    prior_handoff = state_store.get_latest_handoff() if state_store is not None else None
    prior_timestamp = parse_utc_iso(prior_handoff["created_at"]) if prior_handoff else None

    if prior_timestamp is None:
        new_items = list(open_items)
        changed_items = []
    else:
        new_items = [item for item in open_items if parse_utc_iso(item["finding"]["first_seen"]) > prior_timestamp]
        new_ids = {item["finding"]["id"] for item in new_items}
        changed_items = [
            item for item in open_items
            if item["finding"]["id"] not in new_ids
            and item["workflow"]["updated_at"]
            and parse_utc_iso(item["workflow"]["updated_at"]) > prior_timestamp
        ]

    payload = {
        "generated_at": snapshot.generated_at,
        "snapshot_id": snapshot.id,
        "prior_handoff_at": prior_handoff["created_at"] if prior_handoff else None,
        "open_item_count": len(open_items),
        "open_items": [_bounded_item(i) for i in open_items[:_MAX_ITEMS]],
        "new_since_prior": [_bounded_item(i) for i in new_items[:_MAX_ITEMS]],
        "changed_since_prior": [_bounded_item(i) for i in changed_items[:_MAX_ITEMS]],
        "snoozed_items": [_snoozed_item(i) for i in snoozed_items[:_MAX_ITEMS]],
        "capacity_watch": capacity_watch(envelopes_by_source),
        "pending_approvals": [_bounded_item(i) for i in pending_approvals[:_MAX_ITEMS]],
        "recent_changes": recent_changes_since(envelopes_by_source, now=now),
        "source_gaps": _source_gaps(snapshot.coverage),
    }
    payload["content_hash"] = _content_hash(payload)
    return payload


def persist_handoff(
    handoff: dict,
    *,
    state_store: OperationsStateStore,
    created_by: str,
    now: Optional[datetime] = None,
) -> dict:
    """Record a bounded, secret-free marker for an already-built
    `handoff` payload (see OperationsStateStore.record_handoff):
    timestamp, actor, the payload's integrity hash, and open Finding IDs
    only -- never the evidence detail itself."""
    open_finding_ids = [item["id"] for item in handoff.get("open_items", [])]
    summary = {
        "open_item_count": handoff.get("open_item_count", 0),
        "new_since_prior_count": len(handoff.get("new_since_prior", [])),
        "changed_since_prior_count": len(handoff.get("changed_since_prior", [])),
        "snoozed_count": len(handoff.get("snoozed_items", [])),
        "pending_approvals_count": len(handoff.get("pending_approvals", [])),
        "source_gap_count": len(handoff.get("source_gaps", [])),
    }
    return state_store.record_handoff(
        created_by=created_by,
        content_hash=handoff["content_hash"],
        open_finding_ids=open_finding_ids,
        summary=summary,
        now=now,
    )
