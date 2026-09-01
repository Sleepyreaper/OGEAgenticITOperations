# Branding & Customization Guide

This repo is a **reusable, white-label multi-agent AI operations platform** (MIT
licensed). Branding, agent identity, system prompts, and per-agent model routing
are all controlled by a **profile** — a checked-in directory under `profiles/<id>/`
— plus optional environment variable overrides. No code changes are required to
rebrand it for a new customer.

Two profiles ship today:

| Profile | Purpose |
|---|---|
| `oge` (default) | This app's original branding and agent personas (Microsoft Oil, Gas & Energy). `APP_PROFILE` defaults to this — existing deployments are unaffected. |
| `generic` | A neutral starting point (no industry flavor) — clone this for a new customer. |

## Quick Start — Rebrand for a new customer

**Recommended**: run the setup wizard, which clones `profiles/generic/` for you:

```bash
python3 scripts/configure.py
# → answer "yes" to "Create a NEW profile", or pass --new-profile <id> --clone-from generic
```

This creates `profiles/<id>/` with your app name/customer/industry filled in, and
tells you which files to edit next (see below). Commit `profiles/<id>/` to your
fork like any other source file — it's not git-ignored (only the generated
`.env`/`infra/main.bicepparam` deployment artifacts are).

**Manual alternative**: `cp -r profiles/generic profiles/<id>` and edit
`profiles/<id>/profile.json` by hand.

Then select it: set `APP_PROFILE=<id>` (env var / `.env`) or `appProfile: '<id>'`
(`infra/main.bicepparam`).

## Anatomy of a profile

```
profiles/<id>/
├── profile.json           # branding metadata + per-agent config (required)
└── prompts/
    ├── orchestrator.txt          # one system prompt file per agent
    ├── cost_sentinel.txt
    ├── standards_architect.txt
    ├── diagnostics_sre.txt
    ├── scout.txt
    └── compliance_inspector.txt
```

`profile.json` is validated strictly at startup — an unknown/missing key, wrong
type, or missing prompt file raises a clear error immediately rather than
silently falling back to a guess. Example structure (see `profiles/generic/profile.json`
for a full, valid reference):

```json
{
  "id": "contoso",
  "brand": {
    "app_name": "Contoso Ops",
    "app_title": "Contoso Ops — Multi-Agent Cloud Operations Intelligence",
    "tagline_line1": "RETAIL",
    "tagline_line2": "Contoso · Ops Council",
    "executive_subtitle": "Contoso · Retail",
    "logo_path": "/static/contoso-logo.svg",
    "logo_alt": "Contoso Ops",
    "customer": "Contoso",
    "industry": "Retail"
  },
  "agents": {
    "orchestrator": {
      "name": "Orchestrator",
      "role": "Routes requests, synthesizes answers",
      "deployment": "foundry-gpt",
      "endpoint_ref": "",
      "temperature": 0.7,
      "supports_temperature": false,
      "prompt_file": "prompts/orchestrator.txt"
    },
    "cost_sentinel": { "...": "..." },
    "standards_architect": { "...": "..." },
    "diagnostics_sre": { "...": "..." },
    "scout": { "...": "..." },
    "compliance_inspector": { "...": "..." }
  }
}
```

`agents` must contain exactly these six keys — `orchestrator`, `cost_sentinel`,
`standards_architect`, `diagnostics_sre`, `scout`, `compliance_inspector` — since
the multi-agent debate logic (`app/agents/runner.py`) and demo scenarios
(`app/agents/demos.py`) are wired to these keys specifically. Renaming, re-rolling
prompts, and re-pointing models for each is fully supported; changing the *set*
of agents is a larger change than this config layer covers.

### `brand` fields (all required except `customer`/`industry`)

| Field | Where it's used |
|---|---|
| `app_name` | Chat welcome message, "Welcome to the {app_name}" |
| `app_title` | `<title>` tag |
| `tagline_line1` | Nav top small-caps line |
| `tagline_line2` | Nav subtitle line |
| `executive_subtitle` | Executive/Reliability view header subtitle |
| `logo_path` | Nav logo `<img src>` (web-relative, e.g. `/static/your-logo.svg`) |
| `logo_alt` | Nav logo `<img alt>` |
| `customer` / `industry` | Optional — for your own docs/derivation; not directly rendered |

