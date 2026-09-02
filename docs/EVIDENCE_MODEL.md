# Evidence Model (app/operations/)

A deterministic layer of structured **Findings** built from real Azure
signals, sitting between raw Azure APIs and anything that reasons over
them (executive/ops dashboards, AI agents). Nothing in this package calls
an LLM or makes a judgment call — every fact is either directly reported
by a platform, or computed/correlated by explicit, documented logic. This
doc covers the schema, that deterministic-vs-AI boundary, the collectors
and their error semantics, the priority factors, and a worked example.

Built in two phases: **Phase 1** (incidents/reliability/capacity/SLOs --
`run_collection()`) and **Phase 2** (operational risk/hygiene --
Microsoft Defender for Cloud, Cost Management, Azure Backup, Azure
Update Manager, Key Vault expiry, Azure Automation, telemetry coverage,
and Service Health retirement advisories -- `run_full_collection()`,
which runs Phase 1 unchanged and adds the Phase 2 sources after it). See
`docs/AZURE_DATA_SOURCES.md` for the exhaustive per-source
provider/API/table, minimum RBAC role, configuration, expected envelope
status, and limitations reference; this doc stays focused on the shared
schema/model.

`app/operations/service.py` is the collection orchestrator
(`run_collection` for the original four Phase 1 sources,
`run_full_collection` for all fourteen Phase 1+2 sources); the
product-facing snapshot/brief/queue/workflow-state/handoff layer built on
top of it (`app/operations/snapshot.py`, `brief.py`, `queue.py`,
`state.py`, `handoff.py`) is wired to Flask under `/api/operations/*`
(`app/operations/routes.py`, registered from `app/main.py`) — see
`docs/OPERATIONS_API.md` for the full route/schema reference.

## Package layout

```
app/operations/
  models.py            Finding / EvidenceReference / ActionItem /
                        SLOSummary / CapacitySummary / BudgetSummary /
                        TelemetryCoverageSummary dataclasses,
                        enums, and UTC timestamp helpers.
  identifiers.py        Deterministic ID generation (sha256-based).
  priority.py           prioritize_findings() -- bands + explainable
                        factors, not an opaque score.
  config.py             OperationsConfig -- env-driven, strict parsing.
  errors.py             OperationsCollectionError.
  service.py            run_collection() / run_full_collection() --
                        the bounded orchestrators -- plus
                        summarize_coverage() (source coverage/gap
                        inventory).
  collectors/
    http.py             Shared ARM REST GET helper (DI: credential
                        factory + HTTP getter).
    arg.py               Shared Resource Graph query helper (DI:
                        query function).
    alerts.py            Azure Monitor alerts (Microsoft.AlertsManagement).
    changes.py           Activity Log change timeline + Resource Health
                        correlation.
    capacity.py          Microsoft.Compute / Azure OpenAI regional quota,
                        + deterministic exhaustion forecasting.
    slo.py               Configurable workload SLOs over Log Analytics.
    defender.py           Microsoft Defender for Cloud active alerts +
                        unhealthy assessments (Phase 2).
    cost.py               Cost Management budget thresholds + a
                        deterministic cost trend (Phase 2).
    backup.py             Azure Backup job/protected-item health via Log
                        Analytics (Phase 2).
    patches.py            Azure Update Manager missing-update/stale-
                        assessment findings via Resource Graph (Phase 2).
    keyvault.py           Key Vault certificate/secret/key expiry,
                        metadata-only (Phase 2).
    automation.py         Azure Automation failed/suspended jobs (Phase 2).
    telemetry.py          Diagnostic-settings + heartbeat coverage gaps,
                        with an explicit coverage denominator (Phase 2).
    advisories.py         Service Health retirement/deprecation
                        advisories via Resource Graph (Phase 2).
    legacy_scan.py        Adapter: app/azure_data.py's existing scan
                        signals (Resource Health, Service Health, NSG
                        drift, insecure storage, Advisor, Policy
                        compliance, resource hygiene, ownership) ->
                        Findings (Phase 3).
  cache.py               SnapshotCache -- thread-safe TTL cache keyed by
                        normalized subscription set (Phase 3).
  state.py               OperationsStateStore -- SQLite finding
                        workflow-state (status/owner/snooze/audit
                        history) (Phase 3).
  snapshot.py            get_snapshot() -- the ONE bounded, cached,
                        deduplicated, prioritized, workflow-state-merged
                        entry point Flask routes/UI consumers should use
                        (Phase 3).
  brief.py               build_brief() -- deterministic executive brief
                        (Phase 3).
  queue.py               build_queue() -- filtered/paginated/ranked
                        queue view (Phase 3).
  handoff.py             build_handoff()/persist_handoff() -- structured
                        shift handoff (Phase 3).
  routes.py              Flask blueprint, /api/operations/* (Phase 3).
```

