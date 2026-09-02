"""Bounded evidence-collection orchestrator.

Gathers all operations signal collectors (Azure Monitor alerts, the
Activity Log change timeline + Resource Health correlation, regional
capacity/quota, and configurable workload SLOs) for an explicit, bounded
list of subscription IDs, and returns one CollectionEnvelope per source.
A failure in one source is captured as that source's own 'error'
envelope and never prevents the others from completing -- callers must
never see a single Azure API outage silently blank out the whole
evidence layer.

Phase 2 adds eight more operational risk/hygiene sources (Microsoft
Defender for Cloud alerts + assessments, Cost Management budgets +
trend, Azure Backup, Azure Update Manager, Key Vault expiry, Azure
Automation, telemetry coverage, and Service Health retirement
advisories) behind `run_full_collection()` -- see that function's
docstring. `run_collection()` itself is UNCHANGED (same four sources,
same order, same signature/behavior) so existing Phase 1 callers/tests
keep working exactly as before; `run_full_collection()` is strictly
additive on top of it.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from app.operations.collectors import alerts as alerts_collector
from app.operations.collectors import advisories as advisories_collector
from app.operations.collectors import arg as arg_collector
from app.operations.collectors import automation as automation_collector
from app.operations.collectors import backup as backup_collector
from app.operations.collectors import capacity as capacity_collector
from app.operations.collectors import changes as changes_collector
from app.operations.collectors import cost as cost_collector
from app.operations.collectors import defender as defender_collector
from app.operations.collectors import keyvault as keyvault_collector
from app.operations.collectors import patches as patches_collector
from app.operations.collectors import slo as slo_collector
from app.operations.collectors import telemetry as telemetry_collector
from app.operations.collectors.http import (
    CredentialFactory,
    HttpGet,
    HttpPost,
    default_credential_factory,
    default_http_get,
    default_http_post,
)
from app.operations.config import OperationsConfig
from app.operations.errors import OperationsCollectionError
from app.operations.models import EvidenceSource, utc_now_iso

__all__ = [
    "CollectionEnvelope",
    "collect_alerts_envelope",
    "collect_change_health_envelope",
    "collect_capacity_envelope",
    "collect_slo_envelope",
    "run_collection",
    "collect_defender_alerts_envelope",
    "collect_defender_assessments_envelope",
    "collect_cost_budget_envelope",
    "collect_cost_trend_envelope",
    "collect_backup_envelope",
    "collect_update_manager_envelope",
    "collect_key_vault_expiry_envelope",
    "collect_automation_envelope",
    "collect_telemetry_coverage_envelope",
    "collect_retirement_advisories_envelope",
    "run_full_collection",
    "summarize_coverage",
    "all_findings",
]

_VALID_STATUSES = {"ok", "error", "not_configured", "not_supported"}


@dataclass
class CollectionEnvelope:
    """One collector source's result, explicit about which state it's in
    -- never a bare list that leaves 'ok with nothing found' and 'failed
    silently' indistinguishable."""
    source: str
    status: str  # "ok" | "error" | "not_configured" | "not_supported"
    collected_at: str
    findings: list = field(default_factory=list)  # list[Finding]
    summaries: list = field(default_factory=list)  # source-specific: list[SLOSummary] | list[CapacitySummary] | list[BudgetSummary] | list[TelemetryCoverageSummary]
    error: Optional[str] = None

    def __post_init__(self):
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"CollectionEnvelope.status must be one of {sorted(_VALID_STATUSES)}, got {self.status!r}")
        if self.status in ("error", "not_supported") and not self.error:
            raise ValueError(f"CollectionEnvelope.error is required when status == {self.status!r}")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "status": self.status,
            "collected_at": self.collected_at,
            "findings": [f.to_dict() for f in self.findings],
            "summaries": [s.to_dict() for s in self.summaries],
            "error": self.error,
        }


# Exceptions a collector is expected to raise for a genuine, per-source
# data problem: OperationsCollectionError (an Azure API/auth failure --
# see app.operations.errors), plus ValueError/TypeError, which every
# collector's upstream normalization can also raise for malformed Azure
# data that isn't safe to turn into a Finding/summary -- e.g. a
# non-ISO-8601 timestamp (app.operations.models.ensure_utc_iso/
# parse_utc_iso), an out-of-range numeric field, or a record shaped
# wrongly enough that a Finding/SLOSummary/CapacitySummary/BudgetSummary
# dataclass's own __post_init__ validation rejects it (see
# app/operations/models.py). These three are the ONLY exception types
# _collect_envelope below catches -- a genuine programmer error (e.g.
# AttributeError/KeyError from a bug in this codebase, not from Azure's
# response) is deliberately left to propagate rather than being
# misreported as "this data source failed".
_EXPECTED_SOURCE_FAILURES = (OperationsCollectionError, ValueError, TypeError)


