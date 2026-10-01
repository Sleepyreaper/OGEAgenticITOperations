#!/usr/bin/env bash
# Configure an existing Azure SRE Agent as the ZeroOps tier-1 triage agent for OGE Agentic Ops.
#
# Creates/updates (idempotent):
#   1. MCP connector "ogeops"  -> <OGE_APP_URL>/mcp   (tools appear as ogeops_*)
#   2. Skills                   -> sre-agent/skills/*/SKILL.md
#   3. Custom agent             -> sre-agent/agents/zeroops-triage.yaml
#   4. Response plan            -> sre-agent/triggers/incident-filter.json
#   5. Scheduled task           -> sre-agent/triggers/scheduled-task.json (optional)
#
# Required env:
#   SRE_AGENT_RESOURCE_GROUP  resource group of the SRE Agent
#   SRE_AGENT_NAME            SRE Agent resource name
#   OGE_APP_URL               https://<your-oge-app>   (no trailing slash)
#   MCP_API_KEY               same value as the app's MCP_API_KEY setting
# Optional env:
#   SRE_AGENT_SUBSCRIPTION    defaults to the az CLI default subscription
#   SKIP_SCHEDULED_TASK=1     do not create the daily sweep
#   DRY_RUN=1                 print the request bodies instead of calling the API
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_VERSION="2025-05-01-preview"  # ARM api-version for Microsoft.App/agents

: "${SRE_AGENT_RESOURCE_GROUP:?set SRE_AGENT_RESOURCE_GROUP}"
: "${SRE_AGENT_NAME:?set SRE_AGENT_NAME}"
: "${OGE_APP_URL:?set OGE_APP_URL (e.g. https://ops.contoso.com)}"
: "${MCP_API_KEY:?set MCP_API_KEY (must match the app setting)}"
OGE_APP_URL="${OGE_APP_URL%/}"
SUB="${SRE_AGENT_SUBSCRIPTION:-$(az account show --query id -o tsv)}"
DRY_RUN="${DRY_RUN:-0}"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 -c "import yaml" 2>/dev/null || { echo "PyYAML is required: python3 -m pip install pyyaml"; exit 1; }

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m[%s] %s\033[0m\n' "$1" "$2"; }

step 0 "Resolve SRE Agent"
AGENT_ID="/subscriptions/${SUB}/resourceGroups/${SRE_AGENT_RESOURCE_GROUP}/providers/Microsoft.App/agents/${SRE_AGENT_NAME}"
ENDPOINT="$(az rest --method GET --url "https://management.azure.com${AGENT_ID}?api-version=${API_VERSION}" \
  --query properties.agentEndpoint -o tsv)"
ENDPOINT="${ENDPOINT%/}"
[[ -n "$ENDPOINT" ]] || { echo "Could not read properties.agentEndpoint for ${AGENT_ID}"; exit 1; }
ok "endpoint resolved"

TOKEN=""
for aud in https://azuresre.ai https://azuresre.dev; do
  TOKEN="$(az account get-access-token --resource "$aud" --query accessToken -o tsv 2>/dev/null || true)"
  [[ -n "$TOKEN" ]] && break
done
[[ -n "$TOKEN" ]] || { echo "Could not get a data-plane token for the SRE Agent"; exit 1; }

agent_put() {  # agent_put <path> <json-body> <label>
  local path="$1" body="$2" label="$3" code
  if [[ "$DRY_RUN" == "1" ]]; then echo "PUT ${path}"; echo "$body" | python3 -m json.tool | head -40; return 0; fi
  code="$(curl -sS -o /tmp/zeroops-sre-resp.$$ -w '%{http_code}' -X PUT "${ENDPOINT}${path}" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" --data "$body")"
  if [[ "$code" =~ ^2 ]]; then ok "$label"; rm -f /tmp/zeroops-sre-resp.$$; return 0; fi
  warn "$label failed (HTTP $code): $(head -c 300 /tmp/zeroops-sre-resp.$$)"; rm -f /tmp/zeroops-sre-resp.$$; return 1
}

step 1 "Probe OGE MCP endpoint"
probe="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${OGE_APP_URL}/mcp" -H "X-API-Key: ${MCP_API_KEY}" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' || true)"
if [[ "$probe" == "200" ]]; then ok "${OGE_APP_URL}/mcp answers tools/list"
else warn "${OGE_APP_URL}/mcp returned HTTP ${probe} — check MCP_API_KEY, App Service auth exclusions and networking"; fi