See `docs/OPERATIONS_API.md` for the full Phase 3 (product-facing
snapshot/brief/queue/workflow-state/handoff) schema and route reference.

## The deterministic-vs-AI boundary

Every Finding carries a `confidence` field from a closed, four-value
vocabulary (`app.operations.models.ConfidenceLevel`). This is the entire
boundary — nothing else in the schema encodes "how sure are we":

| Level | Meaning | Example |
|---|---|---|
| `confirmed` | A platform directly reported this fact. | An Azure Monitor alert says Sev1/Fired; Activity Log says an operation `Failed`. |
| `derived` | Deterministically computed from platform data via explicit, documented thresholds/math. | Capacity is 92% of its ARM-reported limit, which is >= the configured critical threshold; an SLO's error-budget burn rate crossed its configured `at_risk_burn_rate`. |
| `correlated` | Deterministically correlated across two or more platform signals via explicit timestamp/resource matching. | A write/delete change on the same resource occurred within the configured correlation window before a Resource Health "Unavailable" event. |
| `estimated` | A deterministic forecast/extrapolation from historical data points. | A linear-regression projection of quota usage crossing its limit within 30 days. |

None of these is ever assigned by a model. If a future AI agent layers
reasoning on top of these Findings, that reasoning lives entirely outside
this package and outside the `confidence` field's vocabulary.

## Schema

### EvidenceReference

One citation backing a Finding/summary — never a full raw Azure payload
dump, and never a credential.

| Field | Type | Notes |
|---|---|---|
| `source` | str (`EvidenceSource` enum) | Canonical origin, e.g. `azure_monitor_alert`, `activity_log`, `resource_health`, `log_analytics_slo`, `arm_compute_usage`, `arm_openai_quota`. |
| `title` | str | Required, human-readable. |
| `observed_at` | str | Canonical UTC ISO-8601 (`...Z`, millisecond precision) — normalized in `__post_init__`. |
| `resource_id` | str \| None | ARM resource ID, when applicable. |
| `reference` | str \| None | A query/alert ID/correlation ID — never a token, key, or connection string. |
| `raw_excerpt` | str \| None | Bounded to 400 chars and defensively sanitized (redacts `Authorization:`/`Bearer ...`/`password=`/etc. shapes) before storage. This is defense-in-depth only — callers must never pass a real credential into this field in the first place. |

### Finding

| Field | Type | Notes |
|---|---|---|
| `id` | str | Deterministic (see below) unless explicitly supplied. |
| `category` | str (`FindingCategory`) | `incident`, `reliability`, `capacity`, `change`, `security`, `compliance`, `cost`, `backup`, `patch`, `certificate`, `automation`, `telemetry`, `ownership`. |
| `severity` | str (`Severity`) | `critical` / `high` / `medium` / `low` / `informational`. |
| `status` | str (`FindingStatus`) | `open` / `acknowledged` / `mitigating` / `resolved` / `suppressed`. |
| `title`, `summary`, `business_impact` | str | All required and non-blank. |
| `owner` | str | Empty string when unknown — never guessed. |
| `first_seen`, `last_seen` | str | Canonical UTC ISO-8601; `last_seen >= first_seen` is enforced. |
| `source` | str (`EvidenceSource`) | The primary originating collector. |
| `resource_id` | str \| None | Primary affected resource, when there is one. |
| `affected_resource_count`, `affected_workload_count` | int | >= 0. |
| `confidence` | str (`ConfidenceLevel`) | See the boundary table above. |
| `evidence` | list[EvidenceReference] | Must be actual `EvidenceReference` instances (validated). |
| `recommended_action` | str | May be empty (e.g. a resolved alert). |
| `approval_required` | bool | True when acting on this Finding needs human sign-off (e.g. a rollback). |
| `executive_attention` | bool | True when this belongs on an executive-facing view. |
| `customer_impacting` | bool | Deliberately separate from `executive_attention`/`category`/`severity` — see Priority below. Defaults `False`; a collector sets it `True` ONLY on deterministic evidence of real customer/workload impact (e.g. an active `ServiceIssue` Service Health incident, or a breached `customer_facing` SLO), never inferred from severity/category/executive_attention alone. |
| `metadata` | dict | Free-form, collector-specific detail (e.g. `{"workload": "checkout-api"}` for SLO linkage — see Priority below). |

