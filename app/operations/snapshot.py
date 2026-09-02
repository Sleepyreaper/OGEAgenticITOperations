"""Bounded, cached operations snapshot service.

The single entry point (`get_snapshot`) product-facing consumers
(app/operations/routes.py's /snapshot, /brief, /queue endpoints) call to
get ONE consistent, deduplicated, prioritized, workflow-state-merged view
of every evidence source this app knows about:

  - Phase 1 + Phase 2 (app.operations.service.run_full_collection --
    Azure Monitor alerts, change/health correlation, capacity, workload
    SLOs, Defender, Cost Management, Backup, Update Manager, Key Vault
    expiry, Automation, telemetry coverage, retirement advisories), and
  - the "classic" azure_data.py scan signals
    (app.operations.collectors.legacy_scan.collect_legacy_envelopes --
    Resource Health, Service Health, NSG drift, insecure storage,
    Advisor, Policy compliance, resource hygiene, ownership).

Results are cached in-process, keyed by the normalized subscription
selection (see app.operations.cache.normalize_subscription_key), for
OperationsConfig.snapshot_cache_ttl_seconds (default 60s,
OPERATIONS_SNAPSHOT_CACHE_TTL_SECONDS) -- so a burst of /brief, /queue,
and /snapshot calls in a short window never re-issues the same Azure
calls. `force_refresh=True` bypasses (and repopulates) the cache
explicitly.

Never caches a failure as a successful empty result: every
OperationsSnapshot this module builds and caches carries its own
per-source status/error detail (via `envelopes`/`source_errors`) and an
overall `status` ("ok" | "partial" | "error") -- a cached snapshot is
always the TRUTHFUL state as of `generated_at`, including any failures,
never a disguised "all clear."

Capacity coverage (Phase 1's `capacity` source) requires an explicit
`locations` list (see run_full_collection); this module does not
auto-discover regions -- pass `full_collect_kwargs={"locations": [...]}`
(app/operations/routes.py forwards `OperationsConfig.capacity_locations`
-- the CAPACITY_LOCATIONS env var, see app/operations/config.py -- as
`locations`/`openai_locations` for every route, never an unbounded
query-string parameter). Leaving it unset is a valid, safe default:
capacity simply reports `not_configured`, exactly as it does when
calling run_full_collection directly.
"""

import dataclasses
import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from app.operations.cache import SnapshotCache, normalize_subscription_key
from app.operations.collectors.legacy_scan import collect_legacy_envelopes
from app.operations.config import OperationsConfig
from app.operations.models import Finding, SEVERITY_RANK, format_utc_iso
from app.operations.priority import prioritize_findings
from app.operations.service import CollectionEnvelope, run_full_collection, summarize_coverage
from app.operations.state import OperationsStateStore, merge_workflow_state

__all__ = [
    "OperationsSnapshot",
    "get_snapshot",
    "invalidate_cache",
    "get_default_state_store",
]

# Envelope statuses that represent a genuine attempt to collect (as
# opposed to a deliberate, documented non-participation such as
# not_configured/not_supported) -- used to decide the snapshot's overall
# status; see _snapshot_status.
_APPLICABLE_STATUSES = {"ok", "error"}

_singleton_lock = threading.Lock()
_default_cache: Optional[SnapshotCache] = None
_default_state_store: Optional[OperationsStateStore] = None
_default_state_store_path: Optional[str] = None


def _get_default_cache(config: OperationsConfig) -> SnapshotCache:
    global _default_cache
    with _singleton_lock:
        if _default_cache is None or _default_cache.ttl_seconds != config.snapshot_cache_ttl_seconds:
            _default_cache = SnapshotCache(ttl_seconds=config.snapshot_cache_ttl_seconds)
    return _default_cache


def _get_default_state_store(config: OperationsConfig) -> OperationsStateStore:
    global _default_state_store, _default_state_store_path
    with _singleton_lock:
        if _default_state_store is None or _default_state_store_path != config.operations_state_db_path:
            _default_state_store = OperationsStateStore(config.operations_state_db_path)
            _default_state_store_path = config.operations_state_db_path
    return _default_state_store


def invalidate_cache() -> None:
    """Test/admin hook: clear the module-level default snapshot cache
    entirely (not the per-call caches a test may inject instead)."""
    with _singleton_lock:
        if _default_cache is not None:
            _default_cache.invalidate()


