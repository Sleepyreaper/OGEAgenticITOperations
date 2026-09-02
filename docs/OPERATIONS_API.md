# Operations API (`/api/operations/*`)

The product-facing, deterministic service/API layer built on top of
`app/operations/`'s evidence collectors (see `docs/EVIDENCE_MODEL.md` and
`docs/AZURE_DATA_SOURCES.md`). Nothing here calls an LLM: every field is
either a direct platform fact, a deterministic computation over one, or
an explicit `not_configured`/`unknown`/`error` state — never a fabricated
default. This doc covers the snapshot/brief/queue/workflow-state/handoff
schemas, the finding workflow-state machine, source partial/error
semantics, and worked request/response examples.

## Package layout (product-facing layer)

```
app/operations/
  collectors/legacy_scan.py  Adapter: app/azure_data.py's existing scan
                             signals -> Findings (see "Existing-scan
                             adapter" below).
  cache.py                   SnapshotCache -- thread-safe TTL cache.
  state.py                   OperationsStateStore -- SQLite finding
                             workflow-state (status/owner/snooze/audit).
  snapshot.py                get_snapshot() -- the one bounded, cached,
                             deduplicated, prioritized entry point.
  brief.py                   build_brief() -- executive brief.
  queue.py                   build_queue() -- filtered/paginated/ranked
                             queue.
  handoff.py                 build_handoff()/persist_handoff() --
                             structured shift handoff.
  routes.py                  Flask blueprint mounted at /api/operations,
                             registered from app/main.py.
```

## Existing-scan adapter (`app/operations/collectors/legacy_scan.py`)

Converts `app/azure_data.py`'s existing scan signals into the same
`Finding`/`EvidenceReference` model Phase 1/2 collectors use, as 8
independent `CollectionEnvelope`-shaped sources:

| Source | Signal | Category | Notes |
|---|---|---|---|
| `legacy_resource_health` | Degraded/Unavailable Resource Health | reliability | `app.azure_data.get_resource_health_statuses` |
| `legacy_service_health` | Active Service Health incidents | incident | `get_service_health_events`; skips `eventType == 'HealthAdvisory'` (Phase 2's `retirement_advisories` owns those — see Dedup below) |
| `legacy_security_drift` | Dangerous inbound NSG rules | security | `detect_security_drift` |
| `legacy_insecure_storage` | Public blob access | security | `detect_insecure_storage` |
| `legacy_advisor` | High-impact Azure Advisor recommendations only | varies (mapped from Advisor's own category) | `get_advisor_recommendations` |
| `legacy_policy_compliance` | Azure Policy non-compliance summary + items | compliance | `get_policy_compliance_summary` + `get_non_compliant_resources` |
| `legacy_resource_hygiene` | Orphaned disks/NSGs, idle App Service Plans, empty subnets | cost | `get_orphaned_disks` + `get_deep_analysis` |
| `legacy_ownership` | Resource groups missing a `support-owner` tag | ownership | `get_tagging_compliance` |

**Authorized VM stop/deallocate detection**: `legacy_resource_health`
downgrades an "Unavailable" VM to informational severity (no
`executive_attention`/customer impact) when Resource Health's
`reasonType` is Azure's documented `UserInitiated` value. Some tenants
have been observed reporting `reasonType` blank/absent on a genuinely
authorized stop/deallocate -- as a narrow fallback, an EXACT
(case-insensitive) match of `title`/`summary` against Azure's own
published authorized-stop wording ("Stopped and deallocated" / "This
virtual machine is stopped and deallocated as requested by an
authorized user or process.") is used ONLY when `reasonType` is absent
-- see `legacy_scan.resource_health_findings` for the exact phrases.
This never broadens to "any Unavailable status that mentions being
stopped".

**Dedup**: `app.azure_data.get_service_health_events` and Phase 2's
`app.operations.collectors.advisories.collect_retirement_advisories` both
read the same underlying `Microsoft.ResourceHealth` event feed.
`legacy_service_health` deliberately excludes `HealthAdvisory` events so
the two never double-report the same retirement/deprecation notice. As a
second, general-purpose safety net, `app.operations.snapshot._dedupe_findings`
merges by `Finding.id` regardless of source — any other coincidental ID
collision is merged (see "Deduplication" below), not duplicated.

A failure fetching one of the 8 sources (e.g. a transient ARM/Resource
Graph error) is caught and turned into that source's own `error`
envelope — it never blanks out the other 7, mirroring
`app.operations.service`'s per-source isolation for Phase 1/2.

## Operations snapshot (`app/operations/snapshot.py`)

`get_snapshot(subscription_ids, *, force_refresh=False, ...)` is the one
entry point that combines:

1. Phase 1 + Phase 2 (`app.operations.service.run_full_collection` — 14
   sources), and
2. the legacy-scan adapter (8 sources, above)

into one `OperationsSnapshot`:

```jsonc
{
  "id": "snap-<16 hex chars>",
  "generated_at": "2025-01-01T00:00:00.000Z",
  "subscription_ids": ["<normalized subscription id>", "..."],
  "status": "ok" | "partial" | "error",
  "envelopes": [ /* CollectionEnvelope.to_dict() x 22 */ ],
  "findings": [
    {
      "finding": { /* Finding.to_dict() -- see docs/EVIDENCE_MODEL.md */ },
      "workflow": {
        "status": "new", "assigned_owner": "", "disposition_reason": "",
        "snooze_until": null, "first_seen_at": null, "created_at": null, "updated_at": null
      },
      "priority": { "band": "P1", "factors": { "customer_impact": true, "severity_rank": 0, "slo_state": null, "slo_state_rank": 2, "age_hours": 1.2, "confidence_rank": 0 } }
    }
  ],
  "coverage": { "total_sources": 22, "ok_count": 20, "error_count": 1, "not_configured_count": 1, "not_supported_count": 0, "sources_by_status": { "...": ["..."] } },
  "source_errors": [ { "source": "cost_management_trend", "status": "error", "error": "..." } ],
  "summary": {
    "total_findings": 12, "by_severity": {"high": 3, "...": 0}, "by_category": {"security": 2, "...": 0},
    "by_workflow_status": {"new": 10, "acknowledged": 2}, "executive_attention_count": 3,
    "approval_required_count": 2, "source_coverage": { /* same shape as coverage above */ }
  }
}
```

`findings` is already deduplicated (by deterministic `Finding.id` — see
below) and priority-ordered (see `app/operations/priority.py`: customer
impact, breached/at-risk SLO, severity, confidence, then age — exposed
via `priority.factors`, never an opaque score).

### Deduplication

Findings sharing the same deterministic `id` (same category + source +
resource_id + discriminator — see `app/operations/identifiers.py`) are
merged into one: earliest `first_seen`, latest `last_seen`, the more
severe `severity`, the union of `approval_required`/
`executive_attention`, max affected-resource/workload counts, merged
`metadata`, and a de-duplicated, bounded (max 10) `evidence` list.

### Caching

Keyed by the **normalized** subscription set (trimmed, lowercased,
de-duplicated, sorted — see `app.operations.cache.normalize_subscription_key`),
so `?subs=B,A` and `?subs=a,b` share one cache entry. TTL defaults to 60s
(`OPERATIONS_SNAPSHOT_CACHE_TTL_SECONDS`); `?refresh=true` forces a
rebuild. A cached snapshot always carries its own truthful
`status`/`source_errors` — **an error is never cached as a successful
empty result**.

### Capacity locations

`app/operations/routes.py` builds one `OperationsConfig.from_env()` per
request and forwards `OperationsConfig.capacity_locations` (the
`CAPACITY_LOCATIONS` env var — a comma-separated, validated list of ARM
region slugs, e.g. `eastus2,westeurope`; see `app/operations/config.py`)
as `run_full_collection`'s `locations` **and** `openai_locations`
kwargs, for every route that builds a snapshot (`/snapshot`, `/brief`,
`/queue`, `GET /handoff`, `POST /handoff`, `/evidence/<id>`). There is
no `?locations=` query-string override — an operator sets
`CAPACITY_LOCATIONS` once, and every route immediately gets capacity
coverage for those regions. Leaving `CAPACITY_LOCATIONS` unset is a
valid, safe default: the `capacity` source reports `not_configured`,
exactly as it does when calling `run_full_collection` directly with no
`locations`. Because `CAPACITY_LOCATIONS` is process-static (read once
from the environment, not per-request), it never changes which
snapshot the subscription-keyed cache above returns.

### Capacity name filters

`OperationsConfig.openai_capacity_name_filters` (the
`OPENAI_CAPACITY_NAME_FILTERS` env var — a comma-separated,
case-insensitive substring allowlist) is forwarded the same way, via
`app.operations.service.collect_capacity_envelope`, straight into
`app.operations.collectors.capacity.collect_openai_capacity`'s
`name_filters` — no separate route wiring needed, since it travels on
the same `OperationsConfig` instance every snapshot-building route
already builds per request. It narrows the Azure OpenAI/Cognitive
Services quotas the `capacity` source reports on (e.g. `gpt-5.6`) so a
shared Cognitive Services account's unrelated, always-fully-allocated
model quotas never dominate the capacity executive summary. Unset (the
default) means no filtering — every quota is kept, the correct default
for a dedicated/generic deployment. `Microsoft.Compute` usages are
never filtered by this setting.

### Snapshot status semantics

- `ok` — every *applicable* source (one that actually attempted to
  collect: `ok` or `error`) succeeded. Sources that are legitimately
  `not_configured`/`not_supported` (e.g. no SLOs defined) don't count as
  failures.
- `partial` — at least one applicable source failed, but not all of
  them.
- `error` — **every** applicable source failed. This is the "all core
  sources failed" case the product spec calls out: a snapshot never
  reports `ok`/all-clear when it collected nothing trustworthy.

## Executive brief (`app/operations/brief.py`)

`build_brief(snapshot)`:

```jsonc
{
  "overall_state": "healthy" | "attention" | "impact" | "unknown",
  "headline": "1 active customer-impacting issue(s); top: <finding title>.",
  "updated_at": "2025-01-01T00:00:00.000Z",
  "data_freshness": { "snapshot_generated_at": "...", "age_seconds": 12.3 },
  "business_impact": {
    "active_customer_impacting_count": 1,
    "details": [ /* up to 3 bounded finding summaries, evidence stripped of resource_id */ ]
  },
  "reliability": {
    "slo_configured": true, "state": "healthy" | "at_risk" | "breached" | "not_configured" | "unknown",
    "error_budget_remaining_pct": 87.5, "workloads": [ { "workload": "...", "state": "...", "criticality": "..." } ]
  },
  "capacity": {
    "configured": true, "state": "healthy" | "warning" | "critical" | "not_configured" | "unknown",
    "minimum_headroom_pct": 8.2, "nearest_constraint": "compute:eastus/cores",
    "forecast": { "resource_scope": "...", "metric": "...", "exhaustion_at": "..." } | null
  },
  "changes_since_yesterday": [ /* up to 3, last 24h of Activity Log change Findings */ ],
  "decisions_required": [ /* up to 3, open (non-resolved/dismissed/snoozed), approval_required == true AND
    (executive_attention == true OR severity in {critical, high} OR metadata.decision_required in
    {true, 'blocked', 'cost_commitment'}) -- see "Decisions required" below */ ],
  "attention_items": [ /* up to 3, executive_attention == true, open */ ],
  "source_coverage": { /* same shape as OperationsSnapshot.coverage */ },
  "snapshot_id": "snap-..."
}
```

**Overall state** (deterministic; assembled only from the fields above,
never an opaque composite score):

- `unknown` — the underlying snapshot's `status == "error"` (every
  applicable source failed) — the brief cannot make a truthful claim.
- `impact` — `business_impact.active_customer_impacting_count > 0`: an
  open incident/reliability Finding with High/Critical severity or
  `executive_attention`.
- `attention` — any open `executive_attention` Finding, any open
  Finding qualifying for `decisions_required` (see above -- an
  approval-required Finding alone is NOT enough; human approval does
  not equal executive attention), capacity `critical`/`unknown`, or
  reliability `breached`/`unknown`, or `source_coverage.error_count > 0`
  (at least one applicable source -- any source, not just
  capacity/reliability -- failed to collect; `status == "partial"`).
  When this is the ONLY attention trigger (no concrete `attention_items`
  to lead with), the headline honestly names the incomplete coverage
  (e.g. "Evidence coverage is incomplete: 1 source(s) failed to collect
  (defender_alerts) -- operational health cannot be fully confirmed.")
  instead of a generic "all monitored sources report healthy".
- `healthy` — none of the above.
  **`healthy` is never reported when `source_coverage.error_count > 0`**
  -- a snapshot with any source error is, at minimum, `attention`.

**No composite readiness score, no fake uptime, no fabricated revenue at
risk, no static MTTR.** If SLOs or capacity aren't configured, `reliability`/
`capacity` say `"not_configured"` explicitly; if the underlying source
errored, they say `"unknown"` — never a guessed number.

### Decisions required

`decisions_required` is deliberately STRICTER than plain
`approval_required` — human approval does not equal executive
attention. A routine, low/medium-severity operational approval (e.g.
deleting an orphaned disk after a confirmation window, or a Medium-
severity policy-compliance fix) must never crowd this CIO-facing list.
An open, `approval_required` Finding is only surfaced here when it is
ALSO at least one of:

- explicitly `executive_attention`,
- Critical/High severity, or
- explicitly flagged via `metadata.decision_required` as
  `true`/`"blocked"`/`"cost_commitment"` — an escalation/cost-commitment
  marker a collector or workflow step can set even for a Medium/Low-
  severity Finding.

Every approval-required Finding — including the routine ones excluded
here — remains fully visible and filterable in the unified queue
(`/api/operations/queue`, which applies no such filter) and in
`GET /api/operations/handoff`'s `pending_approvals` (also unfiltered).
This tightening only changes what the EXECUTIVE brief leads with, never
what Ops can see/action.

**Never exposes a subscription id, endpoint, token, or credential**: the
evidence entries embedded in `business_impact`/`decisions_required`/
`attention_items` have their `resource_id` (an ARM id whose first path
segment is the subscription GUID) stripped — see
`app.operations.brief._sanitize_evidence`.

## Unified queue (`app/operations/queue.py`)

`build_queue(snapshot.findings, *, status=None, category=None, severity=None, owner=None, page=1, page_size=25)`:

```jsonc
{
  "items": [
    {
      "id": "sec-...", "rank": 1, "rank_of": 12,
      "rank_reason": "customer-impacting; severity_rank=0; confidence_rank=0; age=1.2h",
      "priority_band": "P1", "priority_factors": { "...": "..." },
      "title": "...", "category": "security", "severity": "critical", "confidence": "confirmed",
      "first_seen": "...", "last_seen": "...", "age_hours": 1.2,
      "business_impact": "...", "recommended_action": "...",
      "approval_required": true, "executive_attention": true,
      "evidence_count": 2, "evidence": [ /* up to 5 */ ],
      "workflow_status": "new", "assigned_owner": "", "disposition_reason": "", "snooze_until": null, "workflow_updated_at": null
    }
  ],
  "total": 12, "page": 1, "page_size": 25, "total_pages": 1
}
```

**Ordering** is exactly `app.operations.priority.prioritize_findings`'s
order (customer impact > breached/fast-burning SLO > severity > capacity
severity [folded into severity via each collector's own threshold
mapping] > confidence > age) — `queue.py` filters/paginates that
already-ranked list; it never re-derives its own order. `rank`/`rank_of`
reflect position within the **filtered** (not paginated) set, and
`rank_reason` is assembled only from the same `priority_factors` every
item already carries.

Filters: `status` (a workflow status OR a `Finding.status` value —
distinct vocabularies, see below), `category` (`FindingCategory`),
`severity` (`Severity`), `owner` (matches `assigned_owner` or the
Finding's own `owner` field, case-insensitive). An unrecognized filter
value or out-of-range page/page_size raises (400), never silently
ignored.

## Finding workflow state (`app/operations/state.py`)

A SQLite-backed store (stdlib `sqlite3`, no external dependency), path
configurable via `OPERATIONS_STATE_DB` (default `operations_state.db`
locally; set to `/home/data/operations.db` on Azure App Service Linux —
`/home` is the only path persisted across restarts/scale events on that
platform).

**Workflow status vocabulary** (`new` / `acknowledged` / `in_progress` /
`resolved` / `dismissed` / `snoozed`) is **deliberately distinct** from
`Finding.status` (`open` / `acknowledged` / `mitigating` / `resolved` /
`suppressed`, `app.operations.models.FindingStatus`): the latter is the
evidence's own platform-reported state (set by a collector at
construction time); the former is the human triage state of that
Finding in this ops tool.

### State machine

| Action | Allowed from | Result status | Required fields |
|---|---|---|---|
| `acknowledge` | `new` | `acknowledged` | `actor` |
| `start` | `new`, `acknowledged` | `in_progress` | `actor` |
| `resolve` | `new`, `acknowledged`, `in_progress`, `snoozed` | `resolved` | `actor`; `reason` recommended |
| `dismiss` | `new`, `acknowledged`, `in_progress`, `snoozed` | `dismissed` | `actor`; `reason` recommended |
| `snooze` | `new`, `acknowledged` | `snoozed` | `actor`, `snooze_until` (future ISO-8601) |
| `assign` | `new`, `acknowledged`, `in_progress`, `snoozed` | *(unchanged)* | `actor`, `owner` |

A finding with no persisted row yet is implicitly `new`. Every
transition not listed as allowed from the finding's current status
raises `OperationsStateError` (HTTP 409 at the route layer) — there is
no silent no-op.

`snooze` is only allowed from `new`/`acknowledged` (not `in_progress`)
so an **expired snooze always reverts to an unambiguous status**: `new`
or `acknowledged`, whichever it was before snoozing. Expiry is resolved
lazily and atomically on the next read (`get_state`/`get_states`) or
write (`apply_action`) touching that finding: past `snooze_until` ->
persist the revert + an `auto_unsnooze` audit row, then proceed.

### Persistence and concurrency

Every mutation runs inside one explicit `BEGIN IMMEDIATE ... COMMIT`
transaction (WAL journal mode, 30s `busy_timeout`) — atomic, and safe
across Flask's multi-threaded request handling and multiple Gunicorn
worker processes sharing the same on-disk file. No SQL string
interpolation anywhere — every query uses `?` placeholders.

A full audit trail (`action`, `from_status`, `to_status`, `actor`,
`reason`, `occurred_at`) is kept per finding
(`OperationsStateStore.get_audit_history`).

## Shift handoff (`app/operations/handoff.py`)

`build_handoff(snapshot, state_store=...)`:

```jsonc
{
  "generated_at": "...", "snapshot_id": "snap-...", "prior_handoff_at": "..." | null,
  "open_item_count": 9,
  "open_items": [ /* up to 25, workflow_status not in resolved/dismissed/snoozed */ ],
  "new_since_prior": [ /* open items whose Finding.first_seen > prior handoff's created_at */ ],
  "changed_since_prior": [ /* open items whose workflow.updated_at > prior handoff's created_at, excluding new_since_prior */ ],
  "snoozed_items": [ /* workflow_status == snoozed, with snooze_until + disposition_reason */ ],
  "capacity_watch": [ /* capacity summaries at warning/critical, nearest headroom first */ ],
  "pending_approvals": [ /* open items with approval_required == true */ ],
  "recent_changes": [ /* last 24h of Activity Log change Findings */ ],
  "source_gaps": [ { "source": "...", "status": "error" | "not_configured" | "not_supported" } ],
  "content_hash": "<32 hex chars>"
}
```

"new/changed since the prior handoff" is always recomputed **live**
against the prior handoff's persisted timestamp — nothing about a prior
handoff's Findings/evidence is stored, only its timestamp, actor, an
integrity hash of the payload above, and the bounded list of open
Finding IDs (`OperationsStateStore.record_handoff`). **No raw
secrets/evidence dumps are ever persisted** for a handoff.

`persist_handoff(handoff, state_store=..., created_by=...)` records that
bounded marker; `POST /api/operations/handoff` builds and persists in
one call.

## Routes (`app/operations/routes.py`, mounted at `/api/operations`)

All routes preserve every existing API in `app/main.py` unchanged.
Every route below except `/demo` builds its snapshot with capacity
coverage for `CAPACITY_LOCATIONS` (see "Capacity locations" above) —
there is no `?locations=` query-string equivalent.

| Method | Path | Notes |
|---|---|---|
| GET | `/snapshot` | `?subs=all\|id1,id2\|<unset>` (same semantics as existing routes), `?refresh=true` |
| GET | `/brief` | same query params as `/snapshot` |
| GET | `/queue` | + `?status=&category=&severity=&owner=&page=&page_size=` |
| PATCH | `/findings/<id>` | body: `{"action": ..., "actor": ..., ...}` — see state machine above |
| GET | `/handoff` | builds (does not persist) |
| POST | `/handoff` | body: `{"created_by": <required>, "subs": [...] (optional)}` — builds and persists |
| GET | `/evidence/<finding_id>` | bounded evidence metadata only (id/title/category/severity/source/confidence/evidence); 404 if not in the resolved snapshot |
| GET | `/demo` | the ONE centralized Demo-mode fixture for the product UI (see "Demo fixture" below) — ignores `?subs=`/`?refresh=` (there is no subscription to select in Demo mode) |

## Demo fixture (`app/operations/demo_fixture.py`, `GET /api/operations/demo`)

`build_demo_payload()` returns `{"meta", "snapshot", "brief", "queue", "handoff",
"analysis_example", "briefing_example"}` — the exact same `brief`/`queue`/`handoff`
schemas the routes above return for Live mode, so `templates/index.html` renders
Demo and Live data with the same JS functions (never scattered hardcoded fake
values in the frontend).

This stays honest rather than being hand-faked JSON: every field is produced
by feeding hand-authored, schema-valid `Finding`/`EvidenceReference`/
`SLOSummary`/`CapacitySummary` objects through the exact same deterministic
pipeline real Azure evidence goes through (`prioritize_findings`,
`build_brief`, `build_queue`, `build_handoff` — including a real, disposable
`OperationsStateStore` seeded with a scripted shift history and one prior
handoff, so "new/changed since prior handoff" is computed live, never
faked). The fixture deliberately seeds one `error` source and one
`not_configured` source, so even Demo mode shows a truthful `"partial"`
snapshot status and non-empty `source_gaps` — never an all-green fake.

The ONLY fabricated content anywhere in the fixture is a handful of
narrative strings a real model call would otherwise produce, confined to
`analysis_example`/`briefing_example` and marked `"simulated": true` in the
response — everything else (routing decision, evidence bundle, citation
validation, approval-tier classification, evaluation metrics) is computed by
the real `app.agents.routing`/`app.agents.evidence`/`app.agents.evaluation`/
`app.approval` modules, not guessed.

### Error responses

- `400` — bad input (missing/invalid subs, malformed PATCH body, an
  unrecognized queue filter, an invalid page/page_size).
- `409` — a disallowed workflow transition (`OperationsStateError`).
- `502` — an operations-layer collection/config failure
  (`OperationsCollectionError`/`OperationsConfigError`) surfaced with its
  message (never a raw stack trace to the client; the server still logs
  the traceback).
- `500` — an unexpected failure at the route boundary.

None of these responses include a raw Azure token, connection string, or
credential. `/brief` additionally never includes a subscription id or
endpoint (see "Never exposes..." above); other routes follow this app's
existing convention of surfacing subscription/resource ids for engineers
who already have Azure Reader access (matching `/api/scan/overview`).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OPERATIONS_SNAPSHOT_CACHE_TTL_SECONDS` | `60` | Snapshot cache TTL (seconds); must be positive. |
| `OPERATIONS_STATE_DB` | `operations_state.db` | SQLite path for finding workflow state; `/home/data/operations.db` recommended on Azure App Service Linux. |

Both are also exposed via `infra/main.bicep`/`infra/modules/web-app.bicep`'s
`operationsSettings` object (`operationsSnapshotCacheTtlSeconds`,
`operationsStateDbPath`).

## Worked examples

**All sources healthy, one High-severity NSG drift finding:**

```
GET /api/operations/brief?subs=00000000-0000-0000-0000-000000000000
```
```jsonc
{ "overall_state": "attention", "headline": "1 item(s) need attention; top: NSG 'nsg-web' allows inbound SSH from any source.", "...": "..." }
```

**No SLOs configured, capacity not supplied:**

```jsonc
{ "reliability": { "slo_configured": false, "state": "not_configured", "error_budget_remaining_pct": null, "workloads": [] },
  "capacity": { "configured": false, "state": "not_configured", "minimum_headroom_pct": null, "nearest_constraint": null, "forecast": null } }
```

**Every applicable source failed (e.g. a widespread auth outage):**

```jsonc
{ "overall_state": "unknown", "headline": "Insufficient source coverage right now to determine operational health.", "...": "..." }
```
(and the underlying `OperationsSnapshot.status == "error"`, never `"ok"`.)

**Acknowledging a finding, then trying to acknowledge it again:**

```
PATCH /api/operations/findings/sec-0b4ea1e73317a57a
{"action": "acknowledge", "actor": "alice"}
-> 200 {"finding_id": "...", "status": "acknowledged", ...}

PATCH /api/operations/findings/sec-0b4ea1e73317a57a
{"action": "acknowledge", "actor": "alice"}
-> 409 {"error": "cannot 'acknowledge' finding '...' from status 'acknowledged'; allowed from ['new']"}
```