### ActionItem, SLOSummary, CapacitySummary, BudgetSummary, TelemetryCoverageSummary

`ActionItem` is a proposed next step tied to a `finding_id` (deliberately
separate from `app.ado_integration.AdoProposal` — a future integration
can translate one into the other). `SLOSummary` and `CapacitySummary`
carry their collectors' domain-specific numbers (good/total,
error-budget/burn-rate; current/limit/headroom/forecast) with the same
strict `__post_init__` validation and `to_dict()` convention as `Finding`.
`BudgetSummary` (Phase 2, `collectors/cost.py`) carries every configured
Cost Management budget's amount/currentSpend/threshold_state -- like
`CapacitySummary`, every budget gets a summary (healthy ones included),
while only warning/critical ones become Findings. `TelemetryCoverageSummary`
(Phase 2, `collectors/telemetry.py`) is the explicit coverage denominator
behind a telemetry-gap check: `checked_count`/`covered_count`/
`coverage_pct`/`skipped_permission_errors` for one `gap_type`
(`diagnostic_settings` or `heartbeat`).

## Deterministic IDs

`Finding.id` is `sha256(category|source|resource_id|discriminator)[:16]`,
prefixed with a short category code (e.g. `inc-`, `chg-`, `cap-`,
`rel-`). Re-running a collector against the same underlying Azure state
produces the *same* ID — there is no random UUID or wall-clock timestamp
anywhere in ID generation. Callers must pass a `discriminator` (an alert
ID, an SLO workload name, a correlation timestamp+resource pair, ...)
whenever `(category, source, resource_id)` alone wouldn't be unique.

## Priority (not a 0-100 score)

`app.operations.priority.prioritize_findings()` sorts Findings
most-urgent-first into four explainable bands (`P1`-`P4`) and returns the
**factors** behind each ranking on `PrioritizedFinding.factors`:

- `customer_impact` — mirrors `Finding.customer_impacting` exactly (see
  `app.operations.priority.is_customer_impacting`): True ONLY when the
  Finding itself was built with explicit, deterministic impact evidence.
  NEVER derived from `executive_attention`, `severity`, or `category`
  alone — a compliance/capacity/security/cost Finding, an
  executive-attention reliability Finding with no impact evidence (e.g.
  Resource Health "Unavailable" for an authorized VM stop), and an
  at_risk-but-not-yet-breached SLO are real operational risk but are
  NOT customer impact.
- `severity_rank` — 0 (critical) .. 4 (informational).
- `slo_state` / `slo_state_rank` — joined via `finding.metadata["workload"]`
  against a caller-supplied `{workload: SLOSummary.state}` map; a Finding
  with no related workload is treated neutrally (same rank as "healthy"),
  never penalized or boosted by an unrelated SLO.
- `age_hours` — time since `first_seen`.
- `confidence_rank` — 0 (`confirmed`) .. 3 (`estimated`).

Band assignment: `P1` if `severity_rank == 0` (critical) or the linked
SLO is `breached`; `P2` if `severity_rank == 1` (high), the linked SLO is
`at_risk`, or `customer_impact` is True; `P3` if `severity_rank == 2`
(medium); else `P4`. Within a band, ties break on
`(severity_rank, slo_state_rank, confidence_rank, -age_hours)` — older
Findings sort first. There is no hidden weighting or numeric score;
every input to the ordering is on `factors` for a UI/agent to display
verbatim ("P1 because: critical severity, SLO breached, 6 hours old").

## Collectors and error semantics

Every collector function takes its Azure clients (`credential_factory`,
`http_get`, `query_logs_fn`) as **injectable parameters with real
defaults** — tests pass fakes and make zero live network calls. A
collector **raises `OperationsCollectionError`** (never returns a
success-shaped empty list) on auth failure, a non-2xx ARM response, a
malformed response body, or a Log Analytics query failure.