def _collect_envelope(
    source: str,
    collect_fn: Callable[[], object],
    *,
    classify_error: Optional[Callable[[BaseException], str]] = None,
) -> CollectionEnvelope:
    """Run one source's `collect_fn` -- a zero-argument callable
    returning either `findings` (a list) or `(findings, summaries)` --
    and wrap the result, or any `_EXPECTED_SOURCE_FAILURES` it raises,
    into that source's own CollectionEnvelope.

    Centralizes the try/except/CollectionEnvelope-construction
    boilerplate every envelope function below used to repeat (and, for
    most of them, only around OperationsCollectionError -- silently
    missing the ValueError/TypeError a malformed Azure record's
    Finding/summary normalization can just as legitimately raise). A
    failure caught here becomes ONLY this source's 'error' envelope; it
    can never prevent any other source (in run_collection/
    run_full_collection) from completing, and it never escapes to crash
    the whole /snapshot route.

    `classify_error` lets a source with its own documented status
    distinction (e.g. cost_management_trend's 'not_supported' billing
    scope -- see _is_cost_not_supported_error) map the caught exception
    to a CollectionEnvelope status other than the default 'error'.
    """
    try:
        result = collect_fn()
    except _EXPECTED_SOURCE_FAILURES as exc:
        status = classify_error(exc) if classify_error else "error"
        return CollectionEnvelope(source=source, status=status, collected_at=utc_now_iso(), error=str(exc))
    if isinstance(result, tuple):
        findings, summaries = result
    else:
        findings, summaries = result, []
    return CollectionEnvelope(source=source, status="ok", collected_at=utc_now_iso(), findings=findings, summaries=summaries)


def collect_alerts_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    resource_owner_lookup: Optional[Callable[[str], str]] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "azure_monitor_alerts"

    def _collect():
        findings = []
        for subscription_id in subscription_ids:
            findings.extend(alerts_collector.collect_fired_alerts(
                subscription_id,
                lookback_hours=config.alert_lookback_hours,
                resource_owner_lookup=resource_owner_lookup,
                credential_factory=credential_factory,
                http_get=http_get,
            ))
        return findings

    return _collect_envelope(source, _collect)


def collect_change_health_envelope(
    config: OperationsConfig,
    *,
    workspace_id: Optional[str] = None,
    query_logs_fn=changes_collector.default_query_logs,
) -> CollectionEnvelope:
    source = "activity_log_change_health"

    def _collect():
        changes = changes_collector.get_change_timeline(
            lookback_hours=config.change_lookback_hours, workspace_id=workspace_id, query_logs_fn=query_logs_fn,
        )
        health_events = changes_collector.get_resource_health_events(
            lookback_hours=config.change_lookback_hours, workspace_id=workspace_id, query_logs_fn=query_logs_fn,
        )
        findings = changes_collector.get_failed_change_findings(changes)
        findings.extend(changes_collector.correlate_changes_with_health(
            changes, health_events, correlation_window_minutes=config.change_correlation_window_minutes,
        ))
        return findings

    return _collect_envelope(source, _collect)


def collect_capacity_envelope(
    subscription_ids: list,
    locations: list,
    config: OperationsConfig,
    *,
    openai_locations: Optional[list] = None,
    history_provider=None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "capacity"
    if not locations:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="no Azure regions supplied (no resources discovered, or none passed to run_collection)",
        )

    def _collect():
        summaries = []
        for subscription_id in subscription_ids:
            summaries.extend(capacity_collector.collect_compute_capacity(
                subscription_id, locations,
                warning_pct=config.capacity_warning_pct, critical_pct=config.capacity_critical_pct,
                history_provider=history_provider, credential_factory=credential_factory, http_get=http_get,
            ))
            oi_locations = openai_locations if openai_locations is not None else locations
            if oi_locations:
                summaries.extend(capacity_collector.collect_openai_capacity(
                    subscription_id, oi_locations,
                    warning_pct=config.capacity_warning_pct, critical_pct=config.capacity_critical_pct,
                    history_provider=history_provider, credential_factory=credential_factory, http_get=http_get,
                ))
        findings = capacity_collector.capacity_summaries_to_findings(summaries)
        return findings, summaries

    return _collect_envelope(source, _collect)