step 2 "MCP connector 'ogeops'"
# ARM child resource Microsoft.App/agents/connectors (same as the official SRE Agent Bicep recipes).
# BearerToken auth: the OGE MCP server accepts "Authorization: Bearer <MCP_API_KEY>" as well as X-API-Key.
CONN_URL="https://management.azure.com${AGENT_ID}/connectors/ogeops?api-version=${API_VERSION}"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "PUT ${CONN_URL}  {dataConnectorType: Mcp, endpoint: ${OGE_APP_URL}/mcp, authType: BearerToken, bearerToken: <redacted>}"
else
  CONN_BODY="$(mktemp)"; chmod 600 "$CONN_BODY"
  OGE_APP_URL="$OGE_APP_URL" python3 -c 'import json,os;print(json.dumps({"properties":{"dataConnectorType":"Mcp","dataSource":"ogeops-mcp","extendedProperties":{"type":"http","endpoint":os.environ["OGE_APP_URL"]+"/mcp","authType":"BearerToken","bearerToken":os.environ["MCP_API_KEY"]},"identity":"system"}}))' >"$CONN_BODY"
  state="$(az rest --method PUT --url "$CONN_URL" --body "@${CONN_BODY}" --query properties.provisioningState -o tsv 2>/tmp/zeroops-conn.$$ || true)"
  rm -f "$CONN_BODY"
  if [[ "$state" == "Succeeded" ]]; then ok "connector ogeops -> ${OGE_APP_URL}/mcp (tools: ogeops_*)"
  else
    warn "connector create returned '${state:-error}': $(grep -v -i bearer /tmp/zeroops-conn.$$ | tail -2)"
    echo "      Fallback: SRE Agent > Settings > Connectors > Add > MCP server (HTTP)"
    echo "      Name: ogeops   URL: ${OGE_APP_URL}/mcp   Auth: Bearer token = <MCP_API_KEY>"
  fi
  rm -f /tmp/zeroops-conn.$$
fi
if command -v azmcp >/dev/null && [[ "$DRY_RUN" != "1" ]]; then
  TENANT="$(az account show --query tenantId -o tsv)"
  n="$(azmcp sreagent connectors test --name ogeops --agent "$SRE_AGENT_NAME" --subscription "$SUB" \
       --resource-group "$SRE_AGENT_RESOURCE_GROUP" --tenant "$TENANT" 2>/dev/null | grep -o '"ogeops_[a-z_]*"' | sort -u | wc -l | tr -d ' ')"
  [[ "$n" -gt 0 ]] && ok "agent discovered ${n} ogeops_* tools" || warn "could not verify tool discovery (azmcp connectors test)"
fi

step 3 "Skills"
for dir in "$ROOT"/skills/*/; do
  name="$(basename "$dir")"
  body="$(SKILL_DIR="$dir" SKILL_NAME="$name" python3 - <<'PY'
import json, os, pathlib, yaml
d = pathlib.Path(os.environ["SKILL_DIR"])
text = (d / "SKILL.md").read_text()
front = yaml.safe_load(text.split("---", 2)[1])
extra = [{"filePath": p.name, "content": p.read_text()} for p in sorted(d.iterdir()) if p.name != "SKILL.md" and p.is_file()]
print(json.dumps({"name": os.environ["SKILL_NAME"], "type": "Skill", "properties": {
    "description": front["description"], "tools": front.get("tools", []),
    "skillContent": text, "additionalFiles": extra}}))
PY
)"
  agent_put "/api/v2/extendedAgent/skills/${name}" "$body" "skill ${name}" || true
done

step 4 "Custom agent zeroops-triage"
body="$(python3 - "$ROOT/agents/zeroops-triage.yaml" <<'PY'
import json, sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
spec = doc["spec"]
print(json.dumps({"name": doc["metadata"]["name"], "type": "ExtendedAgent", "tags": [], "owner": "",
    "properties": {"instructions": spec["instructions"], "handoffDescription": spec.get("handoffDescription", ""),
        "handoffs": spec.get("handoffs", []), "tools": spec.get("tools", []), "mcpTools": spec.get("mcpTools", []),
        "allowParallelToolCalls": spec.get("allowParallelToolCalls", True), "enableSkills": spec.get("enableSkills", True),
        "allowedSkills": spec.get("allowedSkills", [])}}))
PY
)"
agent_put "/api/v2/extendedAgent/agents/zeroops-triage" "$body" "agent zeroops-triage" || true

step 5 "Response plan (incident filter)"
body="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));d.pop("_comment",None);d["type"]="IncidentFilter";d["tags"]=[];print(json.dumps(d))' "$ROOT/triggers/incident-filter.json")"
agent_put "/api/v2/extendedAgent/incidentFilters/zeroops-response" "$body" "response plan zeroops-response" \
  || warn "create it in the portal: Response plans > New > title contains 'ZeroOps' > agent zeroops-triage > Review"

if [[ "${SKIP_SCHEDULED_TASK:-0}" != "1" ]]; then
  step 6 "Scheduled task zeroops-daily-sweep"
  body="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));d.pop("_comment",None);d["type"]="ScheduledTask";print(json.dumps(d))' "$ROOT/triggers/scheduled-task.json")"
  agent_put "/api/v2/extendedAgent/scheduledtasks/zeroops-daily-sweep" "$body" "scheduled task zeroops-daily-sweep" || true
fi

printf '\nDone. In the SRE Agent portal, open a new thread and try:\n'
printf '  "/agent zeroops-triage list the ZeroOps scenarios and the OGE squad cost model"\n'
