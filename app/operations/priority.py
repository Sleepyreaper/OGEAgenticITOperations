"""Deterministic Finding prioritization.

There is no opaque 0-100 priority score anywhere in this module. Every
Finding is placed in a small, explainable priority band (P1-P4) and the
individual factors that produced that band -- customer impact, severity,
related SLO state, age, and evidence confidence -- are exposed on
`PrioritizedFinding.factors` so a caller (dashboard or agent) can show
*why* something is P1, not just that it is.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from app.operations.models import CONFIDENCE_RANK, SEVERITY_RANK, Finding, FindingCategory, parse_utc_iso, utc_now

__all__ = [
    "PriorityBand",
    "PriorityFactors",
    "PrioritizedFinding",
    "is_customer_impacting",
    "prioritize_findings",
]


class PriorityBand(str, Enum):
    P1_CRITICAL = "P1"
    P2_HIGH = "P2"
    P3_MEDIUM = "P3"
    P4_LOW = "P4"


# Lower rank = more urgent SLO state. A Finding with no related SLO (the
# common case) is treated neutrally -- the same rank as "healthy" -- so
# the absence of an SLO relationship never itself raises or lowers
# priority.
_SLO_STATE_RANK = {
    "breached": 0,
    "at_risk": 1,
    "healthy": 2,
    "insufficient_data": 3,
    "not_configured": 3,
}
_SLO_STATE_RANK_NEUTRAL = _SLO_STATE_RANK["healthy"]


@dataclass(frozen=True)
class PriorityFactors:
    """The inputs that produced a Finding's priority band, exposed
    verbatim rather than folded into a single score."""
    customer_impact: bool
    severity_rank: int
    slo_state: Optional[str]
    slo_state_rank: int
    age_hours: float
    confidence_rank: int

    def to_dict(self) -> dict:
        return {
            "customer_impact": self.customer_impact,
            "severity_rank": self.severity_rank,
            "slo_state": self.slo_state,
            "slo_state_rank": self.slo_state_rank,
            "age_hours": round(self.age_hours, 2),
            "confidence_rank": self.confidence_rank,
        }


@dataclass(frozen=True)
class PrioritizedFinding:
    finding: Finding
    band: str
    factors: PriorityFactors

    def to_dict(self) -> dict:
        return {
            "finding": self.finding.to_dict(),
            "band": self.band,
            "factors": self.factors.to_dict(),
        }


def is_customer_impacting(finding: Finding) -> bool:
    """Deterministic, not inferred: True when the Finding is flagged for
    executive attention, or its category is one where impact is
    definitionally customer-facing (an active incident or a reliability/
    SLO Finding). Exported (not just used internally by
    prioritize_findings) so other product-facing services -- e.g.
    app.operations.brief's business_impact section -- apply the exact
    same deterministic rule rather than re-deriving a slightly different
    one."""
    if finding.executive_attention:
        return True
    return finding.category in (FindingCategory.INCIDENT.value, FindingCategory.RELIABILITY.value)


def _priority_band(*, customer_impact: bool, severity_rank: int, slo_state_rank: int) -> PriorityBand:
    if severity_rank == 0 or slo_state_rank == 0:
        return PriorityBand.P1_CRITICAL
    if severity_rank == 1 or slo_state_rank == 1 or customer_impact:
        return PriorityBand.P2_HIGH
    if severity_rank == 2:
        return PriorityBand.P3_MEDIUM
    return PriorityBand.P4_LOW


_BAND_RANK = {
    PriorityBand.P1_CRITICAL.value: 0,
    PriorityBand.P2_HIGH.value: 1,
    PriorityBand.P3_MEDIUM.value: 2,
    PriorityBand.P4_LOW.value: 3,
}


def prioritize_findings(
    findings: list,
    *,
    slo_state_by_workload: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> list:
    """Sort Findings most-urgent-first and attach the factors behind each
    ranking.

    `slo_state_by_workload` (workload name -> SLOSummary.state) lets a
    caller join a Finding to a related workload's current SLO state via
    `finding.metadata["workload"]` -- purely additive context, never
    required. Sort key (ascending): (priority band, severity rank, SLO
    state rank, confidence rank, -age_hours) -- older Findings within an
    otherwise-tied group sort first.
    """
    now = now or utc_now()
    slo_state_by_workload = slo_state_by_workload or {}

    prioritized = []
    for finding in findings:
        severity_rank = SEVERITY_RANK[finding.severity]
        confidence_rank = CONFIDENCE_RANK[finding.confidence]
        workload = finding.metadata.get("workload") if isinstance(finding.metadata, dict) else None
        slo_state = slo_state_by_workload.get(workload) if workload else None
        slo_state_rank = _SLO_STATE_RANK.get(slo_state, _SLO_STATE_RANK_NEUTRAL) if slo_state else _SLO_STATE_RANK_NEUTRAL
        age_hours = max(0.0, (now - parse_utc_iso(finding.first_seen)).total_seconds() / 3600.0)
        customer_impact = is_customer_impacting(finding)

        band = _priority_band(customer_impact=customer_impact, severity_rank=severity_rank, slo_state_rank=slo_state_rank)
        factors = PriorityFactors(
            customer_impact=customer_impact,
            severity_rank=severity_rank,
            slo_state=slo_state,
            slo_state_rank=slo_state_rank,
            age_hours=age_hours,
            confidence_rank=confidence_rank,
        )
        prioritized.append((
            (_BAND_RANK[band.value], severity_rank, slo_state_rank, confidence_rank, -age_hours),
            PrioritizedFinding(finding=finding, band=band.value, factors=factors),
        ))

    prioritized.sort(key=lambda pair: pair[0])
    return [item for _, item in prioritized]
