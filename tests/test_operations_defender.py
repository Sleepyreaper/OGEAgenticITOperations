#!/usr/bin/env python3
"""Test Microsoft Defender for Cloud alert/assessment normalization
(app/operations/collectors/defender.py) -- severity mapping, the
security/compliance category split, and that a non-2xx/auth failure
surfaces as OperationsCollectionError rather than an empty success.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_defender.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import defender  # noqa: E402
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


class FailingCredential:
    def get_token(self, scope):
        raise RuntimeError("no managed identity available")


def fake_credential_factory():
    return FakeCredential()


ALERT_PAYLOAD = {"value": [
    {"id": "alert1", "name": "alert1", "properties": {
        "status": "Active", "severity": "High", "alertDisplayName": "Suspicious activity",
        "description": "desc", "alertType": "VM_X", "startTimeUtc": "2026-01-01T00:00:00Z",
        "systemAlertId": "sys1", "compromisedEntity": "vm1",
        "resourceIdentifiers": [{"azureResourceId": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"}],
    }},
    {"id": "alert2", "name": "alert2", "properties": {"status": "Resolved", "severity": "High"}},
    {"id": "alert3", "name": "alert3", "properties": {"status": "Active", "severity": "Low"}},
]}

ASSESSMENT_PAYLOAD = {"value": [
    {"id": "/subscriptions/s/.../assessments/x", "name": "x", "properties": {
        "displayName": "Enable MFA", "status": {"code": "Unhealthy", "description": "not enabled"},
        "metadata": {"severity": "High", "categories": ["Identity"], "description": "desc", "remediationDescription": "enable it"},
        "resourceDetails": {"id": "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm2"},
    }},
    {"id": "/subscriptions/s/.../assessments/y", "name": "y", "properties": {
        "displayName": "Healthy one", "status": {"code": "Healthy"}, "metadata": {"severity": "Low"},
    }},
]}


def http_get_alerts(url, *, headers, params=None, timeout=30):
    return FakeResponse(ALERT_PAYLOAD)


def http_get_assessments(url, *, headers, params=None, timeout=30):
    return FakeResponse(ASSESSMENT_PAYLOAD)


# ─── collect_active_alerts -- only Active + High/Medium become Findings ──
print("\n\U0001f9ea Test 1: collect_active_alerts -- filters to Active + High/Medium, normalizes correctly")
alerts = defender.collect_active_alerts("sub1", credential_factory=fake_credential_factory, http_get=http_get_alerts)
test("returns exactly 1 Finding (Resolved and Low are excluded)", len(alerts) == 1)
test("category is security (an actual threat, not a posture recommendation)", alerts[0].category == "security")
test("severity is high", alerts[0].severity == "high")
test("resource_id captures the alert's azureResourceId", alerts[0].resource_id == "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1")
test("confidence is confirmed (a platform-reported fact)", alerts[0].confidence == "confirmed")
test("a High-severity active alert demands executive attention", alerts[0].executive_attention is True)
test("Finding.metadata never carries a raw secret/token value", "token" not in str(alerts[0].metadata).lower())

# ─── collect_unhealthy_assessments -- only Unhealthy become Findings, category compliance ──
print("\n\U0001f9ea Test 2: collect_unhealthy_assessments -- filters to Unhealthy, category compliance (never a Secure Score)")
assessments = defender.collect_unhealthy_assessments("sub1", credential_factory=fake_credential_factory, http_get=http_get_assessments, now="2026-01-10T00:00:00.000Z")
test("returns exactly 1 Finding (the Healthy one is excluded)", len(assessments) == 1)
test("category is compliance (a posture gap, distinct from security alerts)", assessments[0].category == "compliance")
test("severity is high (from metadata.severity)", assessments[0].severity == "high")
test("resource_id captures resourceDetails.id", assessments[0].resource_id == "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm2")
test("no 'secure_score'/opaque score field anywhere on the Finding", "secure_score" not in assessments[0].to_dict())

# ─── Severity mapping across all recognized values ─────────────────────
print("\n\U0001f9ea Test 3: severity mapping -- all Defender severities, and an unrecognized one raises")
for raw, expected in [("High", "high"), ("Medium", "medium"), ("Low", "low"), ("Informational", "informational")]:
    sev = defender._severity_from_raw(raw, source="x", context="ctx")
    test(f"{raw} -> {expected}", sev.value == expected)
try:
    defender._severity_from_raw("Extreme", source="x", context="ctx")
    test("an unrecognized severity raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("an unrecognized severity raises OperationsCollectionError", True)

# ─── Explicit failure surfacing -- never an empty success-shaped list ──
print("\n\U0001f9ea Test 4: a non-2xx response / auth failure raises OperationsCollectionError")


def http_get_500(url, *, headers, params=None, timeout=30):
    return FakeResponse({}, status_code=500, text="boom")


try:
    defender.collect_active_alerts("sub1", credential_factory=fake_credential_factory, http_get=http_get_500)
    test("a 500 response raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a 500 response raises OperationsCollectionError instead of returning []", True)

try:
    defender.collect_unhealthy_assessments("sub1", credential_factory=lambda: FailingCredential(), http_get=http_get_assessments)
    test("an auth failure raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("an auth failure raises OperationsCollectionError instead of returning []", True)

try:
    defender.collect_active_alerts("", credential_factory=fake_credential_factory, http_get=http_get_alerts)
    test("an empty subscription_id raises ValueError", False)
except ValueError:
    test("an empty subscription_id raises ValueError", True)


# ─── Multipage -- follows nextLink for both alerts and assessments ─────
print("\n\U0001f9ea Test 5: collect_active_alerts / collect_unhealthy_assessments -- follow nextLink across multiple pages")
ALERTS_PAGE2_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Security/alerts?api-version=x&page=2"
ASSESSMENTS_PAGE2_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Security/assessments?api-version=x&page=2"


def multipage_http_get_alerts(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [
            {"id": "alert-page2", "name": "alert-page2", "properties": {
                "status": "Active", "severity": "High", "alertDisplayName": "Page 2 alert",
                "startTimeUtc": "2026-01-01T00:00:00Z", "systemAlertId": "sys-page2",
            }},
        ]})
    return FakeResponse({"value": ALERT_PAYLOAD["value"], "nextLink": ALERTS_PAGE2_URL})


multipage_alerts = defender.collect_active_alerts("sub1", credential_factory=fake_credential_factory, http_get=multipage_http_get_alerts)
test("collects the page-1 Active/High alert AND the page-2 alert (both pages, not just the first)", len(multipage_alerts) == 2)


def multipage_http_get_assessments(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [
            {"id": "/subscriptions/s/.../assessments/page2", "name": "page2", "properties": {
                "displayName": "Page 2 recommendation", "status": {"code": "Unhealthy"},
                "metadata": {"severity": "Medium"},
            }},
        ]})
    return FakeResponse({"value": ASSESSMENT_PAYLOAD["value"], "nextLink": ASSESSMENTS_PAGE2_URL})


multipage_assessments = defender.collect_unhealthy_assessments(
    "sub1", credential_factory=fake_credential_factory, http_get=multipage_http_get_assessments, now="2026-01-10T00:00:00.000Z",
)
test("collects the page-1 Unhealthy assessment AND the page-2 assessment (both pages, not just the first)", len(multipage_assessments) == 2)


# ─── Bounded pagination -- an injected max_pages/max_records is honored ──
print("\n\U0001f9ea Test 6: collect_active_alerts -- an injected max_pages bound stops a runaway nextLink chain")


def endless_alerts_http_get(url, *, headers, params=None, timeout=30):
    return FakeResponse({"value": [
        {"id": "endless", "name": "endless", "properties": {
            "status": "Active", "severity": "High", "alertDisplayName": "Endless", "startTimeUtc": "2026-01-01T00:00:00Z",
        }},
    ], "nextLink": "https://management.azure.com/next?page=999"})


bounded_alerts = defender.collect_active_alerts("sub1", credential_factory=fake_credential_factory, http_get=endless_alerts_http_get, max_pages=2, max_records=100)
test("stops after exactly 2 pages instead of following the nextLink forever", len(bounded_alerts) == 2)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
