#!/usr/bin/env python3
"""Flask/UI contract tests for the two-view product UI
(templates/index.html + app/main.py + app/operations/routes.py).

Verifies (see the redesign requirements this satisfies):
  - Exactly two PRIMARY nav views (Executive Brief, Operations Center);
    Ops Council/The Crew are reachable but never compete as top-level nav.
  - The old fake executive labels/formulas (readiness ring, Revenue at
    Risk, static MTTR, the five-button "Ask the AI Council" wall, static
    "Who's On It" team cards, opaque pillar scores) are gone.
  - Required containers for the new Executive Brief / Operations Center
    surfaces are present.
  - Agent names are profile-driven (rendered from app.config.settings,
    not hardcoded).
  - GET /api/operations/demo returns the centralized demo-fixture schema.
  - Every `/api/operations/...` route the frontend calls is actually
    registered on the Flask app (no stale/typo'd route strings).
  - Honest error-state renderers exist (never converted to a fake
    all-clear).
  - Old Ops Council/Crew capabilities are still reachable from the
    template (secondary nav, not deleted).

Run: python3 tests/test_ui_contract.py
"""
import html as html_module
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-ui-contract-test"
DB_PATH = str(REPO_ROOT / "tests" / "_test_ui_contract.db")
os.environ["OPERATIONS_STATE_DB"] = DB_PATH


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()

from app.config import settings  # noqa: E402
from app.main import create_app  # noqa: E402

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


app = create_app()
client = app.test_client()

print("\n\U0001f9ea Test 1: GET / renders successfully")
resp = client.get("/")
test("200 OK", resp.status_code == 200)
html = resp.get_data(as_text=True)

print("\n\U0001f9ea Test 2: exactly two PRIMARY nav views")
nav_tab_ids = re.findall(r'id="(nav-[a-z]+)"\s+role="tab"', html)
test("exactly 2 elements with role=tab", len(nav_tab_ids) == 2)
test("the two primary tabs are Executive Brief and Operations Center", set(nav_tab_ids) == {"nav-executive", "nav-ops"})
test('nav labeled "Executive Brief"', "Executive Brief" in html)
test('nav labeled "Operations Center"', "Operations Center" in html)

print("\n\U0001f9ea Test 3: old fake executive labels/formulas/dead code are gone")
forbidden_strings = [
    "Revenue at Risk", "Mean Recovery Time", "Ask the AI Council", "Who's On It",
    "Breach Risk", "Outage Readiness", "Spend Efficiency", "READY</span>",
    "exec-score-ring", "exec-mttr", "exec-risk", "exec-uptime",
    "loadLiveDashboard", "updateExecutiveView", "buildIssueCard", "updateDashboardWithLive",
    "DEMO_ISSUES_HTML", "DEMO_COST_TABLE_HTML", "resetDemoDashboard", "investigateIssue(",
    "renderCouncilResponse", "updateSuggestedQuestions", "autoRefreshLive",
]
for s in forbidden_strings:
    test(f"removed: {s!r}", s not in html)

print("\n\U0001f9ea Test 4: required Executive Brief containers present")
for cid in [
    "exec-headline", "exec-freshness", "exec-error", "exec-business-body", "exec-reliability-body",
    "exec-capacity-body", "exec-changes-list", "exec-decisions-list", "exec-attention-list",
    "exec-briefing-btn", "exec-source-coverage",
]:
    test(f'container id="{cid}"', f'id="{cid}"' in html)

print("\n\U0001f9ea Test 5: required Operations Center containers present")
for cid in [
    "ops-error", "handoff-toggle", "handoff-summary", "handoff-grid", "ops-coverage-body",
    "ops-capacity-watch-body", "ops-recent-changes", "ops-category-chips", "queue-count",
    "queue-filter-status", "queue-filter-category", "queue-filter-severity", "queue-filter-owner",
    "queue-list", "queue-load-more", "tools-toggle", "ado-scan-btn",
]:
    test(f'container id="{cid}"', f'id="{cid}"' in html)

print("\n\U0001f9ea Test 6: finding detail drawer + briefing modal (dialogs) present and accessible")
test('drawer has role="dialog"', 'id="finding-drawer-panel" role="dialog"' in html)
test("drawer has aria-modal", 'aria-modal="true"' in html and "finding-drawer" in html)
test('briefing modal has role="dialog"', 'id="briefing-modal-panel" role="dialog"' in html)
test("reduced-motion media query present", "prefers-reduced-motion" in html)
test("aria-live announcer present", 'id="a11y-announcer"' in html and 'aria-live="polite"' in html)

