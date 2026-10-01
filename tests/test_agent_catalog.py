#!/usr/bin/env python3
"""Test the agent catalog (app/agents/catalog.py), GET /api/agents, the
Agent Squad page render, and that docs/AGENTS.md is regenerated.

Run: python3 tests/test_agent_catalog.py
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-1"
DB_PATH = str(REPO_ROOT / "tests" / "_test_agent_catalog.db")


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()
os.environ["OPERATIONS_STATE_DB"] = DB_PATH

from app.agents import catalog as catalog_mod  # noqa: E402
from app.agents import routing as routing_mod  # noqa: E402
from app.agents import tools as tools_mod  # noqa: E402
from app.config import AGENT_KEYS, Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.operations.models import FindingCategory  # noqa: E402

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


print("\n🧪 Test 1: catalog derives from routing/tools for every profile")
for profile_id in ("power", "generic", "oge"):
    settings = Settings(profile_id=profile_id)
    cat = catalog_mod.build_agent_catalog(settings.agents)
    keys = [a["key"] for a in cat]
    test(f"[{profile_id}] one entry per agent key", sorted(keys) == sorted(AGENT_KEYS))
    test(f"[{profile_id}] coordinator listed first", keys[0] == routing_mod.COORDINATOR_KEY)
    handled = {h["category"] for a in cat for h in a["handles"]}
    test(f"[{profile_id}] every finding category is owned by exactly one agent",
         handled == {c.value for c in FindingCategory}
         and sum(len(a["handles"]) for a in cat) == len(FindingCategory))
    test(f"[{profile_id}] names/roles/deployments come from the profile",
         all(a["name"] == settings.agents[a["key"]].name and a["role"] == settings.agents[a["key"]].role
             and a["model_deployment"] == settings.agents[a["key"]].deployment for a in cat))
    test(f"[{profile_id}] every agent has what/how/outputs/evidence/guardrails",
         all(a["what"] and a["how"] and a["outputs"] and a["evidence_sources"] and a["guardrails"] for a in cat))
    test(f"[{profile_id}] tools list matches registered read-only tools",
         all(a["tools"] == list(tools_mod.TOOLS) for a in cat))

test("every category has a label and evidence source",
     all(catalog_mod.CATEGORY_LABELS.get(c.value) and catalog_mod.CATEGORY_EVIDENCE.get(c.value) for c in FindingCategory))
test("every agent key has a functional description", set(catalog_mod.AGENT_FUNCTIONS) == set(AGENT_KEYS))

print("\n🧪 Test 2: runtime reflects backend")
power = Settings(profile_id="power")
direct = catalog_mod.build_agent_catalog(power.agents, backend="direct", foundry_agent_prefix="acme")
test("direct backend -> no Foundry agent name", all(a["runtime"]["foundry_agent"] == "" for a in direct))
foundry = catalog_mod.build_agent_catalog(power.agents, backend="foundry", foundry_agent_prefix="acme")
cost = next(a for a in foundry if a["key"] == "cost_sentinel")
test("foundry backend -> deterministic Foundry agent name",
     cost["runtime"]["foundry_agent"] == "acme-cost-sentinel-agent-analysis-result")

print("\n🧪 Test 3: power/generic use functional display names")
expected = {
    "orchestrator": "Operations Coordinator", "cost_sentinel": "Cost & Capacity Analyst",
    "diagnostics_sre": "Incident & Change Investigator", "scout": "Security & Monitoring Analyst",
    "compliance_inspector": "Policy & Governance Advisor", "standards_architect": "Resilience & Hygiene Engineer",
}
for profile_id in ("power", "generic"):
    s = Settings(profile_id=profile_id)
    test(f"[{profile_id}] names match", {k: s.agents[k].name for k in expected} == expected)
    test(f"[{profile_id}] each prompt introduces the agent by its display name",
         all(f"You are the {s.agents[k].name}" in s.agents[k].system_prompt for k in expected))

print("\n🧪 Test 4: GET /api/agents and the Agent Squad page")
os.environ.pop("AGENT_BACKEND", None)
app = create_app()
client = app.test_client()
resp = client.get("/api/agents")
test("/api/agents -> 200", resp.status_code == 200)
body = resp.get_json() or {}
test("/api/agents returns all six agents", len(body.get("agents", [])) == 6)
page = client.get("/").get_data(as_text=True)
test("index renders Agent Squad heading", "The Agent Squad" in page)
test("index renders every agent's 'what'",
     all(a["what"].replace("'", "&#39;") in page for a in body.get("agents", [])))
test("index renders 'How it works' panels", page.count("How it works</summary>") == 6)

print("\n🧪 Test 5: docs/AGENTS.md is up to date")
check = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "generate_agent_docs.py"), "--check"],
                       capture_output=True, text=True)
test("generate_agent_docs.py --check passes", check.returncode == 0)

_cleanup_db()
print(f"\n{'=' * 50}\nResults: {PASS} passed, {FAIL} failed\n{'=' * 50}")
sys.exit(1 if FAIL else 0)
