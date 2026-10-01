#!/usr/bin/env python3
"""Test ZeroOps (app/zeroops/): escalation ledger, cost model, scenarios
(inject/cleanup gating + ARM calls via a fake), the MCP server protocol and
tools, and the /api/zeroops routes. analyze_operations is monkeypatched --
no real Azure/model calls.

Run: python3 tests/test_zeroops.py
"""
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"
DB_PATH = str(REPO_ROOT / "tests" / "_test_zeroops.db")


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()
os.environ["OPERATIONS_STATE_DB"] = DB_PATH
for key in list(os.environ):
    if key.startswith("ZEROOPS_") or key in ("MCP_API_KEY", "SRE_AGENT_AAU_PRICE_USD", "SRE_AGENT_MODEL"):
        os.environ.pop(key)

from app.agents import analysis as analysis_mod  # noqa: E402
from app.main import create_app  # noqa: E402
from app.zeroops import cost as cost_mod  # noqa: E402
from app.zeroops import scenarios as scen_mod  # noqa: E402
from app.zeroops import service as service_mod  # noqa: E402
from app.zeroops.ledger import EscalationLedger, get_ledger  # noqa: E402

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2705 {name}")
    else:
        FAIL += 1
        print(f"  \u274c {name}")


ANALYZE_CALLS = []


def fake_analyze(**kwargs):
    ANALYZE_CALLS.append(kwargs)
    if "boom" in kwargs["question"]:
        raise RuntimeError("backend exploded")
    return {
        "snapshot_id": "snap-1", "routing": {"specialist_agents": kwargs.get("requested_agents") or ["scout"]},
        "specialists": {"scout": {}, "compliance_inspector": {}}, "rebuttals": {"scout": {}} if kwargs.get("force_debate") else None,
        "final": {
            "agent": "Policy & Governance Advisor", "agent_key": "compliance_inspector", "schema_valid": True,
            "conclusion": "Hotfix opened public blob access; revert with a 7-day exception review.",
            "business_impact": "Data exposure risk on the demo storage account.", "confidence": "high",
            "narrative": "Grounded in f-1.", "recommended_actions": [
                {"description": "Disable anonymous blob access", "owner": "platform", "urgency": "immediate"}],
            "valid_evidence_ids": ["f-1"], "missing_evidence": [],
        },
        "evaluation": {"citation_validity": 1.0},
        "usage_summary": {"total_tokens": 12345, "model_calls": 4, "estimated_cost_usd": 0.0421},
    }


analysis_mod.analyze_operations = fake_analyze

print("\n\u2500\u2500 Cost model \u2500\u2500")
test("gpt-5.2 incident investigation AAU matches published profile (~11.7)", abs(cost_mod.sre_active_aau("incident_investigation", "gpt-5.2") - 11.725) < 0.01)
test("claude opus quick question AAU (~3.8)", abs(cost_mod.sre_active_aau("quick_question", "claude-opus-4.6") - 3.775) < 0.01)
base = cost_mod.monthly_baseline()
test("always-on is 4 AAU/hr * 730h", base["always_on_aau_per_month"] == 2920.0)
test("default AAU price is $0.10", base["aau_price_usd"] == 0.10 and base["always_on_usd_per_month"] == 292.0)
os.environ["SRE_AGENT_AAU_PRICE_USD"] = "0.11"
test("AAU price overridable", cost_mod.aau_price_usd() == 0.11)
os.environ["SRE_AGENT_AAU_PRICE_USD"] = "nonsense"
test("bad AAU price falls back to default", cost_mod.aau_price_usd() == 0.10)
os.environ.pop("SRE_AGENT_AAU_PRICE_USD")
ic = cost_mod.incident_cost(sre_profile="incident_investigation", squad_usage={"total_tokens": 1000, "estimated_cost_usd": 0.05, "model_calls": 2})
test("incident cost blends SRE estimate + squad measured", ic["total_estimated_usd"] == round(ic["sre_agent"]["estimated_usd"] + 0.05, 4))
test("squad tokens carried through", ic["squad"]["total_tokens"] == 1000 and ic["squad"]["model_calls"] == 2)

