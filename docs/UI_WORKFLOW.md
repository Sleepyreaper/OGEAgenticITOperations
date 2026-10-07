# Product UI — the two-view workflow

This documents `templates/index.html`'s information architecture and how it
maps to the deterministic Operations API (`docs/OPERATIONS_API.md`) and the
evidence-grounded agent analysis layer (`docs/AGENT_INTELLIGENCE.md`).

## Primary navigation: exactly two views

The top nav exposes exactly two **primary** tabs:

1. **Executive Brief** — a one-screen, low-cognitive-load status view for
   leadership. Backed entirely by `GET /api/operations/brief`.
2. **Operations Center** — the day-to-day working surface: a unified,
   priority-ranked findings queue, a shift handoff bar, and evidence-grounded
   AI analysis. Backed by `GET /api/operations/{snapshot,queue,handoff}`,
   `GET /api/operations/evidence/<id>`, `PATCH /api/operations/findings/<id>`,
   and `POST /api/operations/analyze`.

**Ops Council** (multi-agent chat/debate) and **Agent Squad** (what each agent does and how, from `/api/agents`) are
still fully functional but are reached as **secondary** views via the "More"
menu in the top nav — they never compete with the two primary views. This
preserves every existing chat/debate/demo-scenario/remediation/ADO
capability; none of it was removed (the old chaos button was later replaced by ZeroOps), only re-organized so the executive/ops
surfaces aren't cluttered with agent-persona theater.

## Executive Brief

| Section | Source | Honest missing state |
|---|---|---|
| One-sentence status + freshness/coverage | `brief.headline`, `brief.data_freshness`, `brief.source_coverage` | `overall_state == "unknown"` renders as "Insufficient source coverage" — never a fake green |
| Business Impact card | `brief.business_impact` | `0` shown explicitly when there are no active customer-impacting findings |
| Reliability / SLO card | `brief.reliability` | `slo_configured: false` → "SLOs not configured"; `state: "unknown"` (a source error) → "SLO state unknown (source error)" — never rendered as healthy |
| Capacity card | `brief.capacity` | Same not_configured/unknown handling as Reliability |
| What Changed / Decisions-Escalations / Attention Items | `brief.changes_since_yesterday` / `brief.decisions_required` / `brief.attention_items` | Each is capped at 3 items; an empty list renders its own honest "No changes/decisions/attention" text, not blank space |

Every item in these three lists links straight into the Operations Center's
finding detail drawer (`goToFindingInOps(id)`).

**One "Generate Executive Briefing / Explain" button** calls
`POST /api/operations/briefing` and renders a single synthesized coordinator
voice (the profile's `orchestrator` persona — e.g. "Operations Coordinator" in the
default `power` profile) in a modal. Specialist detail is collapsed behind a
`<details>` disclosure ("Supporting specialist analysis") — agents/personas
are otherwise entirely hidden on the executive surface, per the product
requirement that this view exposes one coordinator voice only on request.

**One "Open Operations Center" button** switches to the Operations Center.

In **Demo mode**, both the brief and the briefing modal are fed from the
centralized fixture (`GET /api/operations/demo`, `app/operations/demo_fixture.py`)
— never scattered hardcoded DOM values. The freshness line explicitly reads
"DEMO DATA (simulated, not live Azure)"; the briefing modal shows a
"SIMULATED (Demo)" badge. Live mode always shows "LIVE Azure" / a green
"LIVE" badge instead. Demo and Live are never visually ambiguous.

## Operations Center

| Section | Source |
|---|---|
| Shift handoff bar (collapsible, always visible when open) | `GET /api/operations/handoff` — open / new-since-prior / changed-since-prior / snoozed / pending approvals / source gaps |
| Current health & source coverage | `GET /api/operations/snapshot` → `coverage` |
| Capacity watch | `handoff.capacity_watch` |
| Recent changes (24h) | `handoff.recent_changes` |
| Deep Intelligence (findings-by-category chips) | `snapshot.summary.by_category` — clicking a chip filters the queue by that category |
| Unified priority queue (primary content) | `GET /api/operations/queue` — priority band + factors (`rank_reason`), severity/category, title/business impact, age, owner/status, evidence count, recommended action, approval flag; filterable by status/category/severity/owner with Load-more pagination |
| Agent Activity (recorded proof; see below) | `GET /api/activity`, `GET /api/activity/<id>` |
| Tools & Guided Demo (secondary, collapsible) | Morning Briefing / ZeroOps live-demo link / demo scenarios / Compliance → ADO proposals / configured agents (availability not checked) |

### Finding detail / evidence drawer

Clicking any queue row (or a linked item from the Executive Brief) opens a
drawer showing bounded evidence (`GET /api/operations/evidence/<id>` in Live
mode) and workflow controls:

- **Acknowledge**, **Start** — single click, `actor` only.
- **Assign** — requires a non-empty **owner** (enforced client-side before
  the PATCH is sent).