def collect_slo_envelope(
    config: OperationsConfig,
    *,
    default_workspace_id: Optional[str] = None,
    query_logs_fn=slo_collector.default_query_logs,
) -> CollectionEnvelope:
    source = "workload_slo"
    if not config.slo_definitions_path and not config.slo_definitions_json:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="neither SLO_DEFINITIONS_PATH nor SLO_DEFINITIONS_JSON is set",
        )

    def _collect():
        summaries = slo_collector.collect_workload_slos(
            config_path=config.slo_definitions_path, config_json=config.slo_definitions_json,
            default_workspace_id=default_workspace_id, query_logs_fn=query_logs_fn,
        )
        findings = slo_collector.slo_summaries_to_findings(summaries)
        return findings, summaries

    # _collect_envelope's ValueError handling also covers malformed
    # SLO_DEFINITIONS_JSON/file content (see slo.load_slo_definitions) --
    # a config mistake, not a live API failure, but still a legitimate
    # per-source error that must not abort the whole collection run.
    return _collect_envelope(source, _collect)


def run_collection(
    subscription_ids: list,
    *,
    config: Optional[OperationsConfig] = None,
    locations: Optional[list] = None,
    openai_locations: Optional[list] = None,
    resource_owner_lookup: Optional[Callable[[str], str]] = None,
    history_provider=None,
    default_workspace_id: Optional[str] = None,
    query_logs_fn=None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> list:
    """Run every collector for `subscription_ids` and return one
    CollectionEnvelope per source (always 4, in a fixed order: alerts,
    change/health, capacity, workload SLO).

    `locations` should be the Azure regions actually in use (e.g. from
    `app.azure_data.query_resource_graph`) -- an empty/omitted list makes
    the capacity source report 'not_configured' rather than silently
    skipping capacity checks without saying so.
    """
    if not subscription_ids:
        raise ValueError("subscription_ids must be a non-empty list")
    config = config or OperationsConfig.from_env()
    query_logs_fn = query_logs_fn or changes_collector.default_query_logs

    return [
        collect_alerts_envelope(
            subscription_ids, config, resource_owner_lookup=resource_owner_lookup,
            credential_factory=credential_factory, http_get=http_get,
        ),
        collect_change_health_envelope(config, workspace_id=default_workspace_id, query_logs_fn=query_logs_fn),
        collect_capacity_envelope(
            subscription_ids, locations or [], config, openai_locations=openai_locations,
            history_provider=history_provider, credential_factory=credential_factory, http_get=http_get,
        ),
        collect_slo_envelope(config, default_workspace_id=default_workspace_id, query_logs_fn=query_logs_fn),
    ]


# ─── Phase 2: operational risk/hygiene collectors ──────────────────────

def collect_defender_alerts_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "defender_alerts"
    if not config.enable_defender_alerts:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="disabled via ENABLE_DEFENDER_ALERTS=false",
        )

    def _collect():
        findings = []
        for subscription_id in subscription_ids:
            findings.extend(defender_collector.collect_active_alerts(
                subscription_id, credential_factory=credential_factory, http_get=http_get,
            ))
        return findings

    return _collect_envelope(source, _collect)


def collect_defender_assessments_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "defender_assessments"
    if not config.enable_defender_assessments:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="disabled via ENABLE_DEFENDER_ASSESSMENTS=false",
        )

    def _collect():
        findings = []
        for subscription_id in subscription_ids:
            findings.extend(defender_collector.collect_unhealthy_assessments(
                subscription_id, credential_factory=credential_factory, http_get=http_get,
            ))
        return findings

    return _collect_envelope(source, _collect)


def collect_cost_budget_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "cost_management_budget"
    if not config.enable_cost_management_budget:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="disabled via ENABLE_COST_MANAGEMENT_BUDGET=false",
        )

    def _collect():
        summaries = []
        for subscription_id in subscription_ids:
            summaries.extend(cost_collector.collect_budget_summaries(
                subscription_id, warning_pct=config.cost_budget_warning_pct, critical_pct=config.cost_budget_critical_pct,
                credential_factory=credential_factory, http_get=http_get,
            ))
        findings = cost_collector.budget_summaries_to_findings(summaries)
        return findings, summaries

    return _collect_envelope(source, _collect)