> **Scope note**: the sections above (title, nav header, chat welcome greeting,
> and the loaded agents' display names) are profile-driven. The "The Crew" tab's
> detailed personality bios (`templates/index.html`) are still static, oge-flavored
> HTML — editing those for a new profile is a manual template edit, not yet
> data-driven. This was a deliberate scope decision to avoid a risky rewrite of
> the ~100KB template in one pass.

### `agents.<key>` fields

| Field | Required | Notes |
|---|---|---|
| `name` | Yes | Display name shown in the UI and API responses |
| `role` | Yes | One-line description shown in the UI |
| `deployment` | Yes | Azure OpenAI deployment name |
| `prompt_file` | Yes | Path to the system prompt, relative to the profile directory |
| `endpoint_ref` | No (default `""`) | `""`/`"primary"` = default endpoint, `"secondary"` or any `AZURE_OPENAI_ENDPOINT_<NAME>`, or a literal `https://` URL |
| `temperature` | No (default `1.0`) | Only sent if `supports_temperature` is `true` |
| `supports_temperature` | No (default `false`) | Some deployments (o-series/GPT-5-style reasoning models) reject any temperature but the default — leave `false` unless you've confirmed your model accepts it |
| `api_version` | No (default: the app-wide default) | Per-agent Azure OpenAI API version override |

## Overriding without forking a profile

Every `agents.<key>` field (plus `name`/`role`) can be overridden per-deployment
via environment variables, without touching the profile files at all — useful
for routing one agent to a different model/endpoint temporarily, or for a
customer who wants to keep a profile's identity but swap a model:

```bash
AGENT_COST_SENTINEL_DEPLOYMENT=foundry-reasoning
AGENT_COST_SENTINEL_ENDPOINT=secondary
AGENT_COST_SENTINEL_SUPPORTS_TEMPERATURE=true
AGENT_COST_SENTINEL_TEMPERATURE=0.2
```

`KEY` is the agent key upper-cased (`ORCHESTRATOR`, `COST_SENTINEL`,
`STANDARDS_ARCHITECT`, `DIAGNOSTICS_SRE`, `SCOUT`, `COMPLIANCE_INSPECTOR`). The
same fields are available as an `agentOverrides` Bicep parameter for production
deployments — see [DEPLOYMENT.md](DEPLOYMENT.md).

## Editing prompts

Each agent's full system prompt lives in its own plain-text file
(`profiles/<id>/prompts/<agent_key>.txt`) instead of a large JSON string — open
it in any editor, change the personality/rules/tone, save, and restart the app
(or redeploy). No JSON escaping required. `AGENT_<KEY>_PROMPT_FILE=<path>` can
also point at a prompt file outside the profile directory if you want to swap a
single agent's prompt without editing the profile.

## Other branding surfaces

| File | What's there |
|------|-------------|
| `profiles/<id>/profile.json` | Agent names, personalities (via `role` + prompt files), branding metadata |
| `profiles/<id>/prompts/*.txt` | Full system prompts, one file per agent |
| `static/<your-logo>` | Your logo asset — reference it from `brand.logo_path` |
| `app/agents/demos.py` | Demo data (resource names, scenarios) — not profile-driven; edit directly if needed |
| `infra/main.bicep` / `main.bicepparam.example` | Azure resource naming prefix (`prefix` param), `appProfile`, `agentOverrides` |
| `pipelines/azure-pipelines.yml` | CI/CD pipeline config (web app name, resource group) |
| `package.json` | NPM package name |
| `README.md` | Project description |
| `docs/` | Architecture docs, decision log, demo script |

## Validation

Profiles are validated at startup (not lazily) — a malformed `profile.json`
(missing/extra keys, wrong types, a missing prompt file) raises a clear,
itemized error immediately instead of silently using a partial/guessed config:

```
app.profiles.ProfileError: Profile 'contoso' is malformed (2 problem(s)):
  - brand.logo_path must be a non-empty string
  - agents.scout.prompt_file not found at /path/to/profiles/contoso/prompts/scout.txt
```

`APP_PROFILE` is validated as a plain identifier (lowercase letters, digits,
hyphens, underscores) and resolved strictly inside `profiles/` — path traversal
values like `../../etc` are rejected before anything is read from disk.
