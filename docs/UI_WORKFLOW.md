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

**Ops Council** (multi-agent chat/debate) and **The Crew** (agent bios) are
still fully functional but are reached as **secondary** views via the "More"
menu in the top nav — they never compete with the two primary views. This
preserves every existing chat/debate/demo-scenario/remediation/chaos/ADO
capability; none of it was removed, only re-organized so the executive/ops
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
voice (the profile's `orchestrator` persona — e.g. "Grid Coordinator" in the
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
| Tools & Guided Demo (secondary, collapsible) | Morning Briefing / chaos demo / demo scenarios / Compliance → ADO proposals / crew status |

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
   (simulated in Demo mode, real via PATCH in Live mode) to close the loop.

Chaos testing (💥 "Do Something Stupid"), the six pre-built Ops Council demo
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
  updates, workflow actions, and errors.
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
