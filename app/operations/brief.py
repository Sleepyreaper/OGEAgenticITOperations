"""Executive brief service.

Builds a small, deterministic, truthful summary (`build_brief`) from an
already-built OperationsSnapshot (app.operations.snapshot.get_snapshot).
No LLM call, no invented number: every field is either a direct count/
derivation from Findings/CollectionEnvelopes already in the snapshot, or
an explicit "not_configured"/"unknown" when the underlying source
doesn't exist or didn't succeed -- never a fabricated default (no
composite readiness score, no fake uptime, no invented revenue-at-risk,
no static MTTR). See docs/OPERATIONS_API.md for the exact schema and
worked "SLO not configured" / "all sources failed" examples.
"""

from datetime import datetime
from typing import Optional

from app.operations.models import FindingCategory, Severity, parse_utc_iso, utc_now
from app.operations.snapshot import OperationsSnapshot

__all__ = ["build_brief", "sanitize_evidence"]

_MAX_LIST_ITEMS = 3
_CHANGE_WINDOW_HOURS = 24

_SLO_STATE_RANK = {"breached": 0, "at_risk": 1, "insufficient_data": 2, "healthy": 3}


def sanitize_evidence(evidence: list) -> list:
    """Executive brief responses must never surface a subscription id --
    EvidenceReference.resource_id is an ARM resource id whose first path
    segment is the subscription GUID. Strip it here (everything else --
    source/title/observed_at/reference/raw_excerpt -- is already
    sanitized of credentials/secrets by EvidenceReference itself)."""
    # Public (not a leading-underscore helper) because
    # app/agents/evidence.py's build_evidence_bundle() reuses this exact
    # sanitization for the agent-facing evidence bundle -- see
    # docs/AGENT_INTELLIGENCE.md's evidence-bundle redaction rules.
    sanitized = []
    for item in evidence:
        clipped = dict(item)
        clipped.pop("resource_id", None)
        sanitized.append(clipped)
    return sanitized


def _finding_summary(finding: dict, workflow: dict) -> dict:
    """A bounded, evidence-linked view of one Finding for an executive
    list (business_impact/decisions_required/attention_items) -- never
    the full metadata blob, at most 3 evidence references."""
    evidence = finding.get("evidence") or []
    return {
        "id": finding["id"],
        "title": finding["title"],
        "category": finding["category"],
        "severity": finding["severity"],
        "first_seen": finding["first_seen"],
        "last_seen": finding["last_seen"],
        "business_impact": finding["business_impact"],
        "recommended_action": finding["recommended_action"],
        "workflow_status": workflow["status"],
        "approval_required": finding["approval_required"],
        "evidence_count": len(evidence),
        "evidence": sanitize_evidence(evidence[:3]),
    }


def _is_open(workflow: dict) -> bool:
    """Excludes resolved/dismissed (done) AND snoozed (deliberately
    deferred by a human -- see app.operations.handoff for where snoozed
    items get their own dedicated section instead)."""
    return workflow["status"] not in ("resolved", "dismissed", "snoozed")


def _business_impact_eligible(item: dict) -> bool:
    """Deterministic, stricter-than-ranking rule for the executive
    "active customer-impacting" count: reuses the same `customer_impact`
    factor app.operations.priority.prioritize_findings already computed
    (an incident/reliability Finding, or one flagged executive_attention)
    but additionally requires High/Critical severity or
    executive_attention -- so a single Medium-severity at_risk SLO
    Finding (which IS "customer_impact" for ranking/priority-band
    purposes) doesn't, by itself, make the executive brief claim active
    customer impact."""
    finding = item["finding"]
    factors = item["priority"]["factors"]
    if not factors["customer_impact"]:
        return False
    return finding["severity"] in (Severity.CRITICAL.value, Severity.HIGH.value) or finding["executive_attention"]


# metadata.decision_required values a collector/workflow step can set to
# explicitly flag an approval-required Finding as needing an EXECUTIVE
# decision regardless of severity (e.g. a cost commitment a CIO must
# personally sign off on) -- see _decision_required_eligible.
_METADATA_DECISION_REQUIRED_VALUES = {True, "true", "blocked", "cost_commitment"}


