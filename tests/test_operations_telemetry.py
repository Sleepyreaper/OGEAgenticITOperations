#!/usr/bin/env python3
"""Test telemetry coverage-gap collectors
(app/operations/collectors/telemetry.py) -- diagnostic-settings gap
detection, heartbeat gap detection, the explicit coverage denominator
(TelemetryCoverageSummary), partial permission-failure resilience, and
total-failure surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_telemetry.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import telemetry  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402

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


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
RESOURCES = ["/subscriptions/s/rg/vm1", "/subscriptions/s/rg/vm2", "/subscriptions/s/rg/vm3"]


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeCredential:
    def get_token(self, scope):
        class Token:
            token = "fake-token"  # noqa: S105
        return Token()


def fake_credential_factory():
    return FakeCredential()


def http_get(url, *, headers, params=None, timeout=30):
    if "vm1" in url:
        return FakeResponse({"value": [{"name": "toLogAnalytics"}]})
    if "vm2" in url:
        return FakeResponse({"value": []})
    return FakeResponse({}, status_code=403, text="Forbidden")


# ─── Diagnostic settings coverage -- gap detection + coverage denominator ──
print("\n\U0001f9ea Test 1: collect_diagnostic_settings_coverage -- gap Finding + explicit coverage denominator")
findings, summary = telemetry.collect_diagnostic_settings_coverage(RESOURCES, credential_factory=fake_credential_factory, http_get=http_get, now=NOW)
test("vm1 (has settings) produces no gap Finding", not any("vm1" in f.title for f in findings if "diagnostic settings" in f.title.lower()))
test("vm2 (empty settings) produces exactly one gap Finding", any("vm2" in f.title for f in findings))
gap_finding = next(f for f in findings if "vm2" in f.title)
test("the gap Finding uses category telemetry", gap_finding.category == "telemetry")
test("the gap Finding is medium severity", gap_finding.severity == "medium")
test("vm3's permission failure produces one aggregate permission-gap Finding, not a per-resource one", sum("Cannot check diagnostic settings" in f.title for f in findings) == 1)
test("checked_count excludes the permission-failed resource (2 of 3 actually checked)", summary.checked_count == 2)
test("covered_count reflects only vm1 (has settings)", summary.covered_count == 1)
test("skipped_permission_errors reflects vm3's failure", summary.skipped_permission_errors == 1)
test("coverage_pct is computed from checked/covered, not the raw resource count", summary.coverage_pct == 50.0)
test("gap_type is 'diagnostic_settings'", summary.gap_type == "diagnostic_settings")

# ─── Heartbeat coverage -- gap detection + coverage denominator ────────
print("\n\U0001f9ea Test 2: collect_heartbeat_coverage -- gap Finding + explicit coverage denominator")


def query_logs_fn(query, workspace_id, timespan):
    return [{"ResourceId": "/subscriptions/s/rg/vm1"}]


hb_findings, hb_summary = telemetry.collect_heartbeat_coverage(RESOURCES, query_logs_fn=query_logs_fn, now=NOW)
test("vm2 and vm3 (no heartbeat row) each produce a gap Finding", len(hb_findings) == 2)
test("all heartbeat gap Findings use category telemetry", all(f.category == "telemetry" for f in hb_findings))
test("heartbeat coverage denominator: checked_count is the full resource set (3)", hb_summary.checked_count == 3)
test("heartbeat coverage denominator: covered_count reflects only vm1", hb_summary.covered_count == 1)
test("gap_type is 'heartbeat'", hb_summary.gap_type == "heartbeat")

# ─── Total failure -- every resource fails ─────────────────────────────
print("\n\U0001f9ea Test 3: total failure (every resource's diagnostic-settings check fails) raises, not an empty success")


def http_get_all_403(url, *, headers, params=None, timeout=30):
    return FakeResponse({}, status_code=403, text="Forbidden")


try:
    telemetry.collect_diagnostic_settings_coverage(RESOURCES, credential_factory=fake_credential_factory, http_get=http_get_all_403)
    test("total permission failure across every resource raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("total permission failure across every resource raises OperationsCollectionError", True)


def failing_query(query, workspace_id, timespan):
    raise RuntimeError("workspace unreachable")


try:
    telemetry.collect_heartbeat_coverage(RESOURCES, query_logs_fn=failing_query)
    test("a failing Heartbeat query raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a failing Heartbeat query raises OperationsCollectionError instead of returning []", True)

try:
    telemetry.collect_diagnostic_settings_coverage([], credential_factory=fake_credential_factory, http_get=http_get)
    test("an empty resource_ids list raises ValueError", False)
except ValueError:
    test("an empty resource_ids list raises ValueError", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