print("\n\u2500\u2500 Ledger \u2500\u2500")
ledger = EscalationLedger(DB_PATH)
rec = ledger.create(source="sre-agent", question="why?", incident_ref="INC-1")
test("create returns received record", rec["status"] == "received" and rec["id"].startswith("esc-"))
rec = ledger.update(rec["id"], status="proposed", result={"conclusion": "x"}, cost={"total_estimated_usd": 0.5, "squad": {"total_tokens": 10}})
test("update persists json fields", rec["result"]["conclusion"] == "x" and rec["cost"]["total_estimated_usd"] == 0.5)
test("list returns newest", ledger.list()[0]["id"] == rec["id"])
test("summary totals cost and tokens", ledger.summary()["total_estimated_usd"] == 0.5 and ledger.summary()["total_squad_tokens"] == 10)
for bad, kwargs in (("bad source", {"source": "x", "question": "q"}), ("blank question", {"source": "api", "question": " "})):
    try:
        ledger.create(**kwargs)
        test(f"rejects {bad}", False)
    except ValueError:
        test(f"rejects {bad}", True)
try:
    ledger.update(rec["id"], status="bogus")
    test("rejects bad status", False)
except ValueError:
    test("rejects bad status", True)
try:
    ledger.update("esc-missing", status="failed")
    test("update missing raises KeyError", False)
except KeyError:
    test("update missing raises KeyError", True)

print("\n\u2500\u2500 Scenarios \u2500\u2500")
all_s = scen_mod.list_scenarios()
test("five scenarios", len(all_s) == 5)
test("two SRE-solo, three squad", sum(s["tier"] == "sre" for s in all_s) == 2 and sum(s["tier"] == "squad" for s in all_s) == 3)
test("every squad scenario has question + why_escalate", all(s["escalation_question"] and s["why_escalate"] for s in all_s if s["tier"] == "squad"))
test("unconfigured scenarios report missing settings", all(not s["configured"] and s["missing_settings"] for s in all_s))
test("scripts render placeholders when unconfigured", "<resource_group>" in all_s[0]["remediation_script"])
try:
    scen_mod.run("open-door", "inject")
    test("inject refused when chaos disabled", False)
except scen_mod.ScenarioError as exc:
    test("inject refused when chaos disabled", "ZEROOPS_CHAOS_ENABLED" in str(exc))
os.environ.update({"ZEROOPS_CHAOS_ENABLED": "true", "ZEROOPS_DEMO_RESOURCE_GROUP": "rg-demo"})
try:
    scen_mod.run("open-door", "inject")
    test("inject refused when scenario resource missing", False)
except scen_mod.ScenarioError as exc:
    test("inject refused when scenario resource missing", "ZEROOPS_DEMO_NSG" in str(exc))
os.environ.update({"ZEROOPS_DEMO_NSG": "nsg-demo", "ZEROOPS_DEMO_WEBAPP": "web-demo", "ZEROOPS_DEMO_PLAN": "plan-demo",
                   "ZEROOPS_DEMO_STORAGE": "stdemo", "ZEROOPS_DEMO_KEYVAULT": "kv-demo", "ZEROOPS_DEMO_AUTOMATION": "aa-demo"})
CALLS = []


def fake_call(method, url, *, scope=scen_mod.ARM_SCOPE, body=None, params=None, text=None):
    CALLS.append((method, url, body, text))
    if method == "GET":
        return {"location": "westus2"}
    if "storageAccounts" in url:
        return {"properties": {"minimumTlsVersion": (body or {}).get("properties", {}).get("minimumTlsVersion")}}
    if "/jobs/" in url:
        return {"properties": {"status": "New"}}
    return {"name": "rule", "id": "x/y", "sku": (body or {}).get("sku")}