| Collector | Signal | API / mechanism |
|---|---|---|
| `alerts.collect_fired_alerts` | Fired + recently-resolved Azure Monitor alerts | `Microsoft.AlertsManagement/alerts` (ARM REST; no azure-mgmt-* SDK covers this surface) |
| `changes.get_change_timeline` | Write/delete Activity Log operations | `AzureActivity` (`CategoryValue == 'Administrative'`) via `app.azure_data.query_logs` |
| `changes.get_resource_health_events` | Resource Health state transitions | `AzureActivity` (`CategoryValue == 'ResourceHealth'`) via the same Log Analytics client |
| `changes.correlate_changes_with_health` | Change → degraded/unavailable health correlation | Pure timestamp/resource-scoped window match — no model involved |
| `changes.get_failed_change_findings` | Failed changes | One Finding per `Failed` change; successful changes stay timeline/evidence data (see below) |
| `capacity.collect_compute_capacity` | Regional Compute vCPU/quota usage | `Microsoft.Compute/locations/{region}/usages` (ARM REST) |
| `capacity.collect_openai_capacity` | Regional Azure OpenAI/Cognitive Services quota | `Microsoft.CognitiveServices/locations/{region}/usages` (ARM REST) |
| `capacity.compute_exhaustion_forecast` | Deterministic exhaustion forecast | Least-squares linear regression over caller-supplied `(datetime, value)` history; `not_available` with < 2 points, `not_applicable` for a flat/decreasing trend — never invented |
| `slo.collect_workload_slos` | Configurable workload SLOs | User-authored KQL via `app.azure_data.query_logs`; see below |

**Phase 2 collectors** (full table with API versions/tables/RBAC roles
in `docs/AZURE_DATA_SOURCES.md`):

| Collector | Signal | API / mechanism |
|---|---|---|
| `defender.collect_active_alerts` | Active high/medium Microsoft Defender for Cloud alerts | `Microsoft.Security/alerts` (ARM REST) -- `FindingCategory.SECURITY` |
| `defender.collect_unhealthy_assessments` | Unhealthy Defender for Cloud posture recommendations | `Microsoft.Security/assessments` (ARM REST) -- `FindingCategory.COMPLIANCE`, never re-aggregated into a Secure Score |
| `cost.collect_budget_summaries` / `budget_summaries_to_findings` | Budget threshold state | `Microsoft.Consumption/budgets` (ARM REST) |
| `cost.collect_cost_trend` | Deterministic period-over-period actual-cost trend | `Microsoft.CostManagement/query` (ARM REST, `ActualCost`/`Daily` granularity, ONE request covering both periods, split/summed locally) -- explicitly NOT Cost Management's native anomaly-detection feature |
| `backup.get_backup_jobs` / `backup_job_findings` | Failed/stuck-in-progress backup jobs | `AddonAzureBackupJobs` via `app.azure_data.query_logs` |
| `backup.get_protected_item_health` / `protected_item_findings` | Protection-error/stale protected items | `CoreAzureBackup` via the same Log Analytics client |
| `patches.collect_patch_compliance` | Missing critical/security updates + stale/failed assessments | Resource Graph `patchassessmentresources` table |
| `keyvault.collect_key_vault_expiry` | Certificate/secret/key expiry within a configurable window | Key Vault data-plane List APIs (metadata only -- never a value) |
| `automation.collect_automation_failures` | Failed/suspended Automation jobs | `Microsoft.Automation/.../jobs` (ARM REST) |
| `telemetry.collect_diagnostic_settings_coverage` | Resources lacking diagnostic settings | Per-resource `microsoft.insights/diagnosticSettings` (ARM REST), over a caller-bounded resource-id list |
| `telemetry.collect_heartbeat_coverage` | Resources with no recent Log Analytics heartbeat | `Heartbeat` via `app.azure_data.query_logs` |
| `advisories.collect_retirement_advisories` | Retirement/deprecation advisories, with a deadline when published | Resource Graph `ServiceHealthResources` table (`EventType == 'HealthAdvisory'`) |

**Why only failed changes (and correlations) become their own Findings:**
hundreds of routine successful writes/deletes in a busy subscription would
drown out actionable signal if each became a Finding. Successful changes
are retained as raw timeline data (`get_change_timeline`) and folded into
correlation Findings when they precede a health degradation — that is
where a successful change becomes actionable.