# Substrings of an Azure Cost Management error that -- per Microsoft's
# documented behavior -- indicate the query/forecast API rejected the
# request because this billing scope/offer type doesn't support it (e.g.
# certain legacy/MSDN offers), as opposed to a transient or auth failure.
# Best-effort text matching on the real API error, not a fabricated
# classification -- see docs/AZURE_DATA_SOURCES.md.
_COST_NOT_SUPPORTED_MARKERS = ("not supported", "notsupported", "invalid billing account", "unsupported offer")


def _is_cost_not_supported_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _COST_NOT_SUPPORTED_MARKERS)


def _classify_cost_trend_error(exc: BaseException) -> str:
    """Only an OperationsCollectionError's message can legitimately
    signal 'not_supported' (the documented Cost Management billing-scope
    limitation -- see _is_cost_not_supported_error). A ValueError/
    TypeError here means the trend computation itself hit malformed
    data, which is always a plain 'error', never 'not_supported'."""
    if isinstance(exc, OperationsCollectionError) and _is_cost_not_supported_error(str(exc)):
        return "not_supported"
    return "error"


def collect_cost_trend_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_post: HttpPost = default_http_post,
) -> CollectionEnvelope:
    source = "cost_management_trend"
    if not config.enable_cost_management_trend:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="disabled via ENABLE_COST_MANAGEMENT_TREND=false",
        )

    def _collect():
        findings = []
        for subscription_id in subscription_ids:
            findings.extend(cost_collector.collect_cost_trend(
                subscription_id, lookback_days=config.cost_trend_lookback_days,
                growth_pct_threshold=config.cost_trend_growth_pct_threshold,
                credential_factory=credential_factory, http_post=http_post,
            ))
        return findings

    return _collect_envelope(source, _collect, classify_error=_classify_cost_trend_error)


def collect_backup_envelope(
    config: OperationsConfig,
    *,
    workspace_id: Optional[str] = None,
    query_logs_fn=backup_collector.default_query_logs,
) -> CollectionEnvelope:
    source = "azure_backup"
    if not config.enable_backup:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(), error="disabled via ENABLE_BACKUP=false",
        )

    def _collect():
        jobs = backup_collector.get_backup_jobs(
            lookback_hours=config.backup_lookback_hours, workspace_id=workspace_id, query_logs_fn=query_logs_fn,
        )
        items = backup_collector.get_protected_item_health(workspace_id=workspace_id, query_logs_fn=query_logs_fn)
        findings = backup_collector.backup_job_findings(jobs)
        findings.extend(backup_collector.protected_item_findings(items, stale_days=config.backup_stale_recovery_point_days))
        return findings

    return _collect_envelope(source, _collect)


def collect_update_manager_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    query_resource_graph_fn=arg_collector.default_query_resource_graph,
) -> CollectionEnvelope:
    source = "update_manager"
    if not config.enable_update_manager:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(), error="disabled via ENABLE_UPDATE_MANAGER=false",
        )

    def _collect():
        findings = patches_collector.collect_patch_compliance(
            subscription_ids, stale_days=config.patch_assessment_stale_days, query_fn=query_resource_graph_fn,
        )
        return findings

    return _collect_envelope(source, _collect)


def collect_key_vault_expiry_envelope(
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "key_vault_expiry"
    if not config.enable_key_vault_expiry:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(), error="disabled via ENABLE_KEY_VAULT_EXPIRY=false",
        )
    if not config.key_vault_monitor_uris:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="KEY_VAULT_MONITOR_URIS is not set (no vaults configured to check)",
        )

    def _collect():
        findings = keyvault_collector.collect_key_vault_expiry(
            list(config.key_vault_monitor_uris), warning_days=config.key_vault_expiry_warning_days,
            max_items_per_type=config.key_vault_max_items_per_type,
            credential_factory=credential_factory, http_get=http_get,
        )
        return findings

    return _collect_envelope(source, _collect)


def collect_automation_envelope(
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> CollectionEnvelope:
    source = "automation_failures"
    if not config.enable_automation:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(), error="disabled via ENABLE_AUTOMATION=false",
        )
    if not config.automation_account_ids:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="AUTOMATION_ACCOUNT_IDS is not set (no Automation Accounts configured to check)",
        )

    def _collect():
        findings = automation_collector.collect_automation_failures(
            list(config.automation_account_ids), lookback_hours=config.automation_lookback_hours,
            credential_factory=credential_factory, http_get=http_get,
        )
        return findings

    return _collect_envelope(source, _collect)