def get_default_state_store(config: Optional[OperationsConfig] = None) -> OperationsStateStore:
    """The same process-wide singleton OperationsStateStore `get_snapshot`
    uses internally -- exposed so app/operations/routes.py's workflow
    (PATCH /findings/<id>) and handoff (GET/POST /handoff) routes share
    the exact same in-process lock/connection pool rather than each
    constructing (and locking) its own instance against the same file."""
    return _get_default_state_store(config or OperationsConfig.from_env())


@dataclass
class OperationsSnapshot:
    id: str
    generated_at: str
    subscription_ids: tuple
    status: str  # "ok" | "partial" | "error"
    envelopes: list  # list[CollectionEnvelope]
    findings: list  # list[dict]: {"finding": ..., "workflow": ..., "priority": ...}, priority-ordered
    coverage: dict
    source_errors: list
    summary: dict

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "generated_at": self.generated_at,
            "subscription_ids": list(self.subscription_ids),
            "status": self.status,
            "envelopes": [e.to_dict() for e in self.envelopes],
            "findings": self.findings,
            "coverage": self.coverage,
            "source_errors": self.source_errors,
            "summary": self.summary,
        }


def _as_collection_envelope(raw) -> CollectionEnvelope:
    """Normalize a legacy_scan dict-shaped envelope into a real
    CollectionEnvelope (run_full_collection already returns real
    instances). app.operations.collectors.legacy_scan returns plain
    dicts to avoid a circular import with service.py -- this is where
    the two shapes converge into one type."""
    if isinstance(raw, CollectionEnvelope):
        return raw
    return CollectionEnvelope(
        source=raw["source"], status=raw["status"], collected_at=raw["collected_at"],
        findings=raw.get("findings") or [], summaries=raw.get("summaries") or [], error=raw.get("error"),
    )


def _merge_duplicate(a: Finding, b: Finding) -> Finding:
    """Combine two Findings that hashed to the same deterministic id
    (e.g. the same underlying event surfaced by two sources): keep the
    earliest first_seen, the latest last_seen, the more severe severity,
    the union of approval_required/executive_attention, the max affected
    counts, merged metadata, and a bounded de-duplicated evidence list."""
    first_seen = a.first_seen if a.first_seen <= b.first_seen else b.first_seen
    last_seen = a.last_seen if a.last_seen >= b.last_seen else b.last_seen
    severity = a.severity if SEVERITY_RANK[a.severity] <= SEVERITY_RANK[b.severity] else b.severity

    seen_evidence = set()
    evidence = []
    for item in list(a.evidence) + list(b.evidence):
        key = (item.source, item.reference, item.observed_at)
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        evidence.append(item)
    evidence = evidence[:10]  # bounded payload

    metadata = dict(a.metadata)
    metadata.update(b.metadata)

    return dataclasses.replace(
        a,
        severity=severity,
        first_seen=first_seen,
        last_seen=last_seen,
        affected_resource_count=max(a.affected_resource_count, b.affected_resource_count),
        affected_workload_count=max(a.affected_workload_count, b.affected_workload_count),
        evidence=evidence,
        approval_required=a.approval_required or b.approval_required,
        executive_attention=a.executive_attention or b.executive_attention,
        metadata=metadata,
    )


def _dedupe_findings(findings: list) -> list:
    """Merge Findings sharing the same deterministic id into one (see
    _merge_duplicate). Output preserves the input order of each id's
    first occurrence."""
    merged: dict = {}
    order: list = []
    for finding in findings:
        if finding.id not in merged:
            merged[finding.id] = finding
            order.append(finding.id)
            continue
        merged[finding.id] = _merge_duplicate(merged[finding.id], finding)
    return [merged[fid] for fid in order]


def _snapshot_status(envelopes: list) -> str:
    """"ok" unless at least one APPLICABLE source (ok/error -- excluding
    the deliberate not_configured/not_supported states) failed; "error"
    only when EVERY applicable source failed (so a caller never mistakes
    a total collection failure for "all clear"), "partial" otherwise."""
    applicable = [e for e in envelopes if e.status in _APPLICABLE_STATUSES]
    if not applicable:
        return "ok"
    error_count = sum(1 for e in applicable if e.status == "error")
    if error_count == len(applicable):
        return "error"
    if error_count > 0:
        return "partial"
    return "ok"


