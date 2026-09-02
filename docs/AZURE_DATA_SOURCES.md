# Azure Data Sources — evidence collector reference

Exhaustive, per-source reference for every collector in `app/operations/`
(`docs/EVIDENCE_MODEL.md` covers the shared schema/model this doc feeds
into). For each **currently implemented** source this lists the exact
provider/API/table, the API version pinned in code, the minimum Azure
RBAC role needed to read it, how it's configured, every envelope status
it can produce and why, and its known limitations/assumptions. A final
section lists **future/optional integrations** that are deliberately
*not* implemented yet (and why), so it's never ambiguous whether a gap
is a bug or a documented, intentional scope boundary.

No live Azure calls are made by anything in `app/operations/` itself --
every collector takes its Azure client (`credential_factory`, `http_get`,
`http_post`, `query_logs_fn`, `query_resource_graph_fn`) as an injectable
parameter with a real default, so this doc's API versions/tables are the
*only* place those assumptions need to be kept current when Azure ships
a new version.

**Concurrency/thread-safety**: `app.operations.service.run_collection`/
`run_full_collection` run every source's collector concurrently (see
those functions' docstrings and `docs/OPERATIONS_API.md`'s "Concurrency
and collection performance" section) through a bounded thread pool. This
is safe because every collector already instantiates its own Azure
credential/client per call (`app.azure_data._credential()`,
`ResourceGraphClient`, `LogsQueryClient` -- never a module-level shared,
mutable SDK client) -- a pre-existing invariant this refactor depends on
rather than changes.

## Quick reference

| # | Source (`CollectionEnvelope.source`) | Phase | Domain | Mechanism | Can be `not_configured` | Can be `not_supported` |
|---|---|---|---|---|---|---|
| 1 | `azure_monitor_alerts` | 1 | Incidents | ARM REST | No | No |
| 2 | `activity_log_change_health` | 1 | Change/reliability | Log Analytics | No | No |
| 3 | `capacity` | 1 | Capacity | ARM REST | Yes (no regions supplied) | No |
| 4 | `workload_slo` | 1 | Reliability | Log Analytics (user KQL) | Yes (no SLO defs) | No |
| 5 | `defender_alerts` | 2 | Security | ARM REST | Yes (`ENABLE_DEFENDER_ALERTS=false`) | No |
| 6 | `defender_assessments` | 2 | Compliance/posture | ARM REST | Yes (`ENABLE_DEFENDER_ASSESSMENTS=false`) | No |
| 7 | `cost_management_budget` | 2 | Cost | ARM REST | Yes (flag) | No |
| 8 | `cost_management_trend` | 2 | Cost | ARM REST | Yes (flag) | Yes (unsupported billing scope) |
| 9 | `azure_backup` | 2 | Backup | Log Analytics | Yes (flag) | No |
| 10 | `update_manager` | 2 | Patch | Resource Graph | Yes (flag) | No |
| 11 | `key_vault_expiry` | 2 | Certificate | Key Vault data-plane | Yes (flag, or no vaults configured) | No |
| 12 | `automation_failures` | 2 | Automation | ARM REST | Yes (flag, or no accounts configured) | No |
| 13 | `telemetry_coverage` | 2 | Telemetry | ARM REST + Log Analytics | Yes (flag, or no resources resolved) | No |
| 14 | `retirement_advisories` | 2 | Compliance | Resource Graph | Yes (flag) | No |

