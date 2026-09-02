"""Configurable workload SLOs, evaluated against Log Analytics/App
Insights data via the existing Log Analytics query client
(app.azure_data.query_logs).

SLO definitions are loaded from a checked-in example/config JSON file
(SLO_DEFINITIONS_PATH) or an inline environment JSON blob
(SLO_DEFINITIONS_JSON) -- see config/slo_definitions.example.json for the
schema. With neither set, `load_slo_definitions` returns an empty list;
callers MUST treat that as an explicit 'not_configured' collection
status (see app/operations/service.py), never as "zero workloads
currently breaching."
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional

from app.azure_data import query_logs as default_query_logs
from app.operations.errors import OperationsCollectionError
from app.operations.models import (
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    SLOSummary,
    Severity,
    utc_now_iso,
)

__all__ = [
    "SLODefinition",
    "load_slo_definitions",
    "evaluate_slo",
    "collect_workload_slos",
    "slo_summaries_to_findings",
]

SOURCE = EvidenceSource.LOG_ANALYTICS_SLO.value

_VALID_CRITICALITY = {"customer_facing", "internal", "best_effort"}


@dataclass
class SLODefinition:
    """One entry from the SLO definitions JSON -- see
    config/slo_definitions.example.json. `query` must return exactly one
    row (the first row is used) with `good_column`/`total_column`
    numeric columns; `window_hours` both bounds the Log Analytics
    timespan and defines the SLO's rolling evaluation window."""
    workload: str
    query: str
    objective_pct: float
    window_hours: int
    criticality: str = "customer_facing"
    good_column: str = "good"
    total_column: str = "total"
    at_risk_burn_rate: float = 2.0
    workspace_id: str = ""

    def __post_init__(self):
        if not self.workload.strip():
            raise ValueError("SLO definition is missing 'workload'")
        if not self.query.strip():
            raise ValueError(f"SLO definition {self.workload!r} is missing 'query'")
        if not (0 < self.objective_pct <= 100):
            raise ValueError(f"SLO definition {self.workload!r}: objective_pct must be in (0, 100]")
        if self.window_hours <= 0:
            raise ValueError(f"SLO definition {self.workload!r}: window_hours must be positive")
        if self.criticality not in _VALID_CRITICALITY:
            raise ValueError(f"SLO definition {self.workload!r}: criticality must be one of {sorted(_VALID_CRITICALITY)}")
        if self.at_risk_burn_rate <= 0:
            raise ValueError(f"SLO definition {self.workload!r}: at_risk_burn_rate must be positive")


def _parse_definitions(raw: dict, *, origin: str) -> list:
    if not isinstance(raw, dict) or "slos" not in raw:
        raise ValueError(f"{origin}: expected a top-level 'slos' array")
    entries = raw["slos"]
    definitions = []
    for i, entry in enumerate(entries):
        try:
            definitions.append(SLODefinition(
                workload=str(entry.get("workload", "")),
                query=str(entry.get("query", "")),
                objective_pct=float(entry.get("objective_pct", 0)),
                window_hours=int(entry.get("window_hours", 0)),
                criticality=str(entry.get("criticality", "customer_facing")),
                good_column=str(entry.get("good_column", "good")),
                total_column=str(entry.get("total_column", "total")),
                at_risk_burn_rate=float(entry.get("at_risk_burn_rate", 2.0)),
                workspace_id=str(entry.get("workspace_id", "")),
            ))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{origin}: slos[{i}] is invalid: {exc}") from exc
    return definitions


def load_slo_definitions(*, config_path: str = "", config_json: str = "") -> list:
    """Load SLO definitions from an inline JSON blob (`config_json`, tried
    first) or a JSON file path (`config_path`). Returns [] when neither
    is set -- an explicit, deliberate "not configured" signal, not an
    error."""
    if config_json.strip():
        try:
            raw = json.loads(config_json)
        except ValueError as exc:
            raise ValueError(f"SLO_DEFINITIONS_JSON is not valid JSON: {exc}") from exc
        return _parse_definitions(raw, origin="SLO_DEFINITIONS_JSON")

    if config_path.strip():
        path = Path(config_path)
        if not path.is_file():
            raise ValueError(f"SLO_DEFINITIONS_PATH {config_path!r} does not exist")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{config_path}: not valid JSON: {exc}") from exc
        return _parse_definitions(raw, origin=config_path)

    return []


def _slo_state(observed_pct: Optional[float], objective_pct: float, burn_rate: Optional[float], at_risk_burn_rate: float) -> str:
    if observed_pct is None:
        return "insufficient_data"
    if observed_pct < objective_pct:
        return "breached"
    if burn_rate is not None and burn_rate >= at_risk_burn_rate:
        return "at_risk"
    return "healthy"