**Azure OpenAI quota is a subscription+region aggregate, not
per-deployment.** At the time of writing, ARM does not expose a reliably
available per-deployment TPM-consumption endpoint; `Microsoft.
CognitiveServices/locations/{region}/usages` is the reliably-available
ARM-level view (matches the phase-1 requirement's "where reliably
available" qualifier). If Azure later ships a per-deployment consumption
API, `collect_openai_capacity` is the place to add it.

## Configurable workload SLOs

Loaded from **exactly one** of an inline JSON blob (`SLO_DEFINITIONS_JSON`)
or a JSON file path (`SLO_DEFINITIONS_PATH`) — see
`config/slo_definitions.example.json` for the schema. Setting **neither**
produces an explicit `not_configured` collection status; setting **both**
is a configuration error (`OperationsConfigError`), not a silent
precedence rule. Each definition's `query` must return one row with
numeric `good`/`total` columns (names configurable per definition); the
state per workload is:

- `insufficient_data` — the window returned zero total events (never a
  fabricated 100%/0% uptime).
- `breached` — observed availability is below the objective this window.
- `at_risk` — the objective is currently met, but the error-budget burn
  rate has crossed the definition's `at_risk_burn_rate` (an early-warning
  "won't stay healthy at this rate" signal, matching standard SRE
  multi-window burn-rate practice).
- `healthy` — otherwise.

## Collection orchestration (`app.operations.service`)

```python
from app.operations.service import run_collection, run_full_collection, all_findings, summarize_coverage
from app.operations.priority import prioritize_findings

# Phase 1 only -- unchanged since Phase 1, exactly 4 envelopes:
envelopes = run_collection(
    subscription_ids=["<sub-id>"],
    locations=["eastus", "westus2"],   # regions actually in use -- discover via Resource Graph
)

# Phase 1 + Phase 2 -- 14 envelopes (run_collection's 4, unchanged, plus
# 10 more; see the Phase 2 collectors table above):
envelopes = run_full_collection(
    subscription_ids=["<sub-id>"],
    locations=["eastus", "westus2"],
)
for env in envelopes:
    print(env.source, env.status, len(env.findings), env.error)

findings = all_findings(envelopes)      # flatten for combining with azure_data scan output later
ranked = prioritize_findings(findings)

coverage = summarize_coverage(envelopes)  # {"total_sources": 14, "ok_count": 10, "not_configured_count": 4, ...}
```

`run_collection` always returns exactly four `CollectionEnvelope`s (one
per source, fixed order: alerts, change/health, capacity, workload SLO)
-- this function and its behavior are **unchanged since Phase 1**.
`run_full_collection` calls `run_collection` internally and appends ten
more envelopes (defender_alerts, defender_assessments,
cost_management_budget, cost_management_trend, azure_backup,
update_manager, key_vault_expiry, automation_failures,
telemetry_coverage, retirement_advisories), always in that order --
fourteen total. Each envelope's `status` is one of:

- `ok` — the source ran successfully (possibly with zero Findings — an
  empty, healthy environment is a valid `ok` result).
  An `ok` envelope's optional `coverage_warning` field carries a
  non-fatal, explicit caveat about incomplete coverage -- e.g.
  `defender_assessments`' bounded `nextLink` pagination hitting a
  transient failure on a LATER page (not the first): the assessments
  already collected from earlier pages are still returned and normalized
  normally, `status` stays `ok`, and `coverage_warning` names what was
  missed instead of either silently dropping it or (the previous
  defect) letting that page's own per-item normalization problem
  escalate the entire source into `error`.
- `error` — the source failed (auth, API, or malformed-config); `error`
  carries a human-readable message. **A failure in one source never
  removes or blanks another source's envelope** — each is computed
  independently and no bare `except`/`pass` exists anywhere in the
  orchestrator or collectors.
- `not_configured` — the source has nothing to check by design (no
  regions supplied to the capacity collector, no SLO definitions set, an
  `enable_*` flag set to `false`, or a required Phase 2 input list
  -- `key_vault_monitor_uris`, `automation_account_ids`, or the combined
  telemetry resource-id set -- is empty) — distinct from both `ok`
  (nothing wrong) and `error` (something failed).
- `not_supported` — the source ran but the environment doesn't support
  the operation (currently only `cost_management_trend`, when Cost
  Management's Query API reports this billing scope/offer type doesn't
  support it) — distinct from `error` (a transient/auth failure you'd
  retry) and from `not_configured` (nothing was even attempted).

