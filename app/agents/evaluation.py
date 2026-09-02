"""Lightweight, deterministic online evaluation for grounded agent
analysis (app/agents/analysis.py).

Every metric here is computed from already-validated structured data --
never from re-reading prompt/response text -- and never persisted or
emitted with any prompt/response content, only counts/booleans/
percentages (matching app/telemetry.py's existing "no content capture"
convention). Two independent sinks, matching the task's "persist
aggregate counters OR emit OTEL metrics" requirement:

  * an in-process, thread-safe aggregate counter (queryable via
    ``get_aggregate_summary()`` -- surfaced on ``/api/health``, and
    directly testable without a real Application Insights backend), and
  * OTEL counters (``app.telemetry.record_evaluation_metrics``), a
    no-op unless telemetry is configured.
"""

import threading
from dataclasses import dataclass
from typing import Optional

from app import telemetry

__all__ = [
    "EvaluationResult",
    "evaluate",
    "record_evaluation",
    "get_aggregate_summary",
    "reset_for_tests",
]


@dataclass(frozen=True)
class EvaluationResult:
    schema_valid: bool
    citation_count: int
    valid_citation_count: int
    unsupported_citation_count: int
    citation_validity_pct: Optional[float]
    citation_coverage_pct: Optional[float]
    action_policy_adherent: bool
    debate_used: bool
    agents_consulted: int

    def to_dict(self) -> dict:
        return {
            "schema_valid": self.schema_valid,
            "citation_count": self.citation_count,
            "valid_citation_count": self.valid_citation_count,
            "unsupported_citation_count": self.unsupported_citation_count,
            "citation_validity_pct": self.citation_validity_pct,
            "citation_coverage_pct": self.citation_coverage_pct,
            "action_policy_adherent": self.action_policy_adherent,
            "debate_used": self.debate_used,
            "agents_consulted": self.agents_consulted,
        }


def evaluate(
    *,
    result,
    schema_valid: bool,
    bundle_known_ids,
    action_metadata: list,
    debate_used: bool,
    agents_consulted: int,
) -> EvaluationResult:
    """Compute a deterministic EvaluationResult for one final analysis
    answer.

    `result` is an `app.agents.schema.AgentAnalysisResult` (or `None`
    when `schema_valid` is False). `bundle_known_ids` is the evidence
    bundle's own set of finding ids (see
    `app.agents.evidence.EvidenceBundle.known_ids`). `action_metadata` is
    the list of per-action approval metadata dicts (see
    `app.approval.analysis_action_metadata`) already computed for
    `result.recommended_actions`, in the same order.
    """
    if not schema_valid or result is None:
        return EvaluationResult(
            schema_valid=False, citation_count=0, valid_citation_count=0,
            unsupported_citation_count=0, citation_validity_pct=None, citation_coverage_pct=None,
            action_policy_adherent=True, debate_used=debate_used, agents_consulted=agents_consulted,
        )

    known_ids = set(bundle_known_ids)
    cited = list(result.evidence_ids)
    valid = [c for c in cited if c in known_ids]
    unsupported_count = len(cited) - len(valid)
    validity_pct = round(len(valid) / len(cited) * 100, 1) if cited else None
    coverage_pct = round(len(set(valid)) / len(known_ids) * 100, 1) if known_ids else None
    # Task-adherence: every action's metadata must have auto_executable
    # False (see app.approval.analysis_action_metadata, which raises
    # TaskAdherenceError before this ever sees a violating value -- this
    # is a second, independent verification of the same invariant).
    action_policy_adherent = all(item.get("auto_executable") is False for item in action_metadata)

    return EvaluationResult(
        schema_valid=True, citation_count=len(cited), valid_citation_count=len(valid),
        unsupported_citation_count=unsupported_count, citation_validity_pct=validity_pct,
        citation_coverage_pct=coverage_pct, action_policy_adherent=action_policy_adherent,
        debate_used=debate_used, agents_consulted=agents_consulted,
    )


class _EvaluationCounters:
    """Thread-safe, in-process aggregate counters -- same convention as
    app.operations.cache.SnapshotCache (a plain lock-guarded dict/counter
    set, no external store)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0
        self.schema_valid_count = 0
        self.schema_invalid_count = 0
        self.debate_count = 0
        self.total_citations = 0
        self.total_valid_citations = 0
        self.total_unsupported_citations = 0
        self.action_policy_violation_count = 0

    def record(self, evaluation: EvaluationResult) -> None:
        with self._lock:
            self.total += 1
            if evaluation.schema_valid:
                self.schema_valid_count += 1
            else:
                self.schema_invalid_count += 1
            if evaluation.debate_used:
                self.debate_count += 1
            self.total_citations += evaluation.citation_count
            self.total_valid_citations += evaluation.valid_citation_count
            self.total_unsupported_citations += evaluation.unsupported_citation_count
            if not evaluation.action_policy_adherent:
                self.action_policy_violation_count += 1

    def summary(self) -> dict:
        with self._lock:
            total = self.total
            return {
                "total_analyses": total,
                "schema_valid_count": self.schema_valid_count,
                "schema_invalid_count": self.schema_invalid_count,
                "schema_valid_pct": round(self.schema_valid_count / total * 100, 1) if total else None,
                "debate_count": self.debate_count,
                "total_citations": self.total_citations,
                "total_valid_citations": self.total_valid_citations,
                "total_unsupported_citations": self.total_unsupported_citations,
                "action_policy_violation_count": self.action_policy_violation_count,
            }


_counters = _EvaluationCounters()


def record_evaluation(evaluation: EvaluationResult) -> None:
    """Update the in-process aggregate counters AND emit OTEL metrics
    (the latter a no-op unless telemetry is configured) -- never
    prompt/response content, only the counts/booleans already on
    `evaluation`."""
    _counters.record(evaluation)
    telemetry.record_evaluation_metrics(
        schema_valid=evaluation.schema_valid,
        unsupported_citation_count=evaluation.unsupported_citation_count,
    )


def get_aggregate_summary() -> dict:
    """The in-process aggregate evaluation summary -- surfaced on
    ``/api/health`` (see app/main.py). Resets to all-zero on process
    restart; this is a live-process rollup, not a persisted store."""
    return _counters.summary()


def reset_for_tests() -> None:
    """Test-only hook: replace the module-level counters with a fresh
    instance. Production code never calls this."""
    global _counters
    _counters = _EvaluationCounters()
