#!/usr/bin/env python3
"""Test Azure Monitor alert normalization (app/operations/collectors/alerts.py).

No real network/Azure calls -- credential_factory and http_get are
injected fakes, matching the DI contract in
app/operations/collectors/http.py.

Run: python3 tests/test_operations_alerts.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import alerts  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402
from app.operations.models import ConfidenceLevel, FindingCategory, FindingStatus, Severity  # noqa: E402

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


def make_raw_alert(**overrides):
    essentials = dict(
        severity="Sev1",
        monitorCondition="Fired",
        alertState="New",
        targetResource="/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
        targetResourceName="vm1",
        targetResourceType="Microsoft.Compute/virtualMachines",
        startDateTime=NOW.isoformat(),
        lastModifiedDateTime=NOW.isoformat(),
        monitorService="Platform",
        alertRule="High CPU",
        description="CPU > 90% for 10 minutes",
        signalType="Metric",
    )
    essentials.update(overrides.pop("essentials", {}))
    payload = {"id": "/subscriptions/sub1/.../alertRules/high-cpu", "name": "alert1", "properties": {"essentials": essentials}}
    payload.update(overrides)
    return payload


class FakeToken:
    token = "fake-token"  # noqa: S105 -- test fixture, not a real credential


class FakeCredential:
    def get_token(self, scope):
        return FakeToken()


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def fake_http_get_factory(payload, status_code=200):
    def _fake_http_get(url, *, headers, params=None, timeout=30):
        return FakeResponse(payload, status_code=status_code)
    return _fake_http_get


# ─── normalize_alert ──────────────────────────────────────────────────
print("\n\U0001f9ea Test 1: normalize_alert -- Fired Sev1 alert")
finding = alerts.normalize_alert(make_raw_alert())
test("category is incident", finding.category == FindingCategory.INCIDENT.value)
test("Sev1 maps to high severity", finding.severity == Severity.HIGH.value)
test("Fired + New -> status open", finding.status == FindingStatus.OPEN.value)
test("confidence is confirmed (a platform-reported fact)", finding.confidence == ConfidenceLevel.CONFIRMED.value)
test("resource_id captures targetResource", finding.resource_id.endswith("/vm1"))
test("affected_resource_count is 1 when a resource id is present", finding.affected_resource_count == 1)
test("executive_attention is True for an open high-severity alert", finding.executive_attention is True)
test("evidence includes exactly one EvidenceReference", len(finding.evidence) == 1)
test("owner defaults to empty string with no lookup supplied", finding.owner == "")

print("\n\U0001f9ea Test 2: normalize_alert -- severity mapping across all Sev0-Sev4")
expected = {
    "Sev0": Severity.CRITICAL, "Sev1": Severity.HIGH, "Sev2": Severity.MEDIUM,
    "Sev3": Severity.LOW, "Sev4": Severity.INFORMATIONAL,
}
for raw_sev, expected_sev in expected.items():
    f = alerts.normalize_alert(make_raw_alert(essentials={"severity": raw_sev}))
    test(f"{raw_sev} -> {expected_sev.value}", f.severity == expected_sev.value)

try:
    alerts.normalize_alert(make_raw_alert(essentials={"severity": "Sev99"}))
    test("an unrecognized severity raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("an unrecognized severity raises OperationsCollectionError", True)

print("\n\U0001f9ea Test 3: normalize_alert -- resolved and acknowledged states")
resolved = alerts.normalize_alert(make_raw_alert(essentials={"monitorCondition": "Resolved"}))
test("Resolved monitor condition -> status resolved", resolved.status == FindingStatus.RESOLVED.value)
test("a resolved alert never demands executive attention", resolved.executive_attention is False)
test("a resolved alert has no recommended_action", resolved.recommended_action == "")

acknowledged = alerts.normalize_alert(make_raw_alert(essentials={"alertState": "Acknowledged"}))
test("Fired + Acknowledged -> status acknowledged", acknowledged.status == FindingStatus.ACKNOWLEDGED.value)

print("\n\U0001f9ea Test 4: normalize_alert -- owner tag lookup (dependency-injected)")
lookup_calls = []


def owner_lookup(resource_id):
    lookup_calls.append(resource_id)
    return "platform-team" if resource_id.endswith("/vm1") else ""


owned = alerts.normalize_alert(make_raw_alert(), resource_owner_lookup=owner_lookup)
test("owner is populated from the injected lookup", owned.owner == "platform-team")
test("the lookup was called with the alert's resource_id", lookup_calls == [owned.resource_id])

print("\n\U0001f9ea Test 5: normalize_alert -- missing required fields raise explicitly")
try:
    bad_payload = make_raw_alert()
    bad_payload["id"] = ""
    bad_payload["name"] = ""
    alerts.normalize_alert(bad_payload)
    test("a missing alert id raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("a missing alert id raises OperationsCollectionError", True)

try:
    alerts.normalize_alert(make_raw_alert(essentials={"startDateTime": None}))
    test("a missing startDateTime raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("a missing startDateTime raises OperationsCollectionError", True)


# ─── collect_fired_alerts -- DI + explicit errors ──────────────────────
print("\n\U0001f9ea Test 6: collect_fired_alerts -- happy path via injected fakes (no real network)")
payload = {"value": [make_raw_alert()]}
findings = alerts.collect_fired_alerts(
    "sub1", lookback_hours=24, credential_factory=FakeCredential, http_get=fake_http_get_factory(payload), now=NOW,
)
test("returns one normalized Finding", len(findings) == 1)
test("the Finding's id is deterministic", findings[0].id == alerts.normalize_alert(make_raw_alert()).id)

print("\n\U0001f9ea Test 7: collect_fired_alerts -- explicit error on a non-2xx response (never an empty success)")
try:
    alerts.collect_fired_alerts(
        "sub1", credential_factory=FakeCredential, http_get=fake_http_get_factory({}, status_code=500), now=NOW,
    )
    test("a 500 response raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError as exc:
    test("a 500 response raises OperationsCollectionError instead of returning []", True)
    test("the error message names the alerts source", exc.source == alerts.SOURCE)


class FailingCredential:
    def get_token(self, scope):
        raise RuntimeError("no managed identity available")


try:
    alerts.collect_fired_alerts("sub1", credential_factory=FailingCredential, http_get=fake_http_get_factory({}), now=NOW)
    test("an auth failure raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("an auth failure raises OperationsCollectionError instead of returning []", True)

print("\n\U0001f9ea Test 8: collect_fired_alerts -- filters out alerts outside the lookback window")
stale_payload = {"value": [make_raw_alert(essentials={
    "startDateTime": (NOW - timedelta(hours=48)).isoformat(),
    "lastModifiedDateTime": (NOW - timedelta(hours=48)).isoformat(),
})]}
stale_findings = alerts.collect_fired_alerts(
    "sub1", lookback_hours=24, credential_factory=FakeCredential, http_get=fake_http_get_factory(stale_payload), now=NOW,
)
test("an alert last modified outside the lookback window is excluded", len(stale_findings) == 0)

try:
    alerts.collect_fired_alerts("sub1", lookback_hours=0, credential_factory=FakeCredential, http_get=fake_http_get_factory(payload))
    test("a non-positive lookback_hours raises ValueError", False)
except ValueError:
    test("a non-positive lookback_hours raises ValueError", True)


print("\n\U0001f9ea Test 9: collect_fired_alerts -- follows nextLink across multiple pages (never just the first page)")
PAGE2_URL = "https://management.azure.com/subscriptions/sub1/providers/Microsoft.AlertsManagement/alerts?api-version=x&page=2"


def multipage_http_get(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [make_raw_alert(id="/subscriptions/sub1/.../alertRules/page2-alert", name="page2-alert")]})
    return FakeResponse({"value": [make_raw_alert()], "nextLink": PAGE2_URL})


multipage_findings = alerts.collect_fired_alerts(
    "sub1", credential_factory=FakeCredential, http_get=multipage_http_get, now=NOW,
)
test("both pages' alerts are collected, not just the first page", len(multipage_findings) == 2)

print("\n\U0001f9ea Test 10: collect_fired_alerts -- a bounded max_records/max_pages never runs away, and is honored end-to-end")


def endless_alerts_http_get(url, *, headers, params=None, timeout=30):
    return FakeResponse({"value": [make_raw_alert()], "nextLink": PAGE2_URL})


bounded_findings = alerts.collect_fired_alerts(
    "sub1", credential_factory=FakeCredential, http_get=endless_alerts_http_get, now=NOW, max_pages=2, max_records=100,
)
test("collect_fired_alerts honors an injected max_pages bound (stops at exactly 2 pages' worth of alerts, never runs away)", len(bounded_findings) == 2)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