`summarize_coverage(envelopes)` returns a consolidated inventory --
`total_sources`, `ok_count`, `error_count`, `not_configured_count`,
`not_supported_count`, and `sources_by_status` (each status's list of
source names) -- e.g. for a UI to say "10 of 14 evidence sources
healthy; 4 not configured; 0 errors". It works over any list of
envelopes, not just `run_full_collection`'s output.

`all_findings(envelopes)` is the intended hook for combining this layer
with the existing `app/azure_data.py` scan output later (e.g. once
`detect_security_drift`/`get_deep_analysis` results are normalized into
`Finding`s too) — not implemented yet.

## Example JSON

A `Finding` (`to_dict()` output) for a correlated change→health event:

```json
{
  "id": "chg-f5b3a3f49ff3519d",
  "category": "change",
  "severity": "high",
  "status": "open",
  "title": "Change(s) preceded a unavailable health event",
  "summary": "1 write/delete change(s) occurred within 60 minute(s) before this resource was reported unavailable.",
  "business_impact": "Resource reported unavailable -- investigate whether the preceding change(s) caused it.",
  "owner": "",
  "first_seen": "2026-01-01T03:00:00.000Z",
  "last_seen": "2026-01-01T03:10:00.000Z",
  "source": "resource_health",
  "resource_id": "/subscriptions/.../virtualMachines/vm1",
  "affected_resource_count": 1,
  "affected_workload_count": 0,
  "confidence": "correlated",
  "evidence": [
    {"source": "resource_health", "title": "VM unavailable", "observed_at": "2026-01-01T03:10:00.000Z", "resource_id": "/subscriptions/.../vm1", "reference": "Microsoft.ResourceHealth/healthevent/action", "raw_excerpt": "PlatformInitiated"},
    {"source": "activity_log", "title": "Microsoft.Compute/virtualMachines/write", "observed_at": "2026-01-01T03:00:00.000Z", "resource_id": "/subscriptions/.../vm1", "reference": "corr-1", "raw_excerpt": "caller=alice@example.com; status=Succeeded"}
  ],
  "recommended_action": "Review the listed change(s) for a causal link to the health degradation; roll back if confirmed.",
  "approval_required": true,
  "executive_attention": true,
  "customer_impacting": false,
  "metadata": {"correlation_window_minutes": 60, "matched_change_count": 1}
}
```

A `CollectionEnvelope` (`to_dict()` output) for a failed source:

```json
{
  "source": "azure_monitor_alerts",
  "status": "error",
  "collected_at": "2026-01-01T04:00:00.000Z",
  "findings": [],
  "summaries": [],
  "error": "[azure_monitor_alert] /subscriptions/.../providers/Microsoft.AlertsManagement/alerts returned HTTP 500 (boom)",
  "coverage_warning": null
}
```

