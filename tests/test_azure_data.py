#!/usr/bin/env python3
"""Test app/azure_data.py's Log Analytics column-shape tolerance
(`query_logs`), the never-fabricate-a-percentage Azure Policy compliance
helper (`compute_policy_compliance_pct`/`get_policy_compliance_summary`),
and the REST-based Azure Advisor recommendations fetch
(`get_advisor_recommendations`, which must never import
`azure.mgmt.advisor` -- that package is not a dependency of this
project).

`app.azure_data`'s functions instantiate their Azure clients internally
(no dependency-injection parameter, unlike app/operations/collectors/*.py)
-- these tests use `unittest.mock.patch` on `_credential`/the SDK
client class/`requests.get`/`requests.post` instead. No real Azure/
network calls are made.

Run: python3 tests/test_azure_data.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import azure_data  # noqa: E402

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


class FakeCredential:
    def get_token(self, *scopes, **kwargs):
        class Token:
            token = "fake-token"  # noqa: S105
        return Token()


# ─── query_logs -- tolerates BOTH LogsTable.columns shapes ─────────────
print("\n\U0001f9ea Test 1: query_logs -- azure-monitor-query's columns as plain strings (current SDK shape)")


class FakeTableStrCols:
    columns = ["TimeGenerated", "OperationNameValue"]
    rows = [["2026-01-01T00:00:00Z", "Microsoft.Compute/virtualMachines/write"]]


class FakeResponseStrCols:
    tables = [FakeTableStrCols()]


class FakeLogsQueryClientStrCols:
    def __init__(self, credential):
        pass

    def query_workspace(self, workspace_id, query, *, timespan):
        return FakeResponseStrCols()


with patch("app.azure_data._credential", return_value=FakeCredential()), \
     patch("app.azure_data.LogsQueryClient", FakeLogsQueryClientStrCols):
    rows = azure_data.query_logs("AzureActivity | take 1", workspace_id="ws1")
test("plain-string columns (List[str]) never raise 'str' object has no attribute 'name'", rows == [
    {"TimeGenerated": "2026-01-01T00:00:00Z", "OperationNameValue": "Microsoft.Compute/virtualMachines/write"},
])

print("\n\U0001f9ea Test 2: query_logs -- a column-object shape exposing .name (older/alternate SDK shape) still works")


class FakeColumnObj:
    def __init__(self, name):
        self.name = name


class FakeTableObjCols:
    columns = [FakeColumnObj("TimeGenerated"), FakeColumnObj("OperationNameValue")]
    rows = [["2026-01-01T00:00:00Z", "Microsoft.Compute/virtualMachines/write"]]


class FakeResponseObjCols:
    tables = [FakeTableObjCols()]


class FakeLogsQueryClientObjCols:
    def __init__(self, credential):
        pass

    def query_workspace(self, workspace_id, query, *, timespan):
        return FakeResponseObjCols()


with patch("app.azure_data._credential", return_value=FakeCredential()), \
     patch("app.azure_data.LogsQueryClient", FakeLogsQueryClientObjCols):
    rows2 = azure_data.query_logs("AzureActivity | take 1", workspace_id="ws1")
test("column objects exposing .name still normalize correctly", rows2 == [
    {"TimeGenerated": "2026-01-01T00:00:00Z", "OperationNameValue": "Microsoft.Compute/virtualMachines/write"},
])


# ─── compute_policy_compliance_pct -- never a fabricated/negative % ────
print("\n\U0001f9ea Test 3: compute_policy_compliance_pct -- never divides by a fabricated denominator")
test(
    "totalResources == 0 with nonCompliantResources > 0 -> (None, None), never -5000.0%",
    azure_data.compute_policy_compliance_pct(0, 51) == (None, None),
)
test(
    "non_compliant_resources > total_resources (inconsistent counts) -> (None, None)",
    azure_data.compute_policy_compliance_pct(10, 15) == (None, None),
)
test(
    "a normal case computes the correct compliant_resources/compliance_pct",
    azure_data.compute_policy_compliance_pct(100, 10) == (90, 90.0),
)
test(
    "fully compliant (0 non-compliant) -> 100.0%",
    azure_data.compute_policy_compliance_pct(50, 0) == (50, 100.0),
)

print("\n\U0001f9ea Test 4: get_policy_compliance_summary -- the real-world bug (totalResources=0, nonCompliantResources=51) never fabricates -5000.0%")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


ZERO_TOTAL_PAYLOAD = {"value": [{
    "results": {"totalPoliciesCount": 5, "nonCompliantPolicies": 2, "nonCompliantResources": 51, "totalResources": 0},
    "policyAssignments": [],
}]}


def fake_post_zero_total(url, headers, timeout=30):
    return FakeResponse(ZERO_TOTAL_PAYLOAD)


with patch("app.azure_data._credential", return_value=FakeCredential()), \
     patch("requests.post", fake_post_zero_total):
    summary = azure_data.get_policy_compliance_summary("sub1")
test("compliance_pct is None, never a fabricated/negative percentage", summary["compliance_pct"] is None)
test("compliant_resources is None (unknown), never a negative count", summary["compliant_resources"] is None)
test("the raw non_compliant_resources count is still reported, honestly, as-is", summary["non_compliant_resources"] == 51)
test("the raw total_resources count is still reported, honestly, as-is", summary["total_resources"] == 0)


# ─── get_advisor_recommendations -- ARM REST, never azure.mgmt.advisor ──
print("\n\U0001f9ea Test 5: get_advisor_recommendations -- ARM REST call, no azure-mgmt-advisor dependency")
_azure_data_source = Path(azure_data.__file__).read_text()
test(
    "azure.mgmt.advisor is never imported by app.azure_data (module docstring may still mention it as context)",
    "import azure.mgmt.advisor" not in _azure_data_source and "from azure.mgmt.advisor" not in _azure_data_source,
)
test("azure.mgmt.advisor is not an importable module in this environment (confirms requirements.txt intentionally omits it)", not __import__("importlib").util.find_spec("azure.mgmt.advisor"))

ADVISOR_PAGE1 = {
    "value": [{
        "id": "/subscriptions/s/.../recommendations/r1", "name": "r1",
        "properties": {
            "category": "Cost", "impact": "High",
            "shortDescription": {"problem": "Idle resource", "solution": "Deallocate it"},
            "resourceMetadata": {"resourceId": "/subscriptions/s/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"},
        },
    }],
    "nextLink": "https://management.azure.com/subscriptions/s/providers/Microsoft.Advisor/recommendations?api-version=2023-01-01&$skiptoken=abc",
}
ADVISOR_PAGE2 = {
    "value": [{
        "id": "/subscriptions/s/.../recommendations/r2", "name": "r2",
        "properties": {
            "category": "Security", "impact": "Medium",
            "shortDescription": {"problem": "MFA not enabled", "solution": "Enable MFA"},
            "resourceMetadata": {},
        },
    }],
}
_advisor_calls = []


def fake_get_advisor(url, headers, timeout=30):
    _advisor_calls.append(url)
    if "skiptoken" in url:
        return FakeResponse(ADVISOR_PAGE2)
    return FakeResponse(ADVISOR_PAGE1)


with patch("app.azure_data._credential", return_value=FakeCredential()), \
     patch("requests.get", fake_get_advisor):
    recs = azure_data.get_advisor_recommendations("sub1")
test("follows nextLink, collecting both pages' recommendations", len(recs) == 2)
test("normalizes category/impact/problem/solution/resource from the ARM REST shape", recs[0] == {
    "category": "Cost", "impact": "High", "problem": "Idle resource", "solution": "Deallocate it",
    "resource": "/subscriptions/s/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
})
test("a recommendation with no resourceMetadata gets an empty resource string, never a KeyError", recs[1]["resource"] == "")
test("the api-version is passed on the request URL", all("api-version=2023-01-01" in u for u in _advisor_calls))


def fake_get_advisor_error(url, headers, timeout=30):
    return FakeResponse({}, status_code=403)


try:
    with patch("app.azure_data._credential", return_value=FakeCredential()), \
         patch("requests.get", fake_get_advisor_error):
        azure_data.get_advisor_recommendations("sub1")
    test("a 403 response raises instead of returning a success-shaped []", False)
except Exception:
    test("a 403 response raises instead of returning a success-shaped []", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
