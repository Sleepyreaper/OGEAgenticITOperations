#!/usr/bin/env python3
"""Test the bounded collection orchestrator (app/operations/service.py) --
in particular, that one source's failure surfaces as that source's own
'error' envelope and never erases another source's successful results,
and that 'not_configured' is distinguishable from both 'ok' and 'error'.

All Azure calls are injected fakes; no real network/API calls are made.

Run: python3 tests/test_operations_service.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations import service  # noqa: E402
from app.operations.config import OperationsConfig, OperationsConfigError  # noqa: E402

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


NOW = datetime.now(timezone.utc)


class FakeToken:
    token = "fake-token"  # noqa: S105


class FakeCredential:
    def get_token(self, scope):
        return FakeToken()


class FailingCredential:
    def get_token(self, scope):
        raise RuntimeError("no managed identity available")


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


ALERT_PAYLOAD = {"value": [{
    "id": "alertId1", "name": "alert1",
    "properties": {"essentials": {
        "severity": "Sev2", "monitorCondition": "Fired", "alertState": "New",
        "targetResource": "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
        "targetResourceName": "vm1", "startDateTime": NOW.isoformat(), "lastModifiedDateTime": NOW.isoformat(),
        "monitorService": "Platform", "alertRule": "Disk pressure",
    }},
}]}
COMPUTE_PAYLOAD = {"value": [{"name": {"value": "cores"}, "currentValue": 50, "limit": 100, "unit": "Count"}]}


def make_http_get(alerts_status=200):
    def _fake_http_get(url, *, headers, params=None, timeout=30):
        if "AlertsManagement" in url:
            return FakeResponse(ALERT_PAYLOAD if alerts_status == 200 else {}, status_code=alerts_status, text="boom")
        return FakeResponse(COMPUTE_PAYLOAD)
    return _fake_http_get


# ─── CollectionEnvelope -- explicit status ─────────────────────────────
print("\n\U0001f9ea Test 1: CollectionEnvelope -- status validation")
try:
    service.CollectionEnvelope(source="x", status="bogus", collected_at=NOW.isoformat())
    test("an unrecognized status raises ValueError", False)
except ValueError:
    test("an unrecognized status raises ValueError", True)

try:
    service.CollectionEnvelope(source="x", status="error", collected_at=NOW.isoformat())
    test("status='error' without an error message raises ValueError", False)
except ValueError:
    test("status='error' without an error message raises ValueError", True)


# ─── run_collection -- happy path across all four sources ─────────────
print("\n\U0001f9ea Test 2: run_collection -- all four sources, in a fixed order")
envelopes = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(), query_logs_fn=lambda *a, **k: [],
)
sources = [env.source for env in envelopes]
test("returns exactly 4 envelopes", len(envelopes) == 4)
test("sources are alerts, change/health, capacity, workload_slo in that order", sources == [
    "azure_monitor_alerts", "activity_log_change_health", "capacity", "workload_slo",
])
test("alerts source is ok with one Finding", envelopes[0].status == "ok" and len(envelopes[0].findings) == 1)
test("change/health source is ok with zero findings (no rows injected)", envelopes[1].status == "ok" and envelopes[1].findings == [])
test("capacity source is ok", envelopes[2].status == "ok")
test("SLO source is not_configured (no SLO_DEFINITIONS_* set)", envelopes[3].status == "not_configured")
test("a not_configured envelope still carries an explanatory error message", bool(envelopes[3].error))


# ─── run_collection -- one source fails, others are unaffected ────────
print("\n\U0001f9ea Test 3: run_collection -- alerts fails (HTTP 500); other sources still complete")
envelopes_partial = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(alerts_status=500), query_logs_fn=lambda *a, **k: [],
)
by_source = {env.source: env for env in envelopes_partial}
test("the failing alerts source reports status='error'", by_source["azure_monitor_alerts"].status == "error")
test("the failing source's error message is populated", bool(by_source["azure_monitor_alerts"].error))
test("the failing source has no findings (never a partial/guessed result)", by_source["azure_monitor_alerts"].findings == [])
test("the change/health source is unaffected (still ok)", by_source["activity_log_change_health"].status == "ok")
test("the capacity source is unaffected (still ok)", by_source["capacity"].status == "ok")
test("the SLO source is unaffected (still not_configured)", by_source["workload_slo"].status == "not_configured")

print("\n\U0001f9ea Test 4: run_collection -- capacity auth failure is isolated from alerts")
envelopes_capacity_fail = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FailingCredential, http_get=make_http_get(), query_logs_fn=lambda *a, **k: [],
)
by_source2 = {env.source: env for env in envelopes_capacity_fail}
test("alerts also fails since the same credential_factory is shared (both need ARM auth)", by_source2["azure_monitor_alerts"].status == "error")
test("capacity also reports error (same failing credential)", by_source2["capacity"].status == "error")
test("change/health does not use ARM auth, so it is unaffected", by_source2["activity_log_change_health"].status == "ok")


# ─── run_collection -- capacity 'not_configured' when no locations given ──
print("\n\U0001f9ea Test 5: run_collection -- capacity is not_configured (not error) with no locations")
envelopes_no_locations = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=[],
    credential_factory=FakeCredential, http_get=make_http_get(), query_logs_fn=lambda *a, **k: [],
)
capacity_env = next(env for env in envelopes_no_locations if env.source == "capacity")
test("no locations -> capacity is not_configured, not error", capacity_env.status == "not_configured")


# ─── run_collection -- SLO misconfiguration surfaces as error, not a crash ──
print("\n\U0001f9ea Test 6: run_collection -- malformed SLO_DEFINITIONS_JSON surfaces as an error envelope")
bad_config = OperationsConfig(slo_definitions_json="{not valid json")
envelopes_bad_slo = service.run_collection(
    ["sub1"], config=bad_config, locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(), query_logs_fn=lambda *a, **k: [],
)
slo_env = next(env for env in envelopes_bad_slo if env.source == "workload_slo")
test("malformed SLO config -> status error (not a raised exception that kills the whole run)", slo_env.status == "error")
test("the other three sources are unaffected by the SLO misconfiguration", all(
    env.status != "error" or env.source == "workload_slo" for env in envelopes_bad_slo
))


# ─── all_findings -- the combine-later hook ────────────────────────────
print("\n\U0001f9ea Test 7: all_findings -- flattens every envelope's findings for later combination")
flat = service.all_findings(envelopes)
test("flattens findings across all ok envelopes", len(flat) == len(envelopes[0].findings) + len(envelopes[2].findings))


# ─── OperationsConfig -- rejects both SLO sources set at once ──────────
print("\n\U0001f9ea Test 8: OperationsConfig -- rejects setting both SLO_DEFINITIONS_PATH and SLO_DEFINITIONS_JSON")
try:
    OperationsConfig(slo_definitions_path="config/slo_definitions.example.json", slo_definitions_json="{}")
    test("setting both SLO sources raises OperationsConfigError", False)
except OperationsConfigError:
    test("setting both SLO sources raises OperationsConfigError", True)


# ─── Malformed upstream data (ValueError/TypeError) is contained to its
# own source's error envelope -- never an unhandled exception that
# escapes run_collection/run_full_collection (see app.operations.
# service._collect_envelope's _EXPECTED_SOURCE_FAILURES). Previously
# these envelope functions only caught OperationsCollectionError, so a
# malformed timestamp/record from Azure (not an API/auth failure, but a
# normalization failure) would raise ValueError/TypeError straight
# through run_collection and crash the whole /snapshot route instead of
# being reported as just this one source's own 'error' envelope.
print("\n\U0001f9ea Test 9: run_collection -- a malformed (non-ISO) alert timestamp is contained to the alerts source alone")
MALFORMED_ALERT_PAYLOAD = {"value": [{
    "id": "alertId-bad", "name": "alert-bad",
    "properties": {"essentials": {
        "severity": "Sev2", "monitorCondition": "Fired", "alertState": "New",
        "targetResource": "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
        "targetResourceName": "vm1", "startDateTime": "not-a-real-timestamp", "lastModifiedDateTime": "not-a-real-timestamp",
        "monitorService": "Platform", "alertRule": "Disk pressure",
    }},
}]}


def make_http_get_malformed_alert():
    def _fake_http_get(url, *, headers, params=None, timeout=30):
        if "AlertsManagement" in url:
            return FakeResponse(MALFORMED_ALERT_PAYLOAD)
        return FakeResponse(COMPUTE_PAYLOAD)
    return _fake_http_get


envelopes_bad_alert = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get_malformed_alert(), query_logs_fn=lambda *a, **k: [],
)
by_source3 = {env.source: env for env in envelopes_bad_alert}
test("a malformed (non-ISO) alert timestamp surfaces as this source's own error envelope (ValueError, not a raised exception)", by_source3["azure_monitor_alerts"].status == "error")
test("the error message reflects the malformed-timestamp ValueError", "invalid ISO-8601 timestamp" in (by_source3["azure_monitor_alerts"].error or ""))
test("the unrelated capacity source is unaffected, still ok", by_source3["capacity"].status == "ok")
test("the unrelated change/health source is unaffected, still ok", by_source3["activity_log_change_health"].status == "ok")


print("\n\U0001f9ea Test 10: run_collection -- a malformed (non-ISO) change-timeline row is contained to the change/health source alone")


def malformed_change_query_logs(query, workspace_id, timespan):
    if "Administrative" in query:
        return [{
            "TimeGenerated": "also-not-a-timestamp", "OperationNameValue": "Microsoft.Compute/virtualMachines/write",
            "ActivityStatusValue": "Succeeded", "ResourceGroup": "rg1", "ResourceId": "", "Caller": "user@example.com",
            "CorrelationId": "c1",
        }]
    return []


envelopes_bad_change = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(), query_logs_fn=malformed_change_query_logs,
)
by_source4 = {env.source: env for env in envelopes_bad_change}
test("a malformed (non-ISO) change-timeline timestamp surfaces as this source's own error envelope", by_source4["activity_log_change_health"].status == "error")
test("the unrelated alerts source is unaffected, still ok", by_source4["azure_monitor_alerts"].status == "ok")
test("the unrelated capacity source is unaffected, still ok", by_source4["capacity"].status == "ok")


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