scen_mod._call = fake_call
test("all configured once settings present", all(s["configured"] for s in scen_mod.list_scenarios()))
out = scen_mod.run("open-door", "inject")
test("open-door inject PUTs NSG rule", out["status"] == "ok" and CALLS[-1][0] == "PUT" and "securityRules/" + scen_mod.CHAOS_NSG_RULE in CALLS[-1][1])
test("open-door rule opens 22 from *", CALLS[-1][2]["properties"]["destinationPortRange"] == "22")
scen_mod.run("open-door", "cleanup")
test("open-door cleanup DELETEs rule", CALLS[-1][0] == "DELETE")
scen_mod.run("bad-deploy", "inject")
test("bad-deploy sets broken startup command", CALLS[-1][2]["properties"]["appCommandLine"] == scen_mod.BAD_DEPLOY_COMMAND)
scen_mod.run("bad-deploy", "cleanup")
test("bad-deploy cleanup restores empty command", CALLS[-1][2]["properties"]["appCommandLine"] == "")
scen_mod.run("storm-surge", "inject")
test("storm-surge scales to P0v3 x3 and keeps base tags", CALLS[-1][2]["sku"]["capacity"] == 3 and CALLS[-1][2]["tags"]["zeroops-demo"] == "true")
scen_mod.run("storm-surge", "cleanup")
test("storm-surge cleanup back to B1 x1", CALLS[-1][2]["sku"] == {"name": "B1", "tier": "Basic", "capacity": 1} and CALLS[-1][2]["tags"] == scen_mod.BASE_TAGS)
scen_mod.run("rogue-hotfix", "inject")
test("rogue-hotfix enables public blob + TLS1_0", CALLS[-1][2]["properties"] == {"allowBlobPublicAccess": True, "minimumTlsVersion": "TLS1_0"})
scen_mod.run("rogue-hotfix", "cleanup")
test("rogue-hotfix cleanup reverts", CALLS[-1][2]["properties"]["allowBlobPublicAccess"] is False)
CALLS.clear()
out = scen_mod.run("2am-cert", "inject")
test("2am-cert inject has 3 ok steps", out["status"] == "ok" and len(out["steps"]) == 3)
test("2am-cert stores expiring secret", CALLS[0][0] == "PUT" and scen_mod.CERT_SECRET_NAME in CALLS[0][1] and CALLS[0][2]["attributes"]["exp"] > time.time())
test("2am-cert uploads failing runbook script", any(c[3] == scen_mod.CERT_RUNBOOK_SCRIPT for c in CALLS))
test("2am-cert starts a job", "/jobs/" in CALLS[-1][1])
out = scen_mod.run("2am-cert", "cleanup")
test("2am-cert cleanup deletes and purges", out["status"] == "ok" and len(out["steps"]) == 2)


def failing_call(*a, **k):
    raise scen_mod.ScenarioError("403 forbidden")


scen_mod._call = failing_call
out = scen_mod.run("open-door", "inject")
test("ARM failure is an explicit error step", out["status"] == "error" and "403" in out["steps"][0]["error"])
scen_mod._call = fake_call

print("\n\u2500\u2500 Escalation service \u2500\u2500")
rec = service_mod.escalate(scenario_id="rogue-hotfix", source="simulated", sre_summary="policy + security", wait_seconds=10)
test("scenario escalation completes to proposed", rec["status"] == "proposed")
test("scenario expected agents requested", ANALYZE_CALLS[-1]["requested_agents"] == ["scout", "compliance_inspector", "standards_architect"])
test("SRE triage folded into question", "Azure SRE Agent triage so far: policy + security" in ANALYZE_CALLS[-1]["question"])
test("result carries remediation script with real names", "stdemo" in rec["result"]["remediation_script"])
test("cost recorded with squad tokens", rec["cost"]["squad"]["total_tokens"] == 12345 and rec["cost"]["total_estimated_usd"] > 0.0421)
failed = service_mod.escalate(question="boom please", source="api", wait_seconds=10)
test("backend failure recorded as failed", failed["status"] == "failed" and "exploded" in failed["error"])
try:
    service_mod.propose_fix(failed["id"])
    test("cannot propose a failed escalation", False)
except ValueError:
    test("cannot propose a failed escalation", True)
out = service_mod.propose_fix(rec["id"])
test("propose_fix creates pending proposal", out["proposal"]["status"] == "pending" and out["escalation"]["proposal_id"] == out["proposal"]["id"])
test("proposal description includes script", "az storage account update" in out["proposal"]["description"])
test("propose_fix is idempotent", service_mod.propose_fix(rec["id"])["proposal"]["id"] == out["proposal"]["id"])

print("\n\u2500\u2500 MCP server \u2500\u2500")
app = create_app()
client = app.test_client()
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
test("disabled (503) without MCP_API_KEY", r.status_code == 503)
os.environ["MCP_API_KEY"] = "test-key-123"
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
test("401 without key", r.status_code == 401)
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"X-API-Key": "wrong"})
test("401 with wrong key", r.status_code == 401)
H = {"X-API-Key": "test-key-123"}
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer test-key-123"})
test("bearer auth accepted", r.status_code == 200 and r.get_json()["result"] == {})
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, headers=H)
init = r.get_json()["result"]
test("initialize echoes supported protocol", init["protocolVersion"] == "2025-03-26")
test("initialize advertises tools + serverInfo", "tools" in init["capabilities"] and init["serverInfo"]["name"] == "ogeops")
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}, headers=H)
test("unknown protocol negotiates to latest", r.get_json()["result"]["protocolVersion"] == "2025-06-18")
r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=H)
test("notification returns 202", r.status_code == 202)
r = client.get("/mcp", headers=H)
test("GET (SSE) returns 405", r.status_code == 405)
r = client.post("/mcp", data="not json", content_type="application/json", headers=H)
test("parse error -32700", r.status_code == 400 and r.get_json()["error"]["code"] == -32700)
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=H)
names = [t["name"] for t in r.get_json()["result"]["tools"]]
test("tools/list exposes escalation tools", {"escalate", "get_escalation", "propose_fix", "list_findings", "get_evidence", "agent_catalog", "list_scenarios", "cost_model"} == set(names))
test("tools have input schemas", all(t["inputSchema"]["type"] == "object" for t in r.get_json()["result"]["tools"]))
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "nope"}, headers=H)
test("unknown method -32601", r.get_json()["error"]["code"] == -32601)
r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}}, headers=H)
test("unknown tool -32602", r.get_json()["error"]["code"] == -32602)