An `ok`-status `CollectionEnvelope` with a non-fatal `coverage_warning`
(a later Defender assessments page failed mid-pagination; the earlier
page's assessments were still collected and normalized):

```json
{
  "source": "defender_assessments",
  "status": "ok",
  "collected_at": "2026-01-01T04:00:00.000Z",
  "findings": [ "...1 Finding from page 1..." ],
  "summaries": [],
  "error": null,
  "coverage_warning": "[defender_assessment] .../providers/Microsoft.Security/assessments returned HTTP 503 (Service Unavailable)"
}
```

## Configuration

See `.env.example` for the full list (and `docs/AZURE_DATA_SOURCES.md`
for the Phase 2 knobs alongside each source's other details). Phase 1
summary:

| Variable | Default | Meaning |
|---|---|---|
| `ALERT_LOOKBACK_HOURS` | `24` | How far back to look for fired/resolved Azure Monitor alerts. |
| `CHANGE_LOOKBACK_HOURS` | `24` | How far back to pull the Activity Log change timeline and Resource Health events. |
| `CHANGE_CORRELATION_WINDOW_MINUTES` | `60` | How far before a degraded/unavailable health event to look for a preceding change. |
| `CAPACITY_WARNING_PCT` / `CAPACITY_CRITICAL_PCT` | `75` / `90` | Usage-percentage thresholds for Compute/OpenAI quota Findings. `WARNING` must be `< CRITICAL`. |
| `SLO_DEFINITIONS_PATH` | _(empty)_ | Path to an SLO definitions JSON file (see `config/slo_definitions.example.json`). |
| `SLO_DEFINITIONS_JSON` | _(empty)_ | Inline SLO definitions JSON (takes priority; setting both is an error). |

Phase 2 summary (see `docs/AZURE_DATA_SOURCES.md` for the full
per-source detail):

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_DEFENDER_ALERTS` / `ENABLE_DEFENDER_ASSESSMENTS` | `true` / `true` | Deliberately disable either Defender for Cloud source. |
| `COST_BUDGET_WARNING_PCT` / `COST_BUDGET_CRITICAL_PCT` | `80` / `100` | Budget usage-percentage thresholds. `WARNING` must be `< CRITICAL` (not capped at 100 -- a budget can be over-spent). |
| `COST_TREND_LOOKBACK_DAYS` / `COST_TREND_GROWTH_PCT_THRESHOLD` | `30` / `20` | Period length and material-growth threshold for the deterministic cost-trend collector. |
| `BACKUP_LOOKBACK_HOURS` / `BACKUP_STALE_RECOVERY_POINT_DAYS` | `24` / `3` | Job lookback window; days without a recovery point before a protected item is "stale". |
| `PATCH_ASSESSMENT_STALE_DAYS` | `7` | Days without an assessment refresh before it's "stale" (matches Resource Graph's own 7-day retention for this table). |
| `KEY_VAULT_EXPIRY_WARNING_DAYS` / `KEY_VAULT_MONITOR_URIS` / `KEY_VAULT_MAX_ITEMS_PER_TYPE` | `30` / _(empty)_ / `200` | Expiry warning window; comma-separated vault URIs to check (empty -> `not_configured`); bounded items per object type per vault. |
| `AUTOMATION_LOOKBACK_HOURS` / `AUTOMATION_ACCOUNT_IDS` | `24` / _(empty)_ | Job lookback window; comma-separated Automation Account resource ids to check (empty -> `not_configured`). |
| `TELEMETRY_MONITORED_RESOURCE_TYPES` / `TELEMETRY_CRITICAL_RESOURCE_IDS` / `TELEMETRY_MAX_RESOURCES` | curated built-in list / _(empty)_ / `50` | Resource-type allowlist for bounded Resource Graph discovery; explicit pinned resource ids; overall cap on per-resource diagnostic-settings calls. |
| `TELEMETRY_HEARTBEAT_LOOKBACK_HOURS` | `24` | How far back to check for a Log Analytics `Heartbeat` row. |
| `RETIREMENT_WARNING_DAYS` | `180` | Days-to-deadline threshold below which a retirement/deprecation advisory is `high` severity. |

## Known limitations / API assumptions

- Azure OpenAI capacity is subscription+region aggregate quota, not a
  per-deployment breakdown (see above).
- Capacity exhaustion forecasting requires the caller to inject a
  `history_provider` (historical usage time series); Phase 1 does not
  ship a built-in time-series store, so forecasts are `not_available`
  by default until one is wired up.
- Alert owner tags require an injected `resource_owner_lookup` callable
  (e.g. built from `app.azure_data.get_tagging_compliance`); the
  Alerts Management API itself carries no owner/tag information.
- Change→health correlation is a timestamp+resource(-group) match, not a
  guarantee of causation — it is presented as "investigate", not "this
  change caused it".
- Key Vault, Automation, and telemetry-coverage collectors operate on a
  caller/config-supplied resource list, never an internal subscription-
  wide discovery of "every vault"/"every account" -- an empty list is a
  deliberate `not_configured` state, not a bug.
- The cost-trend collector is a deterministic period-over-period
  actual-cost comparison, not Cost Management's native (still-evolving)
  anomaly-detection feature -- see `docs/AZURE_DATA_SOURCES.md`.
- Diagnostic-settings coverage counts ANY configured destination
  (Log Analytics, Storage, or Event Hub) as "covered" -- it does not
  verify the destination is specifically Log Analytics.
- Retirement/deprecation advisories are collected as ALL active
  `HealthAdvisory` Service Health events, not filtered to
  `eventSubType == 'Retirement'` only (Microsoft does not publish a
  fixed, guaranteed-complete enum of subtypes; see `advisories.py`).
- `app.operations.service.run_collection`/`run_full_collection` remain
  the only entry points for RAW per-source Findings; the deduplicated,
  prioritized, workflow-state-aware view Flask routes/UI consumers
  should use instead is `app.operations.snapshot.get_snapshot` (see
  `docs/OPERATIONS_API.md`).