def _discover_monitored_resource_ids(
    subscription_ids: list,
    resource_types: tuple,
    max_resources: int,
    query_resource_graph_fn,
) -> list:
    """Bounded Resource Graph discovery of resource ids matching
    `resource_types` -- the "monitored resource types" half of telemetry
    coverage's configurable input (see OperationsConfig.
    telemetry_monitored_resource_types). Returns [] (not an error) when
    `resource_types` is empty -- an operator explicitly configuring zero
    types is a valid, deliberate way to rely solely on
    telemetry_critical_resource_ids instead."""
    if not resource_types or max_resources <= 0:
        return []
    type_list = ", ".join(f"'{t}'" for t in resource_types)
    query = f"Resources | where type in~ ({type_list}) | project id | take {max_resources}"
    rows = arg_collector.arg_query(
        query, subscription_ids=subscription_ids, source=EvidenceSource.TELEMETRY_COVERAGE.value,
        query_fn=query_resource_graph_fn,
    )
    return [row["id"] for row in rows if row.get("id")]


def collect_telemetry_coverage_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    workspace_id: Optional[str] = None,
    query_logs_fn=telemetry_collector.default_query_logs,
    query_resource_graph_fn=arg_collector.default_query_resource_graph,
    extra_resource_ids: Optional[list] = None,
) -> CollectionEnvelope:
    """Diagnostic-settings + Log Analytics heartbeat coverage for the
    bounded resource set built from `config.telemetry_critical_resource_ids`
    + `extra_resource_ids` + a bounded Resource Graph discovery of
    `config.telemetry_monitored_resource_types` (capped overall at
    `config.telemetry_max_resources`)."""
    source = "telemetry_coverage"
    if not config.enable_telemetry_coverage:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(), error="disabled via ENABLE_TELEMETRY_COVERAGE=false",
        )

    try:
        discovered = _discover_monitored_resource_ids(
            subscription_ids, config.telemetry_monitored_resource_types, config.telemetry_max_resources,
            query_resource_graph_fn,
        )
    except _EXPECTED_SOURCE_FAILURES as exc:
        return CollectionEnvelope(source=source, status="error", collected_at=utc_now_iso(), error=str(exc))

    combined = []
    seen = set()
    for resource_id in list(config.telemetry_critical_resource_ids) + list(extra_resource_ids or []) + discovered:
        key = resource_id.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        combined.append(resource_id)
    combined = combined[: config.telemetry_max_resources]

    if not combined:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(),
            error="no monitored resource types matched and no critical resource ids configured",
        )

    try:
        diagnostic_findings, diagnostic_summary = telemetry_collector.collect_diagnostic_settings_coverage(
            combined, credential_factory=credential_factory, http_get=http_get,
        )
        heartbeat_findings, heartbeat_summary = telemetry_collector.collect_heartbeat_coverage(
            combined, lookback_hours=config.telemetry_heartbeat_lookback_hours,
            workspace_id=workspace_id, query_logs_fn=query_logs_fn,
        )
    except _EXPECTED_SOURCE_FAILURES as exc:
        return CollectionEnvelope(source=source, status="error", collected_at=utc_now_iso(), error=str(exc))

    findings = diagnostic_findings + heartbeat_findings
    return CollectionEnvelope(
        source=source, status="ok", collected_at=utc_now_iso(), findings=findings,
        summaries=[diagnostic_summary, heartbeat_summary],
    )


def collect_retirement_advisories_envelope(
    subscription_ids: list,
    config: OperationsConfig,
    *,
    query_resource_graph_fn=arg_collector.default_query_resource_graph,
) -> CollectionEnvelope:
    source = "retirement_advisories"
    if not config.enable_retirement_advisories:
        return CollectionEnvelope(
            source=source, status="not_configured", collected_at=utc_now_iso(), error="disabled via ENABLE_RETIREMENT_ADVISORIES=false",
        )

    def _collect():
        findings = advisories_collector.collect_retirement_advisories(
            subscription_ids, warning_days=config.retirement_warning_days, query_fn=query_resource_graph_fn,
        )
        return findings

    return _collect_envelope(source, _collect)