def call(name, args, msg_id=10):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": args}}, headers=H)
    return resp.get_json()["result"]


res = call("escalate", {"bogus": 1})
test("unknown argument is a tool error", res["isError"] and "unknown argument" in res["content"][0]["text"])
res = call("escalate", {"severity": "apocalyptic", "question": "q"})
test("enum validated", res["isError"])
res = call("escalate", {"question": "Storage was opened by a hotfix; revert?", "incident_ref": "alert-42",
                        "sre_summary": "Policy + Defender alerts; unknown dependency", "severity": "high", "debate": True, "wait_seconds": 10})
test("escalate returns structured result", not res["isError"] and res["structuredContent"]["status"] == "proposed")
esc_id = res["structuredContent"]["escalation_id"]
test("escalate recorded as sre-agent source", get_ledger().get(esc_id)["source"] == "sre-agent")
test("debate flag forwarded", ANALYZE_CALLS[-1]["force_debate"] is True)
test("escalate returns next_step guidance", "propose_fix" in res["structuredContent"]["next_step"])
res = call("get_escalation", {"escalation_id": esc_id})
test("get_escalation returns cost", res["structuredContent"]["cost"]["squad"]["total_tokens"] == 12345)
res = call("get_escalation", {"escalation_id": "esc-nope"})
test("get_escalation unknown is tool error", res["isError"])
res = call("propose_fix", {"escalation_id": esc_id})
test("propose_fix via MCP returns pending proposal", res["structuredContent"]["proposal"]["status"] == "pending")
res = call("list_scenarios", {})
test("list_scenarios via MCP", len(res["structuredContent"]["scenarios"]) == 5)
res = call("agent_catalog", {})
test("agent_catalog via MCP", len(res["structuredContent"]["agents"]) >= 6 and "what" in res["structuredContent"]["agents"][0])
res = call("cost_model", {})
test("cost_model via MCP", res["structuredContent"]["always_on_aau_per_hour"] == 4.0)
r = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}], headers=H)
test("batch returns only request responses", r.status_code == 200 and len(r.get_json()) == 1)

print("\n\u2500\u2500 ZeroOps routes \u2500\u2500")
r = client.get("/api/zeroops/overview")
ov = r.get_json()
test("overview ok with mcp enabled + 5 scenarios", r.status_code == 200 and ov["mcp"]["enabled"] and len(ov["scenarios"]) == 5)
test("overview never leaks demo resource names", "rg-demo" not in r.get_data(as_text=True).split('"remediation_script"')[0])
r = client.post("/api/zeroops/scenarios/storm-surge/escalate", json={"wait_seconds": 10})
test("simulate escalation 202 + simulated source", r.status_code == 202 and r.get_json()["source"] == "simulated")
sim_id = r.get_json()["id"]
r = client.post("/api/zeroops/scenarios/open-door/inject")
test("inject route runs scenario", r.status_code == 200 and r.get_json()["status"] == "ok")
os.environ["ZEROOPS_CHAOS_ENABLED"] = "false"
r = client.post("/api/zeroops/scenarios/open-door/inject")
test("inject route 409 when chaos disabled", r.status_code == 409)
r = client.post("/api/zeroops/scenarios/nope/inject")
test("unknown scenario 404", r.status_code == 404)
r = client.post("/api/zeroops/scenarios/open-door/explode")
test("unknown action 400", r.status_code == 400)
r = client.get("/api/zeroops/escalations")
test("list escalations with summary", r.status_code == 200 and r.get_json()["summary"]["count"] >= 3)
r = client.post(f"/api/zeroops/escalations/{sim_id}/propose")
test("propose route", r.status_code == 200 and r.get_json()["proposal"]["status"] == "pending")
r = client.post(f"/api/zeroops/escalations/{sim_id}/decision", json={"decision": "approve", "by": "alice"})
test("approve decision -> approved", r.status_code == 200 and r.get_json()["escalation"]["status"] == "approved")
test("decision audit recorded", r.get_json()["escalation"]["result"]["decisions"][0]["by"] == "alice")
test("decision audit timestamped", bool(r.get_json()["escalation"]["result"]["decisions"][0].get("at")))
r = client.post(f"/api/zeroops/escalations/{sim_id}/decision", json={"decision": "approve"})
test("cannot approve twice (409)", r.status_code == 409)
r = client.post(f"/api/zeroops/escalations/{sim_id}/decision", json={"decision": "resolve"})
test("resolve after approval", r.get_json()["escalation"]["status"] == "resolved")
r = client.post("/api/zeroops/escalations", json={})
test("POST escalation without question 400", r.status_code == 400)
r = client.get("/api/zeroops/escalations/esc-nope")
test("unknown escalation 404", r.status_code == 404)