Every source can also be `error` (an unexpected failure -- auth, a
non-2xx response, a malformed body, or a Log Analytics/Resource Graph
query exception) and `ok` (ran successfully, including "ran
successfully and found zero issues"). See `docs/EVIDENCE_MODEL.md`'s
"Collection orchestration" section for the full status semantics and
`app.operations.service.summarize_coverage()` for rolling all of this up
into one inventory.

---

## Phase 1 sources

### 1. `azure_monitor_alerts`

- **Provider / API**: `Microsoft.AlertsManagement/alerts` (ARM REST;
  no `azure-mgmt-*` SDK covers this surface). API version:
  `2019-05-05-preview`.
- **Minimum RBAC role**: `Reader` (or the more specific `Monitoring
  Reader`) at subscription scope.
- **Configuration**: `ALERT_LOOKBACK_HOURS` (default `24`).
- **Envelope status**: `ok` (including zero alerts) or `error` (auth
  failure, non-2xx, malformed body). Never `not_configured` -- there is
  no optional input for this source.
- **Limitations**: owner attribution requires an injected
  `resource_owner_lookup` callable; the Alerts API itself carries no
  owner/tag information. The API's `timeRange` query parameter only
  accepts coarse buckets (1h/1d/7d/30d) -- the collector requests the
  smallest covering bucket and then filters precisely by timestamp in
  Python.

### 2. `activity_log_change_health`

- **Provider / API**: `AzureActivity` table (`CategoryValue ==
  'Administrative'` for changes, `'ResourceHealth'` for health
  transitions) via `app.azure_data.query_logs` (Log Analytics).
- **Minimum RBAC role**: `Log Analytics Reader` on the target
  workspace (`LOG_ANALYTICS_WORKSPACE_ID`).
- **Configuration**: `CHANGE_LOOKBACK_HOURS` (default `24`),
  `CHANGE_CORRELATION_WINDOW_MINUTES` (default `60`).
- **Envelope status**: `ok` or `error` (Log Analytics query failure).
  Never `not_configured`.
- **Limitations**: requires the Activity Log to be exported (via
  diagnostic settings) to the configured Log Analytics workspace --
  a subscription with no such export produces an empty (not missing)
  `ok` result. Change→health correlation is a timestamp+resource(-group)
  window match, not a causation guarantee.

### 3. `capacity`

- **Provider / API**: `Microsoft.Compute/locations/{region}/usages` and
  `Microsoft.CognitiveServices/locations/{region}/usages` (ARM REST).
  API versions: `2024-07-01` (Compute), `2024-10-01` (Cognitive
  Services).
- **Minimum RBAC role**: `Reader` at subscription scope.
- **Configuration**: `CAPACITY_WARNING_PCT` / `CAPACITY_CRITICAL_PCT`
  (default `75` / `90`); `locations` is supplied by the caller (e.g.
  discovered via Resource Graph, or -- when calling
  `app.operations.service.run_collection`/`run_full_collection` directly
  -- an explicit list). The product-facing routes in
  `app/operations/routes.py` instead read `CAPACITY_LOCATIONS`, a
  comma-separated, validated list of ARM region slugs (e.g.
  `eastus2,westeurope`; see `OperationsConfig.capacity_locations` in
  `app/operations/config.py`), and forward it as both `locations` and
  `openai_locations` -- there is no `?locations=` query-string override.
  Optionally, `OPENAI_CAPACITY_NAME_FILTERS` (a comma-separated,
  case-insensitive substring allowlist; see
  `OperationsConfig.openai_capacity_name_filters`) narrows the Azure
  OpenAI/Cognitive Services quotas this source reports on, applied
  BEFORE threshold classification -- a shared Cognitive Services
  account can carry many unrelated, always-fully-allocated model
  quotas (e.g. Claude/image models provisioned for other teams) that
  would otherwise dominate a capacity executive summary. Unset (the
  default) means no filtering. This setting is profile-independent
  (not tied to any one profile's prompts/branding) and NEVER filters
  `Microsoft.Compute` usages -- only Cognitive Services/Azure OpenAI.
- **Envelope status**: `not_configured` when no `locations` are
  supplied; otherwise `ok` or `error`.
- **Limitations**: Azure OpenAI quota here is a subscription+region
  *aggregate*, not a per-deployment breakdown (see
  `docs/EVIDENCE_MODEL.md`). Exhaustion forecasting needs an injected
  `history_provider`; without one, forecasts are `not_available`.

### 4. `workload_slo`

- **Provider / API**: user-authored KQL against Log Analytics via
  `app.azure_data.query_logs` -- there is no fixed Azure API/table here
  by design (SLOs are workload-specific).
- **Minimum RBAC role**: `Log Analytics Reader` on whichever
  workspace(s) the SLO definitions target.
- **Configuration**: exactly one of `SLO_DEFINITIONS_PATH` /
  `SLO_DEFINITIONS_JSON` (see `config/slo_definitions.example.json`).
- **Envelope status**: `not_configured` when neither is set; `error`
  for a malformed definition file/JSON or a failing query; otherwise
  `ok`.
- **Limitations**: entirely dependent on the quality of the operator-
  authored KQL; the collector validates shape (good/total columns) but
  cannot validate business correctness of a custom query.

---

## Phase 2 sources

### 5. `defender_alerts`

- **Provider / API**: `Microsoft.Security/alerts` List (ARM REST).
  API version: `2022-01-01`.
- **Minimum RBAC role**: `Security Reader` (or `Reader`) at subscription
  scope.
- **Configuration**: `ENABLE_DEFENDER_ALERTS` (default `true`). Severity
  filter (`High`/`Medium`, status `Active`) is a fixed policy, not
  configurable -- see `collectors/defender.py`.
- **Envelope status**: `not_configured` when disabled via the flag;
  otherwise `ok` or `error`.
- **Limitations**: Low/Informational alerts and non-Active statuses
  (Resolved/Dismissed) are intentionally excluded from Findings -- they
  remain visible in the Defender for Cloud portal itself. Requires at
  least one Defender for Cloud plan enabled to produce any alerts;
  Foundational CSPM alone does not generate alerts (only assessments).

### 6. `defender_assessments`

- **Provider / API**: `Microsoft.Security/assessments` List (ARM REST).
  API version: `2020-01-01`.
- **Minimum RBAC role**: `Security Reader` (or `Reader`) at subscription
  scope.
- **Configuration**: `ENABLE_DEFENDER_ASSESSMENTS` (default `true`).
- **Envelope status**: `not_configured` when disabled; otherwise `ok` or
  `error`.
- **Limitations**: assessments describe a live posture *state*, not a
  timestamped event -- there is no "since when has this been Unhealthy"
  field, so `first_seen`/`last_seen` are both set to collection time.
  This module deliberately never re-aggregates assessments into an
  invented Secure Score-like number; each Unhealthy assessment is its
  own Finding.
- **Missing/unrecognized `metadata.severity`**: some assessment
  types/tenants have been observed returning `metadata.severity` as
  `None`/missing or an unrecognized value entirely -- unlike an active
  alert (`defender_alerts`, whose severity handling stays strict and
  still raises), this is downgraded to `informational` with
  `metadata.severity_unknown=True` and `executive_attention=False`
  rather than erroring the entire source (see
  `defender.normalize_assessment`/`_assessment_severity_from_raw`).
- **Partial pagination coverage**: a LATER assessments page (not the
  first) failing to fetch -- e.g. a transient timeout/5xx -- does not
  fail this source or discard the earlier page(s) already collected
  (see `app.operations.collectors.http.paginated_get`'s bounded
  partial-result contract). It surfaces as this envelope's
  `coverage_warning` with `status` staying `ok` -- see
  `service.collect_defender_assessments_envelope`.

### 7. `cost_management_budget`

- **Provider / API**: `Microsoft.Consumption/budgets` List (ARM REST).
  API version: `2023-05-01`.
- **Minimum RBAC role**: `Cost Management Reader` at subscription scope
  (Cost Management's own permission model, not covered by the generic
  ARM `Reader` role's `*/read` wildcard).
- **Configuration**: `COST_BUDGET_WARNING_PCT` / `COST_BUDGET_CRITICAL_PCT`
  (default `80` / `100`; `WARNING` must be `< CRITICAL`, and neither is
  capped at 100 -- a budget can be over-spent).
  `ENABLE_COST_MANAGEMENT_BUDGET` (default `true`).
- **Envelope status**: `not_configured` when disabled; otherwise `ok`
  (including zero budgets configured in Azure -- a legitimate state, not
  a gap) or `error`.
- **Limitations**: only evidence-backed dollar amounts from the budget's
  own `amount`/`currentSpend.amount` are ever normalized -- nothing is
  extrapolated or invented.

### 8. `cost_management_trend`

- **Provider / API**: `Microsoft.CostManagement/query` (ARM REST, POST).
  API version: `2023-11-01`. Query type `ActualCost`, granularity
  `Daily` (one summed `Cost` row per calendar day), aggregation
  `totalCost` -> `Cost`/`Sum`.
- **Minimum RBAC role**: `Cost Management Reader` at subscription scope.
- **Configuration**: `COST_TREND_LOOKBACK_DAYS` (default `30`),
  `COST_TREND_GROWTH_PCT_THRESHOLD` (default `20`),
  `ENABLE_COST_MANAGEMENT_TREND` (default `true`).
- **Envelope status**: `not_configured` when disabled; `not_supported`
  when the Cost Management API's own error response indicates this
  billing scope/offer type doesn't support the query (best-effort text
  match on Azure's own error message -- see
  `service._is_cost_not_supported_error`); otherwise `ok` or `error`.
- **Why this is NOT "anomaly detection"**: Azure Cost Management ships a
  portal-level anomaly-detection feature, but at the time of writing it
  has no stable, generally-available REST API this collector can call
  reliably. Rather than fabricate an "anomaly" from a heuristic, this
  collector is named and documented as a **deterministic period-over-
  period actual-cost trend**: it compares total cost over the last
  `COST_TREND_LOOKBACK_DAYS` against the immediately preceding period of
  equal length, and raises a Finding only when growth reaches the
  configured threshold. If Azure later ships a stable anomaly-detection
  REST API, that becomes a new, separately-named collector/source --
  never silently merged into this one.
- **A single request covers both periods**: `collect_cost_trend` issues
  exactly ONE `Microsoft.CostManagement/query` POST per invocation, with
  a custom `timePeriod` spanning `prior_start` (the start of the prior
  comparison window) through `current_end` (`now`) -- covering both the
  current and prior `COST_TREND_LOOKBACK_DAYS`-day windows in one call.
  The response's daily rows are parsed and split/summed into the
  current vs. prior period locally (`prior_start <= UsageDate <
  current_start` is prior; `current_start <= UsageDate <= current_end`
  is current -- compared as plain dates, since Daily-granularity rows
  carry no time-of-day), rather than asking Azure for two separate
  pre-summed totals. This replaced an earlier two-query design (one
  `granularity: None` query per period) that intermittently hit HTTP
  429 on the second, back-to-back query even with `arm_post`'s bounded
  retries -- see "Throttling resilience" below.
- **Response parsing**: columns are resolved dynamically by name, never
  by a fixed index. The date-bucket column is `UsageDate` (the
  universally observed shape for this API -- an integer or numeric
  string in `YYYYMMDD` form, e.g. `20240115`/`"20240115"`; an
  ISO-8601 date/datetime string, e.g. `"2024-01-15"` or
  `"2024-01-15T00:00:00Z"`, is also accepted) with `date` accepted as an
  explicit, tested fallback column name. The cost column is `Cost` (the
  name this collector itself requests) with `PreTaxCost` accepted as a
  documented fallback. If the response has rows but no recognized
  date/cost column, this collector raises `OperationsCollectionError`
  rather than fabricate which column to read.
- **Limitations**: returns no Finding (never a fabricated one) when the
  prior period has zero/negative cost -- there is no meaningful baseline
  percentage in that case.
- **Throttling resilience**: `Microsoft.CostManagement/query` has been
  observed throttling aggressively (HTTP 429) under real load, which
  used to fail this entire source outright. `app.operations.collectors.
  http.arm_post` (used by every ARM POST in this codebase, not just
  this collector) now retries a 429 or a transient 5xx with bounded,
  Retry-After-honoring exponential backoff (default up to 3 attempts,
  hard-capped at 5) before raising -- a genuine, persistent failure
  still surfaces as an explicit `error` envelope exactly as before; any
  OTHER 4xx (400/401/403/404/...) is never retried. Because
  `collect_cost_trend` now makes only ONE `arm_post` call per
  invocation (see above) instead of two, there is no longer a second,
  independent request that can be throttled after the first succeeds.

### 9. `azure_backup`

- **Provider / API**: `AddonAzureBackupJobs` (failed/stuck-in-progress
  jobs) and `CoreAzureBackup` (protected-item health) tables via
  `app.azure_data.query_logs` (Log Analytics).
- **Minimum RBAC role**: `Log Analytics Reader` on the workspace the
  Recovery Services vault(s) send diagnostics to.
- **Configuration**: `BACKUP_LOOKBACK_HOURS` (default `24`),
  `BACKUP_STALE_RECOVERY_POINT_DAYS` (default `3`), `ENABLE_BACKUP`
  (default `true`).
- **Envelope status**: `not_configured` when disabled, OR when
  `AddonAzureBackupJobs`/`CoreAzureBackup` doesn't exist yet in the
  target workspace -- a KQL "table not found" semantic error (Azure's
  own error text: `...Failed to resolve table or column ... expression
  named '<table>'`), which means no Recovery Services vault has ever
  sent diagnostics there, as distinct from a transient/auth/network
  failure (see `service._classify_log_analytics_table_error`); otherwise
  `ok` or `error`.
- **Why Log Analytics instead of per-vault Recovery Services REST
  calls**: this generically covers every vault sending diagnostics to
  one workspace in two queries total, with no vault enumeration or
  per-vault pagination -- bounded and cheap regardless of how many
  vaults exist. The explicit assumption this requires: **every Recovery
  Services vault must have diagnostic settings configured** sending
  `AzureBackupJobs`/`AzureBackupProtectedInstance` categories to that
  workspace; a vault without diagnostics configured is silently absent
  from this source's results (an `ok`, not `error`, empty-for-that-vault
  result -- indistinguishable, at this layer, from "that vault has no
  problems"). This is the same class of assumption `workload_slo`/
  `activity_log_change_health` already make about Log Analytics
  availability.
- **Assumption**: a job still `InProgress` after `STALE_IN_PROGRESS_HOURS`
  (24h, a fixed constant in `collectors/backup.py`, not currently
  user-configurable) is treated as stuck. Items in
  `ProtectionStopped` state are excluded (deliberately not protected,
  not a hygiene gap).

### 10. `update_manager`

- **Provider / API**: Resource Graph `patchassessmentresources` table
  (`type =~ 'microsoft.compute/virtualmachines/patchassessmentresults'`
  or the Arc-machine equivalent).
- **Minimum RBAC role**: `Reader` at subscription scope (Resource Graph
  read access).
- **Configuration**: `PATCH_ASSESSMENT_STALE_DAYS` (default `7` --
  matches Resource Graph's own 7-day retention window for this table,
  per Microsoft's documentation), `ENABLE_UPDATE_MANAGER` (default
  `true`).
- **Envelope status**: `not_configured` when disabled; otherwise `ok` or
  `error`.
- **Assumption (explicit)**: `availablePatchCountByClassification`'s
  dictionary keys (`critical`/`security`) are parsed **case-
  insensitively** in Python rather than hard-coded to one casing in KQL,
  because Microsoft's own published sample queries for this table use
  lowercase keys, while other Resource Graph security tables in this
  codebase (see `defender`/`advisories` below) have shown different
  casing conventions across API generations. This collector never
  assumes a single casing is guaranteed stable.
- **Limitations**: requires the machine to be onboarded to Azure Update
  Manager (VM or Arc-enabled server) with periodic assessment enabled;
  a machine with no `patchassessmentresources` row is silently absent
  (not flagged) -- Update Manager onboarding coverage itself is not
  checked by this collector.

### 11. `key_vault_expiry`

- **Provider / API**: Key Vault data-plane List APIs --
  `GET {vaultUri}/certificates`, `/secrets`, `/keys` (never a Get-value
  endpoint). API version: `7.4`.
- **Minimum RBAC role**: `Key Vault Reader` (Azure RBAC permission
  model) -- grants metadata/list access to certificates, secrets, and
  keys, and explicitly **cannot** read secret/key contents or
  certificate private data. **Only works for vaults using the "Azure
  role-based access control" permission model** -- a vault still on the
  legacy vault-level access-policy model instead needs an access policy
  granting `List` on each object type; either way, the identity must
  never be granted `Get` on values for this collector's purpose.
- **Configuration**: `KEY_VAULT_EXPIRY_WARNING_DAYS` (default `30`),
  `KEY_VAULT_MONITOR_URIS` (comma-separated vault URIs; empty by
  default), `KEY_VAULT_MAX_ITEMS_PER_TYPE` (default `200`, bounds
  pagination per vault per object type), `ENABLE_KEY_VAULT_EXPIRY`
  (default `true`).
- **Envelope status**: `not_configured` when disabled or when
  `KEY_VAULT_MONITOR_URIS` is empty; `error` only when **every**
  (vault, object type) pair fails (a total collection failure, e.g. an
  invalid credential); otherwise `ok`.
- **Partial RBAC handling**: a permission failure on ONE (vault, object
  type) pair (e.g. List granted on secrets but not certificates) does
  NOT abort the whole run -- it becomes its own low-severity Finding
  ("Cannot check {type} expiry in {vault}") so the monitoring blind spot
  is visible, while every other (vault, object type) pair still
  completes normally.
- **Never collects secret values**: the List APIs used here return only
  `id`/`attributes`/`tags` by design -- there is no code path in this
  collector that calls a Get-value endpoint.
- **Limitations**: items with no `exp` (expiry) attribute set are out of
  scope (nothing to warn about); disabled items are skipped (not
  currently in use, so expiry isn't an active risk). `resource_id` on
  these Findings is always `None` -- Key Vault items are not independent
  ARM resources, only the vault itself is; the vault URI is carried in
  `metadata` instead.

### 12. `automation_failures`

- **Provider / API**: `Microsoft.Automation/automationAccounts/jobs`
  List by Automation Account (ARM REST). API version: `2024-10-23`.
- **Minimum RBAC role**: `Reader` at the scope containing the
  Automation Account(s).
- **Configuration**: `AUTOMATION_LOOKBACK_HOURS` (default `24`),
  `AUTOMATION_ACCOUNT_IDS` (comma-separated Automation Account ARM
  resource ids; empty by default), `ENABLE_AUTOMATION` (default `true`).
- **Envelope status**: `not_configured` when disabled or when
  `AUTOMATION_ACCOUNT_IDS` is empty; otherwise `ok` or `error`.
- **Limitations**: only `Failed`/`Suspended` jobs become Findings;
  `Completed`/`Stopped`/`Running` jobs are not (mirrors the "only
  failures become Findings" convention used throughout this package).
  The lookback filter is applied server-side via OData
  (`properties/creationTime ge datetime'...'`).

### 13. `telemetry_coverage`

- **Provider / API**: two independent checks over the SAME bounded
  resource-id set:
  - Diagnostic settings: `GET {resourceId}/providers/
    microsoft.insights/diagnosticSettings` (ARM REST) per resource.
    API version: `2021-05-01-preview` (the only version this API has
    ever shipped, per Microsoft's own REST reference -- there is no
    later GA version to migrate to).
  - Heartbeat: the `Heartbeat` Log Analytics table (Azure Monitor
    Agent/Log Analytics agent) via `app.azure_data.query_logs`.
- **Minimum RBAC role**: `Reader` at the scope containing the monitored
  resources (diagnostic settings check); `Log Analytics Reader` on the
  target workspace (heartbeat check).
- **Configuration**: `TELEMETRY_MONITORED_RESOURCE_TYPES`
  (comma-separated resource types; defaults to a curated built-in list
  -- VMs, App Service, SQL servers, Key Vault, Storage, Application
  Gateway, AKS, PostgreSQL flexible servers -- see
  `app/operations/config.py`), `TELEMETRY_CRITICAL_RESOURCE_IDS`
  (comma-separated, explicit pins regardless of type),
  `TELEMETRY_MAX_RESOURCES` (default `50` -- bounds the TOTAL number of
  per-resource diagnostic-settings ARM calls in one run, combining
  discovered + pinned resources), `TELEMETRY_HEARTBEAT_LOOKBACK_HOURS`
  (default `24`), `ENABLE_TELEMETRY_COVERAGE` (default `true`).
- **Envelope status**: `not_configured` when disabled, or when the
  combined (discovered + pinned) resource-id set is empty; `error` only
  when EVERY resource's diagnostic-settings check fails (a total
  failure); ALSO `not_configured` (never `error`) when the `Heartbeat`
  table doesn't exist yet in the target workspace (no Azure Monitor
  Agent/Log Analytics agent has ever reported in) -- the same KQL
  "table not found" classification `azure_backup` uses, see
  `service._classify_log_analytics_table_error`; otherwise `ok`.
- **Why not "every resource in the subscription"**: most Azure resource
  types don't support `Microsoft.Insights/diagnosticSettings` at all,
  and Resource Graph does not index diagnostic settings as a queryable
  resource type -- there is no single query that answers "does this
  resource have a diagnostic setting" for an arbitrary resource type, so
  this MUST be a bounded, explicit, per-resource ARM REST check. Hence
  the configurable allowlist/pin-list/cap rather than an unbounded scan.
- **Explicit coverage denominator**: every run returns a
  `TelemetryCoverageSummary` per check (`checked_count`, `covered_count`,
  `coverage_pct`, `skipped_permission_errors`) -- e.g. "22 of 30 monitored
  resources have diagnostic settings" -- not just the gap Findings on
  their own.
- **Partial RBAC handling**: a permission failure checking ONE resource's
  diagnostic settings does not abort the run; failures are tallied and
  surfaced as ONE aggregate Finding (never one Finding per failed
  resource, which would be unbounded noise for a large resource list).
- **Limitations**: "covered" for diagnostic settings means ANY
  destination is configured (Log Analytics, Storage, or Event Hub) --
  it does not verify the destination is specifically Log Analytics (the
  heartbeat check is the one that verifies actual Log Analytics
  ingestion, for VM-class resources only).

### 14. `retirement_advisories`

- **Provider / API**: Resource Graph `ServiceHealthResources` table,
  `type =~ 'Microsoft.ResourceHealth/events'`, `EventType ==
  'HealthAdvisory'`, `Status == 'Active'`.
- **Minimum RBAC role**: `Reader` at subscription scope.
- **Configuration**: `RETIREMENT_WARNING_DAYS` (default `180` -- the
  days-to-deadline threshold below which an advisory is `high`
  severity), `ENABLE_RETIREMENT_ADVISORIES` (default `true`).
- **Envelope status**: `not_configured` when disabled; otherwise `ok` or
  `error`.
- **Deadline extraction**: `ImpactMitigationTime`/`ImpactStartTime` are
  pulled via `tostring(properties.*)` -- deliberately NOT Microsoft's
  own published `todatetime(tolong(...))` sample query pattern, which
  assumes an epoch-millisecond `dynamic` representation. In practice
  these properties come back from Resource Graph as ISO-8601 datetime
  strings, and `tolong()` against that non-numeric `dynamic` value is
  what produced this query's real `ParserFailure`. The actual datetime
  parsing/validation happens in Python instead
  (`app.operations.models.ensure_utc_iso`/`parse_utc_iso`, which already
  tolerate either a datetime or an ISO string) -- simpler and removes
  the ARG-side type-coercion risk entirely. `metadata.deadline` is set
  when Azure has published one; advisories with no published deadline
  are `low` severity rather than assigned an invented one.
- **Reserved-name pitfall**: live probing against a real subscription
  proved this query otherwise succeeds right up until `project`ing an
  alias literally named `priority` -- Azure Resource Graph's Kusto
  dialect treats that name as reserved/problematic and rejects it with
  a `ParserFailure` of its own, independent of the
  `todatetime(tolong(...))` issue above. `properties.Priority` is
  therefore extended/projected as `advisoryPriority` instead (see
  `advisories.QUERY`); `normalize_advisory` reads the row by that same
  key. Only the ARG-side column name changed -- the Finding's own
  `metadata.priority` output key is unaffected.
- **Scope (explicit assumption)**: this collects ALL active
  `HealthAdvisory` events, not filtered to `EventSubType == 'Retirement'`
  only. Microsoft's own documentation states "all upcoming service
  retirement events are part of all active health advisory events",
  confirming Retirement is a subset -- but Microsoft does not publish a
  fixed, guaranteed-complete enum of `EventSubType` values, so filtering
  strictly to `'Retirement'` risks silently dropping a legitimate
  deprecation notice under a different subtype. Under-collecting is
  judged the worse failure mode here.

---

## Future / optional integrations (not implemented)

These are deliberately out of scope for the current collectors -- either
because there is no stable/GA API surface to build on yet, or because
they duplicate an existing, already-implemented signal. Listed so a
future contributor (or reviewer) doesn't have to rediscover why a gap
exists.

- **Native Cost Management anomaly detection** -- see
  `cost_management_trend` above. Revisit once Microsoft ships a stable,
  documented REST API for the portal's anomaly-detection feature.
- ~~Azure Advisor recommendations~~ / ~~Azure Policy compliance /
  Policy Insights~~ -- **already implemented**, just not by a Phase 1/2
  collector above: `app.operations.collectors.legacy_scan.advisor_findings`
  / `policy_compliance_findings` normalize
  `app.azure_data.get_advisor_recommendations` (high-impact-only) and
  `get_policy_compliance_summary`/`get_non_compliant_resources` into
  Findings, wired into `collect_legacy_envelopes`'s `legacy_advisor`/
  `legacy_policy_compliance` sources (see
  `docs/OPERATIONS_API.md`'s "Existing-scan adapter" section). Advisor's raw
  data function calls Microsoft.Advisor's ARM REST API directly (never
  the `azure-mgmt-advisor` SDK, which is not a dependency of this
  project). `get_policy_compliance_summary` never fabricates a
  compliance percentage from an inconsistent/zero `totalResources`
  denominator -- see `azure_data.compute_policy_compliance_pct`.
- **Per-vault Recovery Services Backup REST API** (`Backup Jobs`/
  `Backup Protected Items` under `Microsoft.RecoveryServices/vaults/...`)
  -- an alternative to the Log Analytics-based `azure_backup` collector
  that would work even when a vault has no diagnostic settings
  configured, at the cost of enumerating every vault and paginating
  per-vault. Worth adding as a second, independently-named backup source
  if Log Analytics coverage proves insufficient in practice.
- **Per-deployment Azure OpenAI/Cognitive Services quota** -- `capacity`
  currently reports subscription+region aggregate quota only; ARM does
  not expose a reliably-available per-deployment consumption endpoint
  at the time of writing (see `docs/EVIDENCE_MODEL.md`).
- **A generic `RESOURCE_GRAPH` catch-all source** -- the
  `EvidenceSource.RESOURCE_GRAPH` enum value is reserved for a possible
  future generic/ad-hoc Resource Graph collector, but every current
  Resource Graph-backed collector (`update_manager`, `retirement_advisories`,
  and telemetry's resource discovery) uses its own specific,
  domain-named source instead -- a generic catch-all source would blur
  which domain a Finding belongs to.