def run_full_collection(
    subscription_ids: list,
    *,
    config: Optional[OperationsConfig] = None,
    locations: Optional[list] = None,
    openai_locations: Optional[list] = None,
    resource_owner_lookup: Optional[Callable[[str], str]] = None,
    history_provider=None,
    default_workspace_id: Optional[str] = None,
    query_logs_fn=None,
    query_resource_graph_fn=None,
    telemetry_resource_ids: Optional[list] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    http_post: HttpPost = default_http_post,
) -> list:
    """Run every Phase 1 AND Phase 2 collector and return one
    CollectionEnvelope per source: the same 4 `run_collection()` produces
    (in the same order), followed by 10 more -- defender_alerts,
    defender_assessments, cost_management_budget, cost_management_trend,
    azure_backup, update_manager, key_vault_expiry, automation_failures,
    telemetry_coverage, retirement_advisories -- always in that order.

    This is strictly additive over `run_collection()` (which is called
    internally, unchanged): existing callers/tests of `run_collection()`
    are entirely unaffected by Phase 2. Each Phase 2 source is bounded
    and independently configurable via `OperationsConfig`'s `enable_*`
    flags and per-domain input lists (`key_vault_monitor_uris`,
    `automation_account_ids`, `telemetry_monitored_resource_types`/
    `telemetry_critical_resource_ids`) -- a source with nothing
    configured to check reports 'not_configured' rather than silently
    skipping itself or making an expensive discovery call by default.
    """
    if not subscription_ids:
        raise ValueError("subscription_ids must be a non-empty list")
    config = config or OperationsConfig.from_env()
    query_logs_fn = query_logs_fn or changes_collector.default_query_logs
    query_resource_graph_fn = query_resource_graph_fn or arg_collector.default_query_resource_graph

    phase1 = run_collection(
        subscription_ids, config=config, locations=locations, openai_locations=openai_locations,
        resource_owner_lookup=resource_owner_lookup, history_provider=history_provider,
        default_workspace_id=default_workspace_id, query_logs_fn=query_logs_fn,
        credential_factory=credential_factory, http_get=http_get,
    )
    phase2 = [
        collect_defender_alerts_envelope(subscription_ids, config, credential_factory=credential_factory, http_get=http_get),
        collect_defender_assessments_envelope(subscription_ids, config, credential_factory=credential_factory, http_get=http_get),
        collect_cost_budget_envelope(subscription_ids, config, credential_factory=credential_factory, http_get=http_get),
        collect_cost_trend_envelope(subscription_ids, config, credential_factory=credential_factory, http_post=http_post),
        collect_backup_envelope(config, workspace_id=default_workspace_id, query_logs_fn=query_logs_fn),
        collect_update_manager_envelope(subscription_ids, config, query_resource_graph_fn=query_resource_graph_fn),
        collect_key_vault_expiry_envelope(config, credential_factory=credential_factory, http_get=http_get),
        collect_automation_envelope(config, credential_factory=credential_factory, http_get=http_get),
        collect_telemetry_coverage_envelope(
            subscription_ids, config, credential_factory=credential_factory, http_get=http_get,
            workspace_id=default_workspace_id, query_logs_fn=query_logs_fn, query_resource_graph_fn=query_resource_graph_fn,
            extra_resource_ids=telemetry_resource_ids,
        ),
        collect_retirement_advisories_envelope(subscription_ids, config, query_resource_graph_fn=query_resource_graph_fn),
    ]
    return phase1 + phase2


def summarize_coverage(envelopes: list) -> dict:
    """A consolidated inventory of source coverage/gaps across any list
    of CollectionEnvelopes -- e.g. "7 of 11 evidence sources healthy; 2
    not configured; 2 errors" for a UI/executive summary. Works over the
    output of `run_collection()`, `run_full_collection()`, or any other
    subset/superset of envelopes a caller assembles."""
    by_status = {"ok": [], "error": [], "not_configured": [], "not_supported": []}
    for envelope in envelopes:
        by_status.setdefault(envelope.status, []).append(envelope.source)
    return {
        "total_sources": len(envelopes),
        "ok_count": len(by_status["ok"]),
        "error_count": len(by_status["error"]),
        "not_configured_count": len(by_status["not_configured"]),
        "not_supported_count": len(by_status["not_supported"]),
        "sources_by_status": by_status,
    }


def all_findings(envelopes: list) -> list:
    """Flatten every envelope's Findings into one list -- the hook point
    for combining this evidence layer with existing app.azure_data scan
    output later (e.g. once detect_security_drift/get_deep_analysis
    results are also normalized into Findings)."""
    return [finding for envelope in envelopes for finding in envelope.findings]