def _build_summary(ordered: list, coverage: dict) -> dict:
    by_severity: dict = {}
    by_category: dict = {}
    by_workflow_status: dict = {}
    executive_attention_count = 0
    approval_required_count = 0
    for item in ordered:
        finding = item["finding"]
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1
        by_category[finding["category"]] = by_category.get(finding["category"], 0) + 1
        workflow_status = item["workflow"]["status"]
        by_workflow_status[workflow_status] = by_workflow_status.get(workflow_status, 0) + 1
        if finding["executive_attention"]:
            executive_attention_count += 1
        if finding["approval_required"]:
            approval_required_count += 1
    return {
        "total_findings": len(ordered),
        "by_severity": by_severity,
        "by_category": by_category,
        "by_workflow_status": by_workflow_status,
        "executive_attention_count": executive_attention_count,
        "approval_required_count": approval_required_count,
        "source_coverage": coverage,
    }


def _compute_snapshot_id(subscription_key: tuple, generated_at: str) -> str:
    digest = hashlib.sha256(f"{','.join(subscription_key)}|{generated_at}".encode("utf-8")).hexdigest()[:16]
    return f"snap-{digest}"


def get_snapshot(
    subscription_ids: list,
    *,
    config: Optional[OperationsConfig] = None,
    force_refresh: bool = False,
    cache: Optional[SnapshotCache] = None,
    state_store: Optional[OperationsStateStore] = None,
    full_collect_fn: Callable = run_full_collection,
    legacy_collect_fn: Callable = collect_legacy_envelopes,
    full_collect_kwargs: Optional[dict] = None,
    legacy_collect_kwargs: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> OperationsSnapshot:
    """Return a bounded, cached OperationsSnapshot for `subscription_ids`.

    Cache key is the normalized subscription set (order/case/duplicate
    -insensitive) -- an equivalent selection reuses the same cached
    snapshot within the TTL window regardless of how the caller ordered/
    cased its subscription id list. `force_refresh=True` rebuilds and
    re-caches unconditionally (and evicts the stale entry first, so a
    concurrent reader never observes a torn/half-updated cache entry).
    """
    if not subscription_ids:
        raise ValueError("subscription_ids must be a non-empty list")

    config = config or OperationsConfig.from_env()
    cache = cache if cache is not None else _get_default_cache(config)
    state_store = state_store if state_store is not None else _get_default_state_store(config)
    now = now or datetime.now(timezone.utc)

    cache_key = normalize_subscription_key(subscription_ids)
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    else:
        cache.invalidate(cache_key)

    ordered_subs = list(subscription_ids)
    full_kwargs = dict(full_collect_kwargs or {})
    legacy_kwargs = dict(legacy_collect_kwargs or {})

    full_envelopes = list(full_collect_fn(ordered_subs, config=config, **full_kwargs))
    legacy_envelopes_raw = list(legacy_collect_fn(ordered_subs, **legacy_kwargs))
    envelopes = full_envelopes + [_as_collection_envelope(e) for e in legacy_envelopes_raw]

    all_findings = [f for envelope in envelopes for f in envelope.findings]
    deduped = _dedupe_findings(all_findings)

    prioritized = prioritize_findings(deduped, now=now)
    merged = merge_workflow_state([pf.finding for pf in prioritized], state_store, now=now)
    merged_by_id = {item["finding"]["id"]: item for item in merged}

    ordered = []
    for pf in prioritized:
        item = merged_by_id[pf.finding.id]
        item["priority"] = {"band": pf.band, "factors": pf.factors.to_dict()}
        ordered.append(item)

    coverage = summarize_coverage(envelopes)
    source_errors = [
        {"source": e.source, "status": e.status, "error": e.error}
        for e in envelopes if e.status == "error"
    ]
    status = _snapshot_status(envelopes)
    generated_at = format_utc_iso(now)

    snapshot = OperationsSnapshot(
        id=_compute_snapshot_id(cache_key, generated_at),
        generated_at=generated_at,
        subscription_ids=cache_key,
        status=status,
        envelopes=envelopes,
        findings=ordered,
        coverage=coverage,
        source_errors=source_errors,
        summary=_build_summary(ordered, coverage),
    )

    cache.set(cache_key, snapshot)
    return snapshot
