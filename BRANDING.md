# Branding Guide — OGE Ops Council

This repo is a **white-label** multi-agent AI operations platform, pre-configured with Microsoft OGE (Oil, Gas & Energy) branding. Clone it and customize the values below to rebrand it for your customer.

## Quick Start — What to Change

### 1. Colors (Tailwind Theme)

Edit `templates/index.html` — find the `brand:` block in the Tailwind config:

```javascript
brand: {
  black: '#1A1A1A',   // ← Microsoft dark
  red: '#0078D4',     // ← Microsoft primary blue (accent)
  'red-dark': '#005A9E',
  gray: { ... }       // ← Microsoft Fluent gray palette
}
```

Replace with your customer's brand colors. All CSS classes use `brand-*` prefix (e.g., `bg-brand-black`, `text-brand-red`).

### 2. Logo & App Title

In `templates/index.html`, search for:
- `>OGE<` — the nav logo text (line ~59)
- `Microsoft · Oil, Gas & Energy` — the subtitle (line ~62)
- `OGE Ops Council` — the app name (appears in `<title>` and headers)

### 3. Agent Names & Personalities

Edit `app/config.py` — each agent has a `name`, `role`, and `system_prompt`:

| Config Key | Default Name | What They Do |
|------------|-------------|-------------|
| `orchestrator` | Pipeline | Routes requests, synthesizes answers |
| `cost_sentinel` | Barrel Counter | Cost optimization |
| `standards_architect` | The Roughneck | Infrastructure standards |
| `diagnostics_sre` | Turnaround | Diagnostics & root cause |
| `scout` | Flare Stack | Proactive monitoring |
| `compliance_inspector` | The Inspector | Compliance checking |

Rename them to match your customer's culture. Update the `system_prompt` and `role` text too.

### 4. Demo Data

Edit `app/agents/demos.py` — contains demo resource names, resource groups, and sample scenarios. Replace with customer-relevant examples.

### 5. Azure Resource Prefix

Edit `infra/main.bicep`:
```bicep
param prefix string = 'opscouncil'  // ← Change to customer prefix
```

This prefixes all deployed Azure resources (web app, key vault, managed identity, etc.).

### 6. Pipeline Config

Edit `pipelines/azure-pipelines.yml`:
- `value: '{prefix}-app'` — web app name
- `value: '{PREFIX}_RG'` — resource group name

### 7. Package Name

Edit `package.json`:
```json
"name": "oge-ops-council"  // ← Change to customer project name
```

## File Map — Where Branding Lives

| File | What's There |
|------|-------------|
| `templates/index.html` | UI: colors, logo, nav, all visible text |
| `app/config.py` | Agent names, personalities, system prompts |
| `app/agents/demos.py` | Demo data (resource names, scenarios) |
| `infra/main.bicep` | Azure resource naming prefix |
| `infra/deploy.sh` | Deployment script resource group |
| `pipelines/azure-pipelines.yml` | CI/CD pipeline config |
| `package.json` | NPM package name |
| `README.md` | Project description |
| `docs/` | Architecture docs, decision log, demo script |
