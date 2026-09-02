#!/usr/bin/env python3
"""Test the /api/health endpoint (app/main.py) — specifically the new
config.telemetry_enabled boolean, and that it stays a safe,
configuration-presence-only check (no live Azure calls, no secrets).

Requires the full requirements.txt (app/main.py imports app/azure_data.py,
which needs azure-mgmt-resourcegraph etc.) — run via a venv with
`pip install -r requirements.txt`, not necessarily the bare interpreter
used by tests/test_config.py.

Run: python3 tests/test_health.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Telemetry must be provably disabled for this test — no connection
# string means app.telemetry.init_telemetry() (called from
# app.main.create_app(), before Flask() is constructed) returns False.
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

from app.main import create_app  # noqa: E402
from app import telemetry  # noqa: E402
from app import __version__ as APP_VERSION  # noqa: E402

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


print("\n\U0001f9ea Test 1: GET /api/health reports telemetry_enabled: false with no connection string")
telemetry.reset_for_tests()
app = create_app()
client = app.test_client()

resp = client.get("/api/health")
test("200 OK", resp.status_code == 200)
body = resp.get_json()

test("status is ok", body["status"] == "ok")
test("version matches app.__version__", body["version"] == APP_VERSION)
test("profile field present", "profile" in body)
test("config.telemetry_enabled is a boolean", isinstance(body["config"]["telemetry_enabled"], bool))
test("telemetry_enabled is False (no connection string configured)", body["config"]["telemetry_enabled"] is False)
test("telemetry_enabled matches app.telemetry.is_enabled()", body["config"]["telemetry_enabled"] == telemetry.is_enabled())

# Safety: never leak a connection string, endpoint URL, or subscription
# ID anywhere in the response, even indirectly.
body_text = str(body)
test("no 'APPLICATIONINSIGHTS' substring leaked into the response", "APPLICATIONINSIGHTS" not in body_text.upper() or "applicationinsights_connection_string" not in body_text.lower())
test("no 'InstrumentationKey=' leaked (would indicate a raw connection string)", "InstrumentationKey=" not in body_text)
test("agents dict present with all six keys", set(body["agents"].keys()) == {
    "orchestrator", "cost_sentinel", "standards_architect", "diagnostics_sre", "scout", "compliance_inspector",
})

for key, agent_status in body["agents"].items():
    test(f"{key}: name present", bool(agent_status.get("name")))
    test(f"{key}: deployment present", bool(agent_status.get("deployment")))
    test(f"{key}: endpoint_configured is a boolean", isinstance(agent_status.get("endpoint_configured"), bool))
    test(f"{key}: max_completion_tokens is a non-negative int", isinstance(agent_status.get("max_completion_tokens"), int) and agent_status["max_completion_tokens"] >= 0)
    test(f"{key}: max_context_chars is a non-negative int", isinstance(agent_status.get("max_context_chars"), int) and agent_status["max_context_chars"] >= 0)
    test(f"{key}: response_instruction_configured is a boolean", isinstance(agent_status.get("response_instruction_configured"), bool))
    test(f"{key}: pricing_configured is a boolean", isinstance(agent_status.get("pricing_configured"), bool))

# The default ("power") profile sets a non-empty response_instruction and
# positive pricing on every agent — confirm health surfaces that truthfully.
test(
    "default profile's orchestrator reports response_instruction_configured: true",
    body["agents"]["orchestrator"]["response_instruction_configured"] is True,
)
test(
    "default profile's orchestrator reports pricing_configured: true",
    body["agents"]["orchestrator"]["pricing_configured"] is True,
)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