def _decision_required_eligible(item: dict) -> bool:
    """Stricter than plain `approval_required`: human approval alone
    does NOT equal an executive decision. A routine, low/medium-
    severity operational approval (e.g. deleting an orphaned disk after
    a confirmation window) must never crowd this CIO-facing list --
    that used to be this function's defect (every approval_required
    Finding, regardless of severity, counted as an executive decision).

    An approval-required Finding is only surfaced here when it is ALSO
    at least one of:
      - explicitly `executive_attention`,
      - Critical/High severity (a serious approval, not routine
        hygiene), or
      - explicitly flagged via `metadata.decision_required` as
        true/'blocked'/'cost_commitment' -- an escalation/cost-
        commitment marker a collector or workflow step can set even for
        a Medium/Low-severity Finding.

    Everything else that still needs approval stays fully visible in
    the Ops queue (app.operations.queue.build_queue, which applies no
    such filter) and in handoff's `pending_approvals`
    (app.operations.handoff.build_handoff) -- this tightening is
    strictly about what the EXECUTIVE brief leads with, never about
    hiding an approval from the people who actually action it."""
    finding = item["finding"]
    if not finding["approval_required"]:
        return False
    if finding["executive_attention"]:
        return True
    if finding["severity"] in (Severity.CRITICAL.value, Severity.HIGH.value):
        return True
    metadata = finding.get("metadata") or {}
    return metadata.get("decision_required") in _METADATA_DECISION_REQUIRED_VALUES


def _reliability_section(envelopes_by_source: dict) -> dict:
    envelope = envelopes_by_source.get("workload_slo")
    if envelope is None:
        return {"slo_configured": False, "state": "unknown", "error_budget_remaining_pct": None, "workloads": []}
    if envelope.status == "not_configured":
        return {"slo_configured": False, "state": "not_configured", "error_budget_remaining_pct": None, "workloads": []}
    if envelope.status == "error":
        return {"slo_configured": True, "state": "unknown", "error_budget_remaining_pct": None, "workloads": [], "error": envelope.error}

    summaries = envelope.summaries or []
    if not summaries:
        return {"slo_configured": True, "state": "healthy", "error_budget_remaining_pct": None, "workloads": []}

    worst = min(summaries, key=lambda s: _SLO_STATE_RANK.get(s.state, 99))
    return {
        "slo_configured": True,
        "state": worst.state,
        "error_budget_remaining_pct": worst.error_budget_remaining_pct,
        "workloads": [{"workload": s.workload, "state": s.state, "criticality": s.criticality} for s in summaries],
    }


def _capacity_section(envelopes_by_source: dict) -> dict:
    envelope = envelopes_by_source.get("capacity")
    if envelope is None:
        return {"configured": False, "state": "unknown", "minimum_headroom_pct": None, "nearest_constraint": None, "forecast": None}
    if envelope.status == "not_configured":
        return {"configured": False, "state": "not_configured", "minimum_headroom_pct": None, "nearest_constraint": None, "forecast": None}
    if envelope.status == "error":
        return {"configured": True, "state": "unknown", "minimum_headroom_pct": None, "nearest_constraint": None, "forecast": None, "error": envelope.error}

    summaries = envelope.summaries or []
    if not summaries:
        return {"configured": True, "state": "healthy", "minimum_headroom_pct": None, "nearest_constraint": None, "forecast": None}

    known_headroom = [s for s in summaries if s.headroom_pct is not None]
    nearest = min(known_headroom, key=lambda s: s.headroom_pct) if known_headroom else None
    critical_count = sum(1 for s in summaries if s.threshold_state == "critical")
    warning_count = sum(1 for s in summaries if s.threshold_state == "warning")
    state = "critical" if critical_count else ("warning" if warning_count else "healthy")

    forecasts = [s for s in summaries if s.forecast_state == "available" and s.forecast_exhaustion_at]
    nearest_forecast = min(forecasts, key=lambda s: s.forecast_exhaustion_at) if forecasts else None

    return {
        "configured": True,
        "state": state,
        "minimum_headroom_pct": nearest.headroom_pct if nearest else None,
        "nearest_constraint": f"{nearest.resource_scope}/{nearest.metric}" if nearest else None,
        "forecast": (
            {"resource_scope": nearest_forecast.resource_scope, "metric": nearest_forecast.metric, "exhaustion_at": nearest_forecast.forecast_exhaustion_at}
            if nearest_forecast else None
        ),
    }


def _changes_since_yesterday(envelopes_by_source: dict, *, now: datetime) -> list:
    envelope = envelopes_by_source.get("activity_log_change_health")
    if envelope is None or envelope.status != "ok":
        return []
    recent = [
        f for f in envelope.findings
        if f.category == FindingCategory.CHANGE.value
        and (now - parse_utc_iso(f.last_seen)).total_seconds() <= _CHANGE_WINDOW_HOURS * 3600
    ]
    recent.sort(key=lambda f: f.last_seen, reverse=True)
    return [{"id": f.id, "title": f.title, "occurred_at": f.last_seen, "summary": f.summary} for f in recent[:_MAX_LIST_ITEMS]]


