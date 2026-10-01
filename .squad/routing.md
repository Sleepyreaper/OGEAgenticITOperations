# Work Routing

How to decide who handles what.

## Routing Table

| Work Type | Route To | Examples |
|-----------|----------|----------|
| Triage, architecture, design review | Lead | "should agents get a KQL tool?", routing/approval policy, PR review |
| Evidence sources & findings | Evidence Engineer | new collector, finding category, tool in `app/agents/tools.py`, brief/queue/handoff |
| Models, Foundry, tokens | Foundry Engineer | backend changes, new specialist agent, model tier, prompt version, token dashboard, Foundry Bicep/Terraform |
| Tests & evals | Tester | regression test, fake backend, eval golden set, coverage review |
| Decisions & session logs | Scribe | merge `.squad/decisions/inbox/` (always background) |
| Backlog monitoring | Ralph | scheduled triage of open work items |

Preset installation adds concrete routes for the configured team. Add or edit rows
here only when their agent names also exist in the casting registry.

## Repo Notes

- CI/CD runs on **Azure DevOps** (`pipelines/`), not GitHub Actions — Squad was initialized with `--no-workflows`; do not add `.github/workflows/`.
- One logical change per commit/PR; the Lead approves before merge.

## Issue Routing

| Label | Action | Who |
|-------|--------|-----|
| `squad` | Triage: analyze issue, assign `squad:{member}` label | Lead |
| `squad:{name}` | Pick up issue and complete the work | Named member |

### How Issue Assignment Works

1. When a GitHub issue gets the `squad` label, the **Lead** triages it — analyzing content, assigning the right `squad:{member}` label, and commenting with triage notes.
2. When a `squad:{member}` label is applied, that member picks up the issue in their next session.
3. Members can reassign by removing their label and adding another member's label.
4. The `squad` label is the "inbox" — untriaged issues waiting for Lead review.

## Rules

1. **Eager by default** — spawn all agents who could usefully start work, including anticipatory downstream work.
2. **Scribe always runs** after substantial work, always as `mode: "background"`. Never blocks.
3. **Quick facts → coordinator answers directly.** Don't spawn an agent for "what port does the server run on?"
4. **When two agents could handle it**, pick the one whose domain is the primary concern.
5. **"Team, ..." → fan-out.** Spawn all relevant agents in parallel as `mode: "background"`.
6. **Anticipate downstream work.** If a feature is being built, spawn the tester to write test cases from requirements simultaneously.
7. **Issue-labeled work** — when a `squad:{member}` label is applied to an issue, route to that member. The Lead handles all `squad` (base label) triage.