- **Snooze** — requires a future **snooze_until** date/time (enforced
  client-side; a past/invalid value is rejected before the request).
- **Resolve**, **Dismiss** — both require a non-empty **reason** (enforced
  client-side).

Every action calls `PATCH /api/operations/findings/<id>` in Live mode; the
API's own validation/state-machine errors (e.g. a 409 "cannot X from status
Y") are surfaced inline in the drawer, never swallowed. In **Demo mode**, the
same state machine is mirrored client-side (see `WORKFLOW_ACTION_RULES` in
`templates/index.html`) purely to demonstrate the flow — the change is never
persisted, and the drawer shows an explicit "Demo mode: this workflow change
is simulated locally and is not saved anywhere" notice.

### AI Analyze

The drawer's "🧠 AI Analyze" button calls `POST /api/operations/analyze` in
Live mode (or renders the fixture's pre-built `analysis_example` for the
one highlighted finding in Demo mode) and shows:

- **Routing explanation** — which specialist(s) were consulted, whether a
  coordinator/debate round ran, and the deterministic reason
  (`routing.factors.reason`).
- **Evidence citations** — valid vs. unsupported evidence ids.
- **Confidence** and **missing evidence** (when the model says the evidence
  doesn't fully support a conclusion).
- **Recommended actions**, each with its deterministic approval tier
  (`app/approval.py`) — e.g. `production_write` actions are always flagged
  `human_approval_required`.
- **Specialist debate details**, collapsed behind a `<details>` disclosure
  by default — visible to an ops engineer on request, never shown by default.

## Agent Activity (inside Operations Center)

A panel below the queue shows what the application **recorded**, so proof does
not require the Azure, Foundry or SRE portals. It reads only
`GET /api/activity` (list) and `GET /api/activity/<id>` (detail) and is
independent of the Demo/Live toggle. Nothing is inferred and no step, dialogue
or reasoning is ever synthesized; there is no raw-reasoning viewer.

**List** — one row per investigation: origin (analysis, briefing, MCP handoff,
fast detector, simulated fixture, API, SRE thread request), phase as text,
requested vs **actual** backend (from the latest run only), last observed time,
recorded counts, measured usage (tokens, model calls, duration, estimated cost
— a missing cost reads "unavailable", never a guess), whatever specialist /
tool / result fields the run usage carries (otherwise "not recorded in the list
summary"), schema/source gaps, failures, and the explicit
**"No execution or verification observed"** chip. Empty state: "No recorded
activity since activity capture was enabled." A failed load shows a bounded
error and is never rendered as the empty state.

**Detail** — opened with the row's *Open timeline* button (or the *View
recorded investigation* link on a ZeroOps escalation card that has an
`investigation_id`): a summary, runs (requested vs actual backend, a visible
"backend differs from request" note, usage), a "Failures, invalid output and
gaps" list, the ascending event timeline (`seq` order, `<ol>` of `<time>`
entries) and artifacts. Only returned events/artifacts are shown. Execution or
verification is reported only when a returned event/artifact carries
`executed`/`verified` provenance or an execution/verification receipt kind.

**Labels** (text, never colour alone):

| Provenance | Meaning shown to the operator |
|---|---|
| Observed | recorded by this application |
| Reported | stated by another system/model; not verified here |
| Configured | a setting only; not proof anything ran |
| Simulated | fixture/demo data, not a live event |
| Approved | a human approval was recorded; **does not execute a fix** |
| Executed / Verified (receipt) | shown only when such a receipt is returned |

Actor labels come from returned fields: *Deterministic collector/router*
(`system`/`orchestrator` routing), *Deterministic detector*, *Specialist ·
Foundry* or *Specialist · Azure OpenAI direct* (only from the recorded
event/run backend; a disagreement between the two is labelled as a conflict,
and a configured or requested backend is never shown as Foundry),
*MCP handoff* (`escalation_received` from MCP/SRE), *SRE thread request*
(`sre_thread_result`; a created thread is not triage or remediation),
*Human decision* (`decision_recorded` by an operator/reviewer) and *Simulated
fixture* (simulated provenance). Unknown vocabulary values are shown as the raw
value marked "unrecognized", never guessed. Phase `approved` reads "Approved —
not executed"; `closed_unverified` reads "Closed by operator — not verified".

**Polling** — the list refreshes about every 10 s, only while Operations Center
is visible and the tab is visible, one request at a time with an 8 s timeout and
exponential backoff (max 120 s). Five consecutive failures pause auto-refresh
(the *Refresh* button retries). An open, non-terminal timeline is refreshed in
the same tick using `event_after=<last seq>`. On any failure the last data stays
on screen with a bounded error. Updates reconcile by key, so focus, open
`<details>` and scroll position are preserved; the page never auto-scrolls.
Screen readers get one joined `aria-live` announcement per update (new
investigations, new events, phase change, failure/recovery). *Auto-refresh* can
be turned off.

## PUBLIC_DEMO_MODE

