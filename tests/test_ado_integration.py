#!/usr/bin/env python3
"""Test the ADO integration module — proposal lifecycle.

Run: python3 tests/test_ado_integration.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ado_integration import (
    create_proposal, get_proposals, get_proposal,
    approve_proposal, reject_proposal,
    generate_proposals_from_inspection,
    ViolationClass, ProposalType, ProposalStatus,
    _proposals,  # access the store for cleanup
)

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


def reset():
    _proposals.clear()


# ─── Test 1: Create proposals from each violation class ────
print("\n🧪 Test 1: Create proposals — each violation class maps to correct type")
reset()

p1 = create_proposal(
    violation_class="policy_bug",
    policy_name="Deny-Storage-HTTP",
    resource_id="/subscriptions/xxx/resourceGroups/rg-data/providers/Microsoft.Storage/storageAccounts/legacysftp",
    support_owner="data-platform-team",
    inspector_reasoning="Policy doesn't account for legacy SFTP gateway pattern requiring HTTP.",
    title="Fix Deny-Storage-HTTP to exempt SFTP gateways",
    description="The built-in HTTPS-only policy flags storage accounts used for legacy SFTP. Create custom policy with exemption tag.",
    acceptance_criteria="Custom policy deployed. Legacy SFTP account marked compliant.",
    priority=2,
    pr_file_changes=[{"path": "policies/deny-storage-http.json", "content": "{...updated policy...}"}],
)
test("policy_bug → pull_request", p1.proposal_type == ProposalType.PULL_REQUEST.value)
test("has PR file changes", len(p1.pr_file_changes) == 1)
test("has source branch", p1.pr_source_branch.startswith("ops-council/policy-fix-"))

p2 = create_proposal(
    violation_class="misconfiguration",
    policy_name="Require-TLS-1.2",
    resource_id="/subscriptions/xxx/resourceGroups/rg-web/providers/Microsoft.Web/sites/legacy-app",
    support_owner="web-team",
    inspector_reasoning="App genuinely has TLS 1.0 enabled. No valid reason found.",
    title="Upgrade legacy-app to TLS 1.2",
    description="Web app is running TLS 1.0. Update minTlsVersion to '1.2'.",
    priority=1,
)
test("misconfiguration → bug", p2.proposal_type == ProposalType.WORK_ITEM_BUG.value)
test("priority 1 (critical)", p2.priority == 1)

p3 = create_proposal(
    violation_class="workaround_abuse",
    policy_name="Require-Workload-Identity",
    resource_id="/subscriptions/xxx/resourceGroups/rg-k8s/providers/Microsoft.ContainerService/managedClusters/aks-ci",
    support_owner="devops-team",
    inspector_reasoning="Exemption expired 6 months ago. CI/CD should have migrated to workload identity.",
    title="AKS cluster aks-ci: migrate to workload identity",
    description="Expired exemption for service principal auth. Create PBI to investigate and migrate.",
    acceptance_criteria="AKS cluster uses workload identity. Old SP deleted.",
    priority=2,
)
test("workaround_abuse → pbi", p3.proposal_type == ProposalType.WORK_ITEM_PBI.value)

p4 = create_proposal(
    violation_class="intentional_exemption",
    policy_name="Deny-Public-IP",
    resource_id="/subscriptions/xxx/resourceGroups/rg-dmz/providers/Microsoft.Network/publicIPAddresses/dmz-lb-ip",
    support_owner="network-team",
    inspector_reasoning="DMZ load balancer requires public IP by architecture design. Exemption is current.",
    title="Verify DMZ public IP exemption documentation",
    description="Confirm exemption is documented and reviewed within last 90 days.",
    priority=4,
)
test("intentional_exemption → task", p4.proposal_type == ProposalType.WORK_ITEM_TASK.value)
test("low priority", p4.priority == 4)


# ─── Test 2: List and filter proposals ─────────────────────
print("\n🧪 Test 2: List and filter proposals")

all_proposals = get_proposals()
test("4 proposals total", len(all_proposals) == 4)

pending = get_proposals(status="pending")
test("all 4 are pending", len(pending) == 4)

approved = get_proposals(status="approved")
test("none approved yet", len(approved) == 0)


# ─── Test 3: Get single proposal ──────────────────────────
print("\n🧪 Test 3: Get single proposal by ID")

fetched = get_proposal(p1.id)
test("found by ID", fetched is not None)
test("correct title", fetched.title == "Fix Deny-Storage-HTTP to exempt SFTP gateways")

missing = get_proposal("nonexistent")
test("returns None for missing", missing is None)


# ─── Test 4: Approve a proposal ───────────────────────────
print("\n🧪 Test 4: Approve proposal — human in the loop")

result = approve_proposal(p1.id, approved_by="brad.allen")
test("status = approved", result["status"] == "approved")
test("approved_by set", result["proposal"]["approved_by"] == "brad.allen")
test("has ADO payload", "ado_payload" in result)

payload = result["ado_payload"]
test("PR payload has sourceRefName", "sourceRefName" in str(payload.get("body", {})))
test("PR payload has file_changes", len(payload.get("file_changes", [])) == 1)

# Can't approve again
result2 = approve_proposal(p1.id)
test("double-approve blocked", "error" in result2)

# Check filter
approved = get_proposals(status="approved")
test("1 approved now", len(approved) == 1)
pending = get_proposals(status="pending")
test("3 still pending", len(pending) == 3)


# ─── Test 5: Reject a proposal ────────────────────────────
print("\n🧪 Test 5: Reject proposal — human says no")

result = reject_proposal(p4.id, reason="Exemption was already reviewed last week")
test("status = rejected", result["status"] == "rejected")
test("reason captured", "already reviewed" in result["proposal"]["rejected_reason"])

# Can't reject again
result2 = reject_proposal(p4.id)
test("double-reject blocked", "error" in result2)

pending = get_proposals(status="pending")
test("2 pending now", len(pending) == 2)


# ─── Test 6: Work item payload structure ──────────────────
print("\n🧪 Test 6: Work item payload — ADO REST API format")

result = approve_proposal(p2.id, approved_by="christopher.smith")
payload = result["ado_payload"]

test("is work item API", "wit/workitems" in payload["api"])
test("type is Bug", "$Bug" in payload["api"])
test("method is POST", payload["method"] == "POST")
test("body is JSON patch", isinstance(payload["body"], list))
test("has title field", any(p["path"] == "/fields/System.Title" for p in payload["body"]))
test("has priority field", any(p["path"] == "/fields/Microsoft.VSTS.Common.Priority" for p in payload["body"]))
test("has tags", any(p["path"] == "/fields/System.Tags" for p in payload["body"]))
test("has metadata", "violation_class" in payload.get("metadata", {}))


# ─── Test 7: PBI payload structure ────────────────────────
print("\n🧪 Test 7: PBI payload — workaround abuse")

result = approve_proposal(p3.id, approved_by="shane.ops")
payload = result["ado_payload"]

test("type is PBI", "Product Backlog Item" in payload["api"])
test("has acceptance criteria", any(
    p["path"] == "/fields/Microsoft.VSTS.Common.AcceptanceCriteria"
    for p in payload["body"]
))


# ─── Test 8: Generate proposals from Inspector output ─────
print("\n🧪 Test 8: Bulk generation from Inspector classifications")
reset()

inspection = {
    "classifications": [
        {
            "class": "policy_bug",
            "policy_name": "Deny-HTTP-Storage",
            "resource_id": "/subs/xxx/rg/storage1",
            "support_owner": "data-team",
            "reasoning": "Policy doesn't handle SFTP pattern",
            "title": "Fix storage HTTP policy",
            "description": "Update policy to exempt SFTP gateways",
            "acceptance_criteria": "SFTP storage compliant",
            "priority": 2,
        },
        {
            "class": "workaround_abuse",
            "policy_name": "Require-Workload-ID",
            "resource_id": "/subs/xxx/rg/aks1",
            "support_owner": "devops-team",
            "reasoning": "Expired exemption, should have migrated",
            "title": "Migrate AKS to workload identity",
            "description": "Create PBI for migration",
            "acceptance_criteria": "Workload identity enabled",
            "priority": 1,
        },
    ]
}

proposals = generate_proposals_from_inspection(inspection)
test("2 proposals created", len(proposals) == 2)
test("first is PR", proposals[0]["proposal_type"] == "pull_request")
test("second is PBI", proposals[1]["proposal_type"] == "pbi")
test("tags include class", "policy_bug" in proposals[0]["tags"])
test("tags include ops-council", "ops-council" in proposals[0]["tags"])


# ─── Test 9: Edge cases ──────────────────────────────────
print("\n🧪 Test 9: Edge cases")
reset()

# Empty classifications
proposals = generate_proposals_from_inspection({"classifications": []})
test("empty input → empty output", len(proposals) == 0)

# Approve nonexistent
result = approve_proposal("doesnt-exist")
test("approve missing → error", "error" in result)

# Reject nonexistent
result = reject_proposal("doesnt-exist")
test("reject missing → error", "error" in result)


# ─── Summary ──────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