r = client.get("/")
page = r.get_data(as_text=True)
test("index renders ZeroOps view + nav", 'id="view-zeroops"' in page and 'id="nav-zeroops"' in page and "loadZeroOps" in page)

print("\n\U0001F916 SRE Agent assets (sre-agent/) stay in sync with the MCP server")
import json as _json  # noqa: E402
import re as _re  # noqa: E402
from app.zeroops import mcp_server as _mcp  # noqa: E402

SRE_DIR = REPO_ROOT / "sre-agent"
agent_yaml = (SRE_DIR / "agents" / "zeroops-triage.yaml").read_text()


def _yaml_list(text, key):
    m = _re.search(rf"^  {key}:\n((?:    - .+\n)+)", text, _re.M)
    return [line.strip()[2:] for line in m.group(1).splitlines()] if m else []


mcp_tools = _yaml_list(agent_yaml, "mcpTools")
server_tools = {f"ogeops_{t['name']}" for t in _mcp.TOOL_DEFINITIONS}
test("agent mcpTools all exist on the MCP server", mcp_tools and set(mcp_tools) <= server_tools)
test("agent exposes every MCP tool", set(mcp_tools) == server_tools)
allowed = _yaml_list(agent_yaml, "allowedSkills")
skill_dirs = {p.name for p in (SRE_DIR / "skills").iterdir() if (p / "SKILL.md").exists()}
test("allowedSkills match skill folders", set(allowed) == skill_dirs and len(skill_dirs) >= 3)
for skill in sorted(skill_dirs):
    text = (SRE_DIR / "skills" / skill / "SKILL.md").read_text()
    test(f"skill {skill} frontmatter name/description",
         text.startswith("---\n") and f"\nname: {skill}\n" in text and "\ndescription: " in text)
escalate_props = set(next(t for t in _mcp.TOOL_DEFINITIONS if t["name"] == "escalate")["inputSchema"]["properties"])
used_args = set(_re.findall(r"^\s+([a-z_]+)\s+=", agent_yaml, _re.M))
test("agent instructions only use real ogeops_escalate args", used_args and used_args <= escalate_props)
f = _json.loads((SRE_DIR / "triggers" / "incident-filter.json").read_text())
test("response plan routes ZeroOps alerts to zeroops-triage",
     f["properties"]["handlingAgent"] == "zeroops-triage" and f["properties"]["titleContains"] == "ZeroOps")
t = _json.loads((SRE_DIR / "triggers" / "scheduled-task.json").read_text())
test("scheduled task targets zeroops-triage", t["properties"]["agent"] == "zeroops-triage")
demo_bicep = (REPO_ROOT / "infra" / "zeroops-demo" / "main.bicep").read_text()
for s in scen_mod.SCENARIOS:
    test(f"demo infra has an alert for {s.id}", f"'ZeroOps {s.id} - " in demo_bicep)
test("demo infra outputs every ZEROOPS_DEMO_* setting",
     all(f"ZEROOPS_DEMO_{k}" in demo_bicep for k in ("RESOURCE_GROUP", "NSG", "WEBAPP", "PLAN", "STORAGE", "KEYVAULT", "AUTOMATION")))
connector = (SRE_DIR / "connectors" / "ogeops-mcp.json").read_text()
test("connector example holds no real key", "@@MCP_API_KEY@@" in connector)

_cleanup_db()
print(f"\n{'=' * 50}\n  Results: {PASS} passed, {FAIL} failed\n{'=' * 50}")
sys.exit(1 if FAIL else 0)