On load the page reads the boolean `config.public_demo_mode` from `GET
/api/health`. If it cannot be read the page **fails closed** (treated as public,
with a banner and a Retry button). In public mode:

- Only `/api/health`, `/api/operations/demo`, `/api/activity` and
  `/api/activity/<id>` are called (the server gate also enforces this).
  Subscriptions, scans, analysis, the Ops Council stream, digest, remediation,
  ADO inspection/proposals, and every `/api/zeroops/*` endpoint are never called.
- The Live Azure toggle and subscription selector are hidden; Council input,
  Morning Briefing, council demo scenarios and the compliance scan are disabled
  with an explanatory title; the ZeroOps view shows an explanation (no
  scenario/inject/hand-off/proposal/approve/decision controls) and points to
  Agent Activity.
- Trusted mode (flag false) keeps all existing controls. No key or secret is
  present in the page's JavaScript.

## Copy rules (configured is not connected, approved is not executed)

- The nav shows "Configured: N/M agent endpoints · availability not checked" and
  "Last observed activity", not "All crew online". Configured-deployment lists
  use neutral markers, not green "online" dots.
- ZeroOps: "SRE Agent endpoint configured (connectivity not checked)"; a created
  SRE thread is "SRE thread request accepted … triage is not observed"; *Record
  approval (does not execute)*; *Record operator closure (not verified)* (an
  operator closure is not SRE verification); statuses read "approved (not
  executed)" and "closed by operator (not verified)".
- The legacy Ops Council runs direct-model analysis and is not necessarily
  Foundry; the Agent Squad page labels its runtime text "Configured runtime
  (catalog, not proof of a run)". Foundry is asserted only from recorded run data
  in Agent Activity.
- Missing cost is "unavailable"; the Council no longer estimates a per-token cost
  when none was reported.

## Guided demo story

The intended walkthrough (Demo mode, no Azure required):

1. **Evidence arrives / change detected** — Operations Center → Recent
   Changes shows the Terraform apply that modified the NSG rule.
2. **Queue prioritizes** — the new SSH-exposure finding appears as the #1
   (P1) item in the priority queue, with its `rank_reason` explaining why.
3. **Grounded agent analysis cites evidence** — open the finding, click
   "AI Analyze"; the simulated analysis cites the finding's own evidence id
   and shows the routing decision that selected it.
4. **Recommendation** — the analysis's recommended actions, each tagged with
   its approval tier.
5. **Human approval** — use the drawer's Acknowledge/Assign/Resolve controls
   (simulated in Demo mode, real via PATCH in Live mode) to record the
   workflow. These record decisions only; they do not execute a fix or verify
   one.

Live break/fix demos moved to **ZeroOps** (inject → ⚡ detected in seconds →
SRE Agent or squad hand-off; see [ZEROOPS_SRE_AGENT.md](ZEROOPS_SRE_AGENT.md)). The six pre-built Ops Council demo
scenarios, the Morning Briefing digest, Terraform/CLI remediation generation,
and the Compliance → ADO proposal scan/approve/reject flow are all reachable
from the Operations Center's "Tools & Guided Demo" panel and route into the
Ops Council secondary view for the streamed multi-agent debate experience.

## Accessibility

- Primary tabs use `role="tab"`/`aria-selected`; the finding drawer and
  briefing modal use `role="dialog"`/`aria-modal="true"` with focus moved on
  open and restored on close.
- `Escape` closes the topmost open dialog/menu; click-outside closes the
  "More" menu and the subscription picker.
- An `aria-live="polite"` region (`#a11y-announcer`) announces brief/queue
  updates, workflow actions, and errors. Agent Activity announcements are
  buffered and joined so several updates in one poll are all heard.
- Agent Activity uses a semantic `<ul>` of investigations and an `<ol>` timeline
  with `<time datetime>`; every state is conveyed as text; disclosures are native
  `<details>`/`<summary>` (keyboard operable); *Open timeline* moves focus to the
  timeline heading and *Close timeline* restores focus to the opener; polling
  never moves focus or scroll.
- `prefers-reduced-motion: reduce` disables all CSS animations/transitions.
- Layouts use responsive Tailwind grid breakpoints (`sm:`/`md:`/`lg:`)
  rather than fixed multi-column grids, so the queue, cards, and the Ops
  Council chat/agent-panel layout collapse to a single column on mobile.

## UX assumptions worth calling out

- There is no user-auth/session system in this app today, so PATCH workflow
  actions in Live mode send a fixed `actor: "ops-user"` string (matching the
  existing `ado_integration.py` convention of a default `approved_by`
  actor) rather than a signed-in identity.
- The demo fixture's `analysis_example` only pre-builds a full simulated
  analysis for one highlighted (P1) finding; AI Analyze on any other demo
  finding shows an explicit "not available in Demo mode for this item"
  message rather than fabricating a second narrative.
- Live "Load more" pagination calls the server with `page`/`page_size`;
  Demo mode simulates the same UX by paging the fixture's already-complete
  local array client-side (never a second network round trip, since the
  fixture is a single bounded payload).