def _headline(overall_state: str, business_impact: dict, attention_items: list, *, coverage_error_sources: Optional[list] = None) -> str:
    """Assembled ONLY from the already-computed evidence fields --
    never a free-text/model-generated sentence."""
    if overall_state == "unknown":
        return "Insufficient source coverage right now to determine operational health."
    if overall_state == "impact":
        top = business_impact["details"][0]
        return f"{business_impact['active_customer_impacting_count']} active customer-impacting issue(s); top: {top['title']}."
    if overall_state == "attention":
        if attention_items:
            return f"{len(attention_items)} item(s) need attention; top: {attention_items[0]['title']}."
        if coverage_error_sources:
            names = ", ".join(sorted(coverage_error_sources)[:3])
            remainder = len(coverage_error_sources) - 3
            if remainder > 0:
                names = f"{names}, and {remainder} more"
            return (
                f"Evidence coverage is incomplete: {len(coverage_error_sources)} source(s) failed to collect "
                f"({names}) -- operational health cannot be fully confirmed."
            )
        return "Capacity, reliability, or approval signals require attention -- see capacity/reliability/decisions_required for detail."
    return "No active customer-impacting issues; all monitored sources report healthy."


def build_brief(snapshot: OperationsSnapshot, *, now: Optional[datetime] = None) -> dict:
    now = now or utc_now()
    envelopes_by_source = {e.source: e for e in snapshot.envelopes}

    open_items = [item for item in snapshot.findings if _is_open(item["workflow"])]

    business_impact_items = [item for item in open_items if _business_impact_eligible(item)]
    business_impact = {
        "active_customer_impacting_count": len(business_impact_items),
        "details": [_finding_summary(i["finding"], i["workflow"]) for i in business_impact_items[:_MAX_LIST_ITEMS]],
    }

    reliability = _reliability_section(envelopes_by_source)
    capacity = _capacity_section(envelopes_by_source)
    changes_since_yesterday = _changes_since_yesterday(envelopes_by_source, now=now)

    decisions_required_items = [item for item in open_items if _decision_required_eligible(item)]
    decisions_required = [_finding_summary(i["finding"], i["workflow"]) for i in decisions_required_items[:_MAX_LIST_ITEMS]]

    attention_items_all = [item for item in open_items if item["finding"]["executive_attention"]]
    attention_items = [_finding_summary(i["finding"], i["workflow"]) for i in attention_items_all[:_MAX_LIST_ITEMS]]

    # source_coverage.error_count is the same "did every applicable
    # source actually collect" signal snapshot.status/_snapshot_status
    # already computes from these same envelopes -- reused here (never
    # re-derived) so a partial collection (one or more sources errored,
    # even ones with no per-source section like capacity/reliability
    # above, e.g. defender_alerts/cost_management_budget/azure_backup)
    # can never be summarized as "healthy". Only status == 'error'
    # (EVERY applicable source failed) escalates further, to 'unknown'.
    coverage = snapshot.coverage or {}
    coverage_error_sources = list((coverage.get("sources_by_status") or {}).get("error") or [])
    coverage_error_count = coverage.get("error_count", len(coverage_error_sources))

    if snapshot.status == "error":
        overall_state = "unknown"
    elif business_impact["active_customer_impacting_count"] > 0:
        overall_state = "impact"
    elif (
        attention_items_all
        or decisions_required_items
        or capacity["state"] in ("critical", "unknown")
        or reliability["state"] in ("breached", "unknown")
        or coverage_error_count > 0
    ):
        overall_state = "attention"
    else:
        overall_state = "healthy"

    return {
        "overall_state": overall_state,
        "headline": _headline(overall_state, business_impact, attention_items, coverage_error_sources=coverage_error_sources),
        "updated_at": snapshot.generated_at,
        "data_freshness": {
            "snapshot_generated_at": snapshot.generated_at,
            "age_seconds": max(0.0, (now - parse_utc_iso(snapshot.generated_at)).total_seconds()),
        },
        "business_impact": business_impact,
        "reliability": reliability,
        "capacity": capacity,
        "changes_since_yesterday": changes_since_yesterday,
        "decisions_required": decisions_required,
        "attention_items": attention_items,
        "source_coverage": snapshot.coverage,
        "snapshot_id": snapshot.id,
    }
