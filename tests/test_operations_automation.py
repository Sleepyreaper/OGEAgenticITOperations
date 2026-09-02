#!/usr/bin/env python3
"""Test Azure Automation failed/suspended job normalization
(app/operations/collectors/automation.py) -- severity by status, the
$filter-based lookback, and explicit failure surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_automation.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import automation  # noqa: E402
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
ACCOUNT_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Automation/automationAccounts/aa1"

PAYLOAD = {"value": [
    {"id": f"{ACCOUNT_ID}/jobs/j1", "name": "j1", "properties": {
        "status": "Failed", "runbook": {"name": "Patch-VMs"}, "exception": "boom",
        "creationTime": (NOW - timedelta(hours=1)).isoformat(), "endTime": NOW.isoformat(),
    }},
    {"id": f"{ACCOUNT_ID}/jobs/j2", "name": "j2", "properties": {
        "status": "Completed", "runbook": {"name": "X"}, "creationTime": NOW.isoformat(),
    }},
    {"id": f"{ACCOUNT_ID}/jobs/j3", "name": "j3", "properties": {
        "status": "Suspended", "runbook": {"name": "Y"}, "creationTime": NOW.isoformat(),
    }},
]}


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
    assert params is not None and "$filter" in params, "collect_automation_failures must pass the lookback OData $filter"
    return FakeResponse(PAYLOAD)


# ─── Successful normalization + severity ───────────────────────────────
print("\n\U0001f9ea Test 1: collect_automation_failures -- only Failed/Suspended become Findings")
findings = automation.collect_automation_failures([ACCOUNT_ID], credential_factory=fake_credential_factory, http_get=http_get, now=NOW)
test("exactly 2 Findings (Completed is excluded)", len(findings) == 2)
by_runbook = {f.metadata["runbook_name"]: f for f in findings}
test("a Failed job is high severity", by_runbook["Patch-VMs"].severity == "high")
test("a Suspended job is medium severity", by_runbook["Y"].severity == "medium")
test("a Failed job demands executive attention", by_runbook["Patch-VMs"].executive_attention is True)
test("a Suspended job does not demand executive attention", by_runbook["Y"].executive_attention is False)
test("resource_id is the Automation Account (the addressable ARM resource)", by_runbook["Patch-VMs"].resource_id == ACCOUNT_ID)
test("all Findings use category automation", all(f.category == "automation" for f in findings))
test("the exception detail is included in the summary for a failed job", "boom" in by_runbook["Patch-VMs"].summary)
test("job id is used as the deterministic discriminator (evidence reference)", by_runbook["Patch-VMs"].evidence[0].reference == f"{ACCOUNT_ID}/jobs/j1")

# ─── Explicit failure surfacing ─────────────────────────────────────────
print("\n\U0001f9ea Test 2: explicit failures -- never an empty success-shaped list")


def http_get_500(url, *, headers, params=None, timeout=30):
    return FakeResponse({}, status_code=500, text="boom")


try:
    automation.collect_automation_failures([ACCOUNT_ID], credential_factory=fake_credential_factory, http_get=http_get_500)
    test("a 500 response raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a 500 response raises OperationsCollectionError instead of returning []", True)

try:
    automation.collect_automation_failures([], credential_factory=fake_credential_factory, http_get=http_get)
    test("an empty automation_account_ids list raises ValueError", False)
except ValueError:
    test("an empty automation_account_ids list raises ValueError", True)

try:
    automation.collect_automation_failures([ACCOUNT_ID], lookback_hours=0, credential_factory=fake_credential_factory, http_get=http_get)
    test("a non-positive lookback_hours raises ValueError", False)
except ValueError:
    test("a non-positive lookback_hours raises ValueError", True)


# ─── Multipage/bounded pagination -- follows nextLink, honors an injected bound ──
print("\n\U0001f9ea Test 3: collect_automation_failures -- follows nextLink across multiple pages, and honors an injected max_pages bound")
JOBS_PAGE2_URL = f"https://management.azure.com{ACCOUNT_ID}/jobs?api-version=x&page=2"


def multipage_http_get(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [{"id": f"{ACCOUNT_ID}/jobs/j4", "name": "j4", "properties": {
            "status": "Failed", "runbook": {"name": "Page2-Runbook"}, "creationTime": NOW.isoformat(),
        }}]})
    return FakeResponse({"value": PAYLOAD["value"], "nextLink": JOBS_PAGE2_URL})


multipage_findings = automation.collect_automation_failures([ACCOUNT_ID], credential_factory=fake_credential_factory, http_get=multipage_http_get, now=NOW)
test("collects Failed/Suspended jobs from both pages (2 from page 1, 1 from page 2)", len(multipage_findings) == 3)
test("the page-2 job is present by runbook name", any(f.metadata["runbook_name"] == "Page2-Runbook" for f in multipage_findings))


def endless_http_get(url, *, headers, params=None, timeout=30):
    return FakeResponse({"value": [{"id": f"{ACCOUNT_ID}/jobs/endless", "name": "endless", "properties": {
        "status": "Failed", "runbook": {"name": "Endless"}, "creationTime": NOW.isoformat(),
    }}], "nextLink": "https://management.azure.com/next?page=999"})


bounded_findings = automation.collect_automation_failures(
    [ACCOUNT_ID], credential_factory=fake_credential_factory, http_get=endless_http_get, now=NOW, max_pages=2, max_records=100,
)
test("an injected max_pages bound stops a runaway nextLink chain at exactly 2 pages", len(bounded_findings) == 2)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
