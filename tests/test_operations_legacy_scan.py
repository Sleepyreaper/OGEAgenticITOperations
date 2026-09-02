#!/usr/bin/env python3
"""Test the existing-scan adapter (app/operations/collectors/legacy_scan.py)
-- converting app/azure_data.py's raw scan signal shapes into structured
Findings, and collect_legacy_envelopes' per-source error isolation.

All azure_data.py calls are injected fakes; no real Azure calls are made.

Run: python3 tests/test_operations_legacy_scan.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import legacy_scan  # noqa: E402
from app.operations.models import FindingCategory, Severity  # noqa: E402

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


NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


# ─── resource_health_findings ───────────────────────────────────────────
print("\n\U0001f9ea Test 1: resource_health_findings -- only degraded/unavailable rows become Findings")
rows = [
    {"name": "vm1", "resourceGroup": "rg1", "type": "Microsoft.Compute/virtualMachines", "status": "Available", "summary": "", "title": "", "location": "eastus"},
    {"name": "vm2", "resourceGroup": "rg1", "type": "Microsoft.Compute/virtualMachines", "status": "Degraded", "summary": "disk pressure", "title": "VM degraded", "location": "eastus"},
    {"name": "vm3", "resourceGroup": "rg2", "type": "Microsoft.Compute/virtualMachines", "status": "Unavailable", "summary": "", "title": "", "location": "eastus"},
]
findings = legacy_scan.resource_health_findings(rows, subscription_id="sub1", now=NOW)
test("only 2 of 3 rows (degraded/unavailable) become Findings", len(findings) == 2)
test("category is reliability", all(f.category == FindingCategory.RELIABILITY.value for f in findings))
test("Unavailable maps to HIGH severity", next(f for f in findings if "vm3" in (f.resource_id or "")).severity == Severity.HIGH.value)
test("Degraded maps to MEDIUM severity", next(f for f in findings if "vm2" in (f.resource_id or "")).severity == Severity.MEDIUM.value)
test("resource_id is correctly constructed", findings[0].resource_id == "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm2")
test("HIGH severity finding has executive_attention", next(f for f in findings if f.severity == Severity.HIGH.value).executive_attention is True)

# Deterministic ID: re-running with the same rows produces the same ids
findings2 = legacy_scan.resource_health_findings(rows, subscription_id="sub1", now=NOW)
test("same input -> same deterministic ids across runs", {f.id for f in findings} == {f.id for f in findings2})


# ─── service_health_findings ─────────────────────────────────────────────
print("\n\U0001f9ea Test 2: service_health_findings -- active only, HealthAdvisory excluded")
events = [
    {"title": "Active incident", "status": "Active", "level": "Critical", "eventType": "Incident", "impactStart": "2025-05-31T00:00:00Z", "services": ["Storage"], "regions": ["eastus"], "summary": "outage"},
    {"title": "Resolved incident", "status": "Resolved", "level": "Warning", "eventType": "Incident", "impactStart": "2025-05-30T00:00:00Z", "services": ["Storage"], "regions": [], "summary": ""},
    {"title": "Retirement notice", "status": "Active", "level": "Warning", "eventType": "HealthAdvisory", "impactStart": "2025-05-01T00:00:00Z", "services": ["Compute"], "regions": [], "summary": "retiring"},
]
findings = legacy_scan.service_health_findings(events, subscription_id="sub1", now=NOW)
test("only the active, non-HealthAdvisory event becomes a Finding", len(findings) == 1)
test("category is incident", findings[0].category == "incident")
test("severity HIGH for Critical level", findings[0].severity == Severity.HIGH.value)
test("first_seen preserves impactStart", findings[0].first_seen == "2025-05-31T00:00:00.000Z")
test("last_seen is the collection time (still active)", findings[0].last_seen == "2025-06-01T00:00:00.000Z")


# ─── security_drift_findings ─────────────────────────────────────────────
print("\n\U0001f9ea Test 3: security_drift_findings -- CRITICAL for SSH/RDP/*, HIGH otherwise")
rows = [
    {"nsgName": "nsg1", "ruleName": "allow-ssh", "port": "22", "priority": 100, "resourceGroup": "rg1", "subscriptionId": "sub1"},
    {"nsgName": "nsg2", "ruleName": "allow-sql", "port": "1433", "priority": 100, "resourceGroup": "rg1", "subscriptionId": "sub1"},
]
findings = legacy_scan.security_drift_findings(rows, now=NOW)
test("2 findings produced", len(findings) == 2)
test("SSH (port 22) is CRITICAL", next(f for f in findings if "nsg1" in f.title).severity == Severity.CRITICAL.value)
test("SQL Server (port 1433) is HIGH", next(f for f in findings if "nsg2" in f.title).severity == Severity.HIGH.value)
test("all security drift findings require approval", all(f.approval_required for f in findings))
test("category is security", all(f.category == "security" for f in findings))


# ─── insecure_storage_findings ────────────────────────────────────────────
print("\n\U0001f9ea Test 4: insecure_storage_findings")
rows = [{"name": "sa1", "resourceGroup": "rg1", "location": "eastus", "publicAccess": True, "subscriptionId": "sub1"}]
findings = legacy_scan.insecure_storage_findings(rows, now=NOW)
test("1 finding produced", len(findings) == 1)
test("severity is HIGH", findings[0].severity == Severity.HIGH.value)
test("executive_attention is True", findings[0].executive_attention is True)
test("approval_required is True", findings[0].approval_required is True)


# ─── advisor_findings -- high-impact only ────────────────────────────────
print("\n\U0001f9ea Test 5: advisor_findings -- only impact == 'High' becomes a Finding")
recs = [
    {"category": "Cost", "impact": "Low", "problem": "minor", "solution": "ignore", "resource": ""},
    {"category": "Security", "impact": "High", "problem": "open port", "solution": "close it", "resource": "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Network/x"},
    {"category": "HighAvailability", "impact": "High", "problem": "single instance", "solution": "add a replica", "resource": ""},
]
findings = legacy_scan.advisor_findings(recs, subscription_id="sub1", now=NOW)
test("only 2 of 3 recs (impact == High) become Findings", len(findings) == 2)
test("Security category maps to FindingCategory.SECURITY", next(f for f in findings if "open port" in f.summary).category == "security")
test("HighAvailability category maps to FindingCategory.RELIABILITY", next(f for f in findings if "single instance" in f.summary).category == "reliability")
test("all advisor findings are executive_attention", all(f.executive_attention for f in findings))


# ─── policy_compliance_findings ──────────────────────────────────────────
print("\n\U0001f9ea Test 6: policy_compliance_findings -- summary + item Findings")
summary = {"total_resources": 100, "non_compliant_resources": 10, "non_compliant_policies": 2, "compliance_pct": 90.0}
items = [{"resourceId": "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/sa1", "resourceName": "sa1", "resourceType": "Microsoft.Storage/storageAccounts", "policyAssignmentName": "assign1", "policyDefinitionName": "def1", "policyDefinitionAction": "audit"}]
findings = legacy_scan.policy_compliance_findings(summary, items, subscription_id="sub1", now=NOW)
test("1 summary + 1 item = 2 findings", len(findings) == 2)
test("summary finding is MEDIUM severity (90%% >= 80%%)", findings[0].severity == Severity.MEDIUM.value)
test("item finding references the resource id", findings[1].resource_id == items[0]["resourceId"])

print("\n\U0001f9ea Test 6b: policy_compliance_findings -- no summary Finding when fully compliant")
summary_clean = {"total_resources": 100, "non_compliant_resources": 0, "non_compliant_policies": 0, "compliance_pct": 100.0}
findings_clean = legacy_scan.policy_compliance_findings(summary_clean, [], subscription_id="sub1", now=NOW)
test("no findings when fully compliant", findings_clean == [])

print("\n\U0001f9ea Test 6c: policy_compliance_findings -- HIGH severity summary below 80%% compliance")
summary_bad = {"total_resources": 100, "non_compliant_resources": 30, "non_compliant_policies": 5, "compliance_pct": 70.0}
findings_bad = legacy_scan.policy_compliance_findings(summary_bad, [], subscription_id="sub1", now=NOW)
test("HIGH severity below 80%% compliance", findings_bad[0].severity == Severity.HIGH.value)
test("executive_attention True for HIGH severity summary", findings_bad[0].executive_attention is True)


# ─── resource_hygiene_findings ────────────────────────────────────────────
print("\n\U0001f9ea Test 7: resource_hygiene_findings -- disks/NSGs/plans/subnets")
orphaned_disks = [{"name": "disk1", "resourceGroup": "rg1", "subscriptionId": "sub1", "properties.diskSizeGB": 128}]
deep_analysis = {
    "orphaned_nsgs": [{"name": "nsg1", "resourceGroup": "rg1", "subscriptionId": "sub1"}],
    "idle_app_service_plans": [{"name": "plan1", "resourceGroup": "rg1", "subscriptionId": "sub1", "tier": "Standard"}],
    "empty_subnets": [{"vnetName": "vnet1", "subnetName": "subnet1", "resourceGroup": "rg1", "subscriptionId": "sub1", "addressPrefix": "10.0.0.0/24"}],
}
findings = legacy_scan.resource_hygiene_findings(orphaned_disks=orphaned_disks, deep_analysis=deep_analysis, now=NOW)
test("4 hygiene findings produced (1 disk + 1 nsg + 1 plan + 1 subnet)", len(findings) == 4)
test("all hygiene findings are category cost", all(f.category == "cost" for f in findings))
test("disk finding preserves size in summary", "128" in next(f for f in findings if "disk1" in f.title).summary)
test("subnet finding is INFORMATIONAL severity", next(f for f in findings if "subnet1" in f.title).severity == Severity.INFORMATIONAL.value)
test("plan finding resource_id built correctly", next(f for f in findings if "plan1" in f.title).resource_id == "/subscriptions/sub1/resourceGroups/rg1/providers/Microsoft.Web/serverfarms/plan1")

print("\n\U0001f9ea Test 7b: resource_hygiene_findings -- defensive _get_any for unaliased disk columns")
disk_alt_keys = [{"name": "disk2", "resourceGroup": "rg1", "subscriptionId": "sub1", "diskSizeGB": 64}]
findings_alt = legacy_scan.resource_hygiene_findings(orphaned_disks=disk_alt_keys, deep_analysis={}, now=NOW)
test("falls back to the alternate 'diskSizeGB' key", "64" in findings_alt[0].summary)


# ─── ownership_findings ───────────────────────────────────────────────────
print("\n\U0001f9ea Test 8: ownership_findings -- only resource groups missing support-owner")
tagging_rows = [
    {"name": "rg1", "supportOwner": "team-a", "location": "eastus", "subscriptionId": "sub1"},
    {"name": "rg2", "supportOwner": None, "location": "eastus", "subscriptionId": "sub1"},
    {"name": "rg3", "supportOwner": "", "location": "eastus", "subscriptionId": "sub1"},
]
findings = legacy_scan.ownership_findings(tagging_rows, now=NOW)
test("2 of 3 resource groups are missing an owner", len(findings) == 2)
test("category is ownership", all(f.category == "ownership" for f in findings))
test("severity is LOW (not urgent, just hygiene)", all(f.severity == Severity.LOW.value for f in findings))


# ─── collect_legacy_envelopes -- 8 sources, fixed order, per-source isolation ──
print("\n\U0001f9ea Test 9: collect_legacy_envelopes -- 8 sources in fixed order, one failing source isolated")


def fake_resource_health(sub):
    return []


def fake_service_health(sub, days=30):
    return []


def fake_security_drift(subscription_ids=None):
    return []


def fake_insecure_storage(subscription_ids=None):
    return []


def failing_advisor(sub):
    raise RuntimeError("Advisor API unavailable")


def fake_policy_summary(sub):
    return {"total_resources": 0, "non_compliant_resources": 0}


def fake_non_compliant(sub):
    return []


def fake_orphaned_disks(subscription_ids=None):
    return []


def fake_deep_analysis(subscription_ids=None):
    return {}


def fake_tagging(subscription_ids=None):
    return []


envelopes = legacy_scan.collect_legacy_envelopes(
    ["sub1"],
    resource_health_fn=fake_resource_health, service_health_fn=fake_service_health,
    security_drift_fn=fake_security_drift, insecure_storage_fn=fake_insecure_storage,
    advisor_fn=failing_advisor, policy_summary_fn=fake_policy_summary, non_compliant_fn=fake_non_compliant,
    orphaned_disks_fn=fake_orphaned_disks, deep_analysis_fn=fake_deep_analysis, tagging_fn=fake_tagging,
    now=NOW,
)
sources = [e["source"] for e in envelopes]
test("returns exactly 8 envelopes", len(envelopes) == 8)
test("sources are in the documented fixed order", sources == [
    "legacy_resource_health", "legacy_service_health", "legacy_security_drift", "legacy_insecure_storage",
    "legacy_advisor", "legacy_policy_compliance", "legacy_resource_hygiene", "legacy_ownership",
])
by_source = {e["source"]: e for e in envelopes}
test("the failing advisor source reports status='error'", by_source["legacy_advisor"]["status"] == "error")
test("the failing source's error message is populated", bool(by_source["legacy_advisor"]["error"]))
test("the failing source has no findings", by_source["legacy_advisor"]["findings"] == [])
test("other sources are unaffected (still ok)", all(
    e["status"] == "ok" for e in envelopes if e["source"] != "legacy_advisor"
))

try:
    legacy_scan.collect_legacy_envelopes([])
    test("empty subscription_ids raises ValueError", False)
except ValueError:
    test("empty subscription_ids raises ValueError", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