def _name_rendered(name: str, rendered_html: str) -> bool:
    """An agent display name may appear either HTML-escaped (Jinja's
    default autoescaping, e.g. in the Tools & Demos crew list) or
    JSON/unicode-escaped (Jinja's `tojson` filter, e.g. in the JS
    AGENT_COLORS map) -- check both forms rather than assuming raw text."""
    if name in rendered_html:
        return True
    if html_module.escape(name) in rendered_html:
        return True
    json_escaped = name.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("'", "\\u0027")
    return json_escaped in rendered_html


print("\n\U0001f9ea Test 7: agent names are profile-driven (not hardcoded)")
test("orchestrator name rendered from settings", _name_rendered(settings.agents["orchestrator"].name, html))
test("scout name rendered from settings", _name_rendered(settings.agents["scout"].name, html))
test("cost_sentinel name rendered from settings", _name_rendered(settings.agents["cost_sentinel"].name, html))

print("\n\U0001f9ea Test 8: old Ops Council / Crew capabilities still reachable (secondary nav, not deleted)")
test('secondary nav id="nav-council" present', 'id="nav-council"' in html)
test('secondary nav id="nav-agents" present', 'id="nav-agents"' in html)
test("showView('council') wired to the secondary nav", "showView('council')" in html)
test("showView('agents') wired to the secondary nav", "showView('agents')" in html)
test('view-council container present (chat/debate preserved)', 'id="view-council"' in html)
test('view-agents container present (crew bios preserved)', 'id="view-agents"' in html)
test("chaos demo (runChaos) still reachable", "runChaos()" in html)
test("morning briefing still reachable", "runMorningBriefing()" in html)
test("remediation generation still reachable", "generateRemediation()" in html)
test("demo scenarios (runDemo) still reachable", "runDemo(" in html)
test("ADO compliance proposals reachable from the UI", "runAdoInspection()" in html)

print("\n\U0001f9ea Test 9: safe/honest error-state renderers exist")
test("renderExecError function defined", "function renderExecError(" in html)
test("renderOpsError function defined", "function renderOpsError(" in html)
test("exec error path never claims healthy", "Unable to load the executive brief" in html)
test("ops error path never claims an empty queue is 'all clear'", "Never shown as an empty/all-clear queue" in html)

print("\n\U0001f9ea Test 10: GET /api/operations/demo returns the centralized demo-fixture schema")
resp = client.get("/api/operations/demo")
test("200 OK", resp.status_code == 200)
demo = resp.get_json()
test("has meta/snapshot/brief/queue/handoff/analysis_example/briefing_example keys",
     set(demo.keys()) == {"meta", "snapshot", "brief", "queue", "handoff", "analysis_example", "briefing_example"})
test("meta.demo is True", demo["meta"]["demo"] is True)

print("\n\U0001f9ea Test 11: every /api/operations/... route string used by the frontend is actually registered")
registered_rules = {rule.rule for rule in app.url_map.iter_rules()}
frontend_operations_calls = set(re.findall(r"['\"`](/api/operations/[a-zA-Z0-9_\-{}/<>]*)", html))
# Strip JS template-literal interpolation (${...}) down to a <param> shape so it
# can be compared against Flask's own <finding_id>-style rule syntax.
normalized_calls = {re.sub(r"\$\{[^}]*\}", "<param>", call).split("?")[0] for call in frontend_operations_calls}
expected_routes = {
    "/api/operations/snapshot", "/api/operations/brief", "/api/operations/queue", "/api/operations/handoff",
    "/api/operations/demo", "/api/operations/analyze", "/api/operations/briefing",
}
for route in expected_routes:
    test(f"frontend references {route!r} and it is registered", route in normalized_calls and route in registered_rules)
test("frontend references the evidence route (dynamic finding id)", any(
    call.startswith("/api/operations/evidence/") for call in normalized_calls
))
test("frontend references the workflow PATCH route (dynamic finding id)", any(
    call.startswith("/api/operations/findings/") for call in normalized_calls
))
test("evidence/findings routes are registered on the Flask app", any(
    r.startswith("/api/operations/evidence/") for r in registered_rules
) and any(r.startswith("/api/operations/findings/") for r in registered_rules))

print("\n\U0001f9ea Test 12: pre-existing routes are preserved unchanged")
for route in ("/api/health", "/api/demos", "/api/ask", "/api/ask/stream", "/api/scan/overview", "/api/chaos/create",
              "/api/ado/proposals", "/api/ado/inspect-and-propose"):
    test(f"{route} still registered", route in registered_rules)

_cleanup_db()

print(f"\n{'='*60}\nResults: {PASS} passed, {FAIL} failed\n{'='*60}")
sys.exit(1 if FAIL else 0)