def evaluate_slo(
    definition: SLODefinition,
    *,
    default_workspace_id: Optional[str] = None,
    query_logs_fn: Callable[..., list] = default_query_logs,
) -> SLOSummary:
    """Run one SLO definition's query and compute its SLOSummary.

    Raises OperationsCollectionError (never fabricates a result) if the
    query fails, returns no rows, is missing the configured good/total
    columns, or returns an internally inconsistent good/total pair.
    """
    workspace_id = definition.workspace_id or default_workspace_id
    try:
        rows = query_logs_fn(definition.query, workspace_id, timedelta(hours=definition.window_hours))
    except Exception as exc:
        raise OperationsCollectionError(SOURCE, f"SLO query for {definition.workload!r} failed", detail=str(exc)) from exc

    if not rows:
        raise OperationsCollectionError(SOURCE, f"SLO query for {definition.workload!r} returned no rows")
    row = rows[0]
    if definition.good_column not in row or definition.total_column not in row:
        raise OperationsCollectionError(
            SOURCE,
            f"SLO query for {definition.workload!r} is missing expected columns "
            f"{definition.good_column!r}/{definition.total_column!r}",
            detail=f"got columns: {sorted(row.keys())}",
        )

    good = float(row.get(definition.good_column) or 0)
    total = float(row.get(definition.total_column) or 0)
    if total < 0 or good < 0 or good > total:
        raise OperationsCollectionError(
            SOURCE, f"SLO query for {definition.workload!r} returned invalid good/total ({good}/{total})"
        )

    observed_pct = round(good / total * 100, 4) if total > 0 else None
    allowed_failure_pct = 100 - definition.objective_pct
    burn_rate = None
    error_budget_remaining_pct = None
    if observed_pct is not None and allowed_failure_pct > 0:
        actual_failure_pct = 100 - observed_pct
        burn_rate = round(actual_failure_pct / allowed_failure_pct, 4)
        error_budget_remaining_pct = round(max(0.0, min(100.0, 100 * (1 - burn_rate))), 2)

    state = _slo_state(observed_pct, definition.objective_pct, burn_rate, definition.at_risk_burn_rate)
    evaluated_at = utc_now_iso()

    return SLOSummary(
        workload=definition.workload,
        state=state,
        objective_pct=definition.objective_pct,
        observed_pct=observed_pct,
        window_hours=definition.window_hours,
        criticality=definition.criticality,
        evaluated_at=evaluated_at,
        good_count=int(good) if total > 0 else None,
        total_count=int(total) if total > 0 else None,
        error_budget_remaining_pct=error_budget_remaining_pct,
        burn_rate=burn_rate,
        evidence=[EvidenceReference(
            source=SOURCE,
            title=f"SLO query: {definition.workload}",
            observed_at=evaluated_at,
            reference=definition.query[:200],
        )],
    )


def collect_workload_slos(
    *,
    config_path: str = "",
    config_json: str = "",
    default_workspace_id: Optional[str] = None,
    query_logs_fn: Callable[..., list] = default_query_logs,
) -> list:
    definitions = load_slo_definitions(config_path=config_path, config_json=config_json)
    return [
        evaluate_slo(d, default_workspace_id=default_workspace_id, query_logs_fn=query_logs_fn)
        for d in definitions
    ]


def slo_summaries_to_findings(summaries: list) -> list:
    """One Finding per at-risk/breached SLO. Healthy/insufficient_data
    SLOs are informational only (surfaced via the summaries themselves),
    to keep the Findings list actionable."""
    findings = []
    for summary in summaries:
        if summary.state not in ("at_risk", "breached"):
            continue
        severity = Severity.HIGH if summary.state == "breached" else Severity.MEDIUM
        findings.append(Finding(
            category=FindingCategory.RELIABILITY.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=f"{summary.workload}: SLO {summary.state}",
            summary=(
                f"Observed availability {summary.observed_pct}% vs objective {summary.objective_pct}% "
                f"over the last {summary.window_hours}h (burn rate {summary.burn_rate})."
            ),
            business_impact=f"{summary.criticality.replace('_', ' ').title()} workload is {summary.state.replace('_', ' ')} against its SLO.",
            first_seen=summary.evaluated_at,
            last_seen=summary.evaluated_at,
            source=SOURCE,
            affected_workload_count=1,
            confidence=ConfidenceLevel.DERIVED.value,
            evidence=summary.evidence,
            recommended_action=(
                "Investigate the error budget burn source for this workload."
                if summary.state == "at_risk" else
                "SLO objective was violated this window -- investigate root cause and consider a customer notification."
            ),
            approval_required=False,
            executive_attention=summary.state == "breached" and summary.criticality == "customer_facing",
            # Deterministic evidence of real customer impact: the SLO
            # has actually breached (not merely at_risk) AND is
            # customer_facing -- see Finding.customer_impacting's
            # contract. An at_risk SLO, or a breached internal/
            # best_effort-criticality workload, is a real reliability
            # risk but is NOT yet (or never definitionally) customer
            # impact.
            customer_impacting=summary.state == "breached" and summary.criticality == "customer_facing",
            metadata={"workload": summary.workload, "error_budget_remaining_pct": summary.error_budget_remaining_pct},
            discriminator=summary.workload,
        ))
    return findings
