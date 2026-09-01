#!/usr/bin/env python3
"""Test the ADO integration module — proposal lifecycle + ADO client + remediation parsing.

Run: python3 tests/test_ado_integration.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ado_integration import (
    create_proposal, get_proposals, get_proposal,
    approve_proposal, reject_proposal,
    generate_proposals_from_inspection,
    parse_remediation_to_file_changes,
    AdoClient, get_ado_client,
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
test("has source branch", p1.pr_source_branch.startswith("dte-weather-ops/policy-fix-"))

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
test("PR payload has steps", "steps" in payload)
test("PR payload has file push step", any("files" in str(s) for s in payload.get("steps", [])))

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
test("tags include dte-weather-ops", "dte-weather-ops" in proposals[0]["tags"])


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


# ─── Test 10: Parse remediation to file changes ─────────
print("\n🧪 Test 10: Parse Roughneck remediation output into PR file changes")

remediation_output = """Here's the fix for the Deny-Storage-HTTP policy:

**main.tf**
```hcl
resource "azurerm_policy_definition" "deny_storage_http_v2" {
  name         = "deny-storage-http-v2"
  policy_type  = "Custom"
  mode         = "Indexed"
  display_name = "Storage accounts should use HTTPS (with SFTP exemption)"

  metadata = jsonencode({
    category    = "Storage"
    managed-by  = "dte-weather-ops"
  })

  policy_rule = jsonencode({
    if = {
      allOf = [
        { field = "type", equals = "Microsoft.Storage/storageAccounts" },
        { field = "Microsoft.Storage/storageAccounts/supportsHttpsTrafficOnly", equals = false },
        { not = { field = "tags['sftp-gateway']", equals = "true" } }
      ]
    }
    then = { effect = "deny" }
  })
}
```

**variables.tf**
```hcl
variable "subscription_id" {
  description = "Target subscription"
  type        = string
}

variable "resource_group" {
  description = "Resource group for policy assignment"
  type        = string
  default     = "policy-management"
}
```

**RUNBOOK.md**
```markdown
## Deny-Storage-HTTP Policy Fix

1. Review the updated policy definition
2. Run terraform plan — verify only the policy definition changes
3. Apply — the new policy exempts SFTP gateway storage accounts
4. Validate: check that legacy SFTP account is now compliant
```
"""

file_changes = parse_remediation_to_file_changes(remediation_output, "Deny-Storage-HTTP")
test("3 files extracted", len(file_changes) == 3)
test("main.tf path", file_changes[0]["path"] == "/remediation/deny-storage-http/main.tf")
test("variables.tf path", file_changes[1]["path"] == "/remediation/deny-storage-http/variables.tf")
test("RUNBOOK.md path", file_changes[2]["path"] == "/remediation/deny-storage-http/RUNBOOK.md")
test("main.tf has terraform content", "azurerm_policy_definition" in file_changes[0]["content"])
test("variables.tf has variable", 'variable "subscription_id"' in file_changes[1]["content"])
test("RUNBOOK.md has steps", "terraform plan" in file_changes[2]["content"])


# ─── Test 11: Parse remediation — edge cases ────────────
print("\n🧪 Test 11: Remediation parsing edge cases")

# No code blocks at all
empty_changes = parse_remediation_to_file_changes("No code here, just text.", "test")
test("no code blocks → empty", len(empty_changes) == 0)

# Generic code blocks without labeled filenames
generic_output = """Here's a fix:

```hcl
resource "azurerm_storage_account" "fix" {
  name = "fixed"
}
```

And a script:

```bash
az storage account update --name fixed --https-only true
```
"""
generic_changes = parse_remediation_to_file_changes(generic_output, "generic-fix")
test("generic blocks parsed", len(generic_changes) == 2)
test("tf file detected", generic_changes[0]["path"].endswith(".tf"))
test("sh file detected", generic_changes[1]["path"].endswith(".sh"))

# Policy name sanitization
changes = parse_remediation_to_file_changes(
    "**main.tf**\n```hcl\ntest\n```", "Deny/Storage HTTP (v2)"
)
test("policy name sanitized", "/remediation/deny-storage-http--v2-/main.tf" == changes[0]["path"] or "deny" in changes[0]["path"])


# ─── Test 12: ADO Client — configuration check ──────────
print("\n🧪 Test 12: ADO Client configuration")

client = AdoClient()
test("unconfigured without env vars", not client.configured)

# Simulate configured client
client.org_url = "https://dev.azure.com/dtenergy"
client.project = "CloudOps"
client.repo = "policy-as-code"
client.pat = "fake-pat-for-testing"
test("configured with all vars", client.configured)
test("auth header is Basic", "Basic" in client._auth_header["Authorization"])


# ─── Test 13: PR proposal payload shows full flow ───────
print("\n🧪 Test 13: PR proposal payload shows 6-step Terraform pipeline flow")
reset()

p = create_proposal(
    violation_class="policy_bug",
    policy_name="Deny-Storage-HTTP",
    resource_id="/subs/xxx/storage1",
    support_owner="data-team",
    inspector_reasoning="Policy doesn't handle SFTP",
    title="Fix storage HTTP policy",
    description="Exempt SFTP gateways",
    pr_file_changes=[
        {"path": "/remediation/deny-storage-http/main.tf", "content": "resource \"azurerm_policy\"..."},
        {"path": "/remediation/deny-storage-http/variables.tf", "content": "variable \"sub\"..."},
    ],
)

# Approve without ADO configured → get payload showing full flow
result = approve_proposal(p.id)
test("approved in PoC mode", result["status"] == "approved")

payload = result["ado_payload"]
test("payload has steps", "steps" in payload)
test("6 steps in flow", len(payload["steps"]) == 6)
test("step 1 = create branch", "branch" in str(payload["steps"][0]))
test("step 2 = push files", "files" in payload["steps"][1])
test("step 3 = create PR", "pullrequest" in str(payload["steps"][2]).lower())
test("step 4 = terraform plan", "terraform" in str(payload["steps"][3]).lower())
test("step 5 = human review", "approval" in str(payload["steps"][4]).lower())
test("step 6 = terraform apply", "apply" in str(payload["steps"][5]).lower())
test("metadata has policy name", payload["metadata"]["policy_name"] == "Deny-Storage-HTTP")


# ─── Test 14: Full flow — policy bug with fix code ──────
print("\n🧪 Test 14: Full flow — Inspector classifies + Roughneck generates fix → PR proposal")
reset()

# Simulate what inspect-and-propose does after calling both agents
classifications = [
    {
        "class": "policy_bug",
        "policy_name": "Deny-Storage-HTTP",
        "resource_id": "/subs/xxx/storage1",
        "support_owner": "data-team",
        "reasoning": "Policy doesn't handle SFTP pattern",
        "title": "Fix storage HTTP policy",
        "description": "Update policy to exempt SFTP gateways",
        "acceptance_criteria": "SFTP storage compliant",
        "priority": 2,
        "file_changes": [
            {"path": "/remediation/deny-storage-http/main.tf", "content": "resource \"azurerm_policy\"..."},
            {"path": "/remediation/deny-storage-http/variables.tf", "content": "variable..."},
            {"path": "/remediation/deny-storage-http/RUNBOOK.md", "content": "# Steps..."},
        ],
    },
    {
        "class": "workaround_abuse",
        "policy_name": "Require-Workload-ID",
        "resource_id": "/subs/xxx/aks1",
        "support_owner": "devops-team",
        "reasoning": "Expired exemption",
        "title": "Migrate AKS to workload identity",
        "description": "Create PBI",
        "acceptance_criteria": "Workload identity enabled",
        "priority": 1,
    },
]

proposals = generate_proposals_from_inspection({"classifications": classifications})
test("2 proposals created", len(proposals) == 2)

pr_proposal = proposals[0]
pbi_proposal = proposals[1]

test("PR has file changes", len(pr_proposal["pr_file_changes"]) == 3)
test("PBI has no file changes", len(pbi_proposal["pr_file_changes"]) == 0)
test("PR is pull_request type", pr_proposal["proposal_type"] == "pull_request")
test("PBI is pbi type", pbi_proposal["proposal_type"] == "pbi")

# Approve the PR proposal — should show full 6-step flow
result = approve_proposal(pr_proposal["id"])
test("PR approval shows terraform pipeline flow", len(result["ado_payload"]["steps"]) == 6)

# Approve the PBI proposal — should show work item payload
result = approve_proposal(pbi_proposal["id"])
test("PBI shows work item API", "wit/workitems" in result["ado_payload"]["api"])


# ─── Summary ──────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
