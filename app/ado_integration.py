"""Azure DevOps integration — work item + PR creation for Cloud Weather Ops Phase 2.

The Regulator classifies policy violations → The Lineman generates fix code →
system creates PR or PBI in ADO → human reviews and approves → CI/CD deploys.

Full Flow:
  1. Inspector classifies violation (policy bug / misconfiguration / workaround abuse)
  2. For policy bugs: Lineman generates Terraform/policy-as-code fix
  3. System creates a PROPOSAL with the fix code attached
  4. Human reviews proposal in the Cloud Weather Ops UI
  5. On approval: system calls ADO REST API to:
     - Policy bugs: create branch → push fix files → open PR → Terraform pipeline validates
     - Workaround abuse: create PBI with investigation details
     - Misconfiguration: create Bug assigned to support owner
     - Exemptions: create Task to verify documentation
  6. Human reviews PR in ADO (sees terraform plan output from pipeline)
  7. Human merges PR → pipeline runs terraform apply to production

Auth: PAT stored in Key Vault (ADO-PAT secret). For production,
use Managed Identity + ADO Workload Identity Federation.
"""

import os
import re
import json
import uuid
import base64
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


class ProposalType(str, Enum):
    """What the Regulator is proposing."""
    PULL_REQUEST = "pull_request"     # Policy bug → fix the policy definition
    WORK_ITEM_PBI = "pbi"            # Workaround abuse → investigate
    WORK_ITEM_BUG = "bug"            # Misconfiguration → fix the resource
    WORK_ITEM_TASK = "task"          # Exemption review → verify documentation


class ProposalStatus(str, Enum):
    """Human-in-the-loop status."""
    PENDING = "pending"              # Waiting for human review
    APPROVED = "approved"            # Human approved — ready to create in ADO
    REJECTED = "rejected"            # Human rejected — archived
    CREATED = "created"              # Successfully created in ADO
    FAILED = "failed"               # ADO API call failed


class ViolationClass(str, Enum):
    """The Regulator's classification framework."""
    POLICY_BUG = "policy_bug"
    MISCONFIGURATION = "misconfiguration"
    INTENTIONAL_EXEMPTION = "intentional_exemption"
    WORKAROUND_ABUSE = "workaround_abuse"


# ═══════════════════════════════════════════════════════════════
# ADO REST API Client
# ═══════════════════════════════════════════════════════════════

class AdoClient:
    """Azure DevOps REST API client.

    Handles: work items (PBI/Bug/Task), Git branches, file pushes, pull requests.

    Config via environment variables:
      ADO_ORG_URL     — e.g. https://dev.azure.com/dtenergy
      ADO_PROJECT     — e.g. CloudOps
      ADO_REPO        — e.g. policy-as-code (for PR file pushes)
      ADO_PAT         — Personal Access Token (from Key Vault)
    """

    def __init__(self):
        self.org_url = os.environ.get("ADO_ORG_URL", "").rstrip("/")
        self.project = os.environ.get("ADO_PROJECT", "")
        self.repo = os.environ.get("ADO_REPO", "")
        self.pat = os.environ.get("ADO_PAT", "")
        self.api_version = "7.1"

    @property
    def configured(self) -> bool:
        return bool(self.org_url and self.project and self.pat)

    @property
    def _auth_header(self) -> dict:
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _url(self, path: str) -> str:
        return f"{self.org_url}/{quote(self.project)}/{path}"

    def _git_url(self, path: str) -> str:
        return f"{self.org_url}/{quote(self.project)}/_apis/git/repositories/{quote(self.repo)}/{path}"

    # ─── Work Items ─────────────────────────────────────────

    def create_work_item(self, work_item_type: str, patch_document: list) -> dict:
        """Create a work item (PBI, Bug, Task) in ADO.

        Args:
            work_item_type: "Product Backlog Item", "Bug", or "Task"
            patch_document: JSON Patch array of field operations

        Returns:
            ADO response with id, url, fields
        """
        url = self._url(f"_apis/wit/workitems/${quote(work_item_type)}")
        resp = requests.post(
            url,
            headers={
                **self._auth_header,
                "Content-Type": "application/json-patch+json",
            },
            params={"api-version": self.api_version},
            json=patch_document,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── Git: Branch + Push + PR ────────────────────────────

    def get_default_branch_ref(self) -> dict:
        """Get the latest commit on the default branch (main)."""
        url = self._git_url("refs")
        resp = requests.get(
            url,
            headers=self._auth_header,
            params={"api-version": self.api_version, "filter": "heads/main"},
            timeout=30,
        )
        resp.raise_for_status()
        refs = resp.json().get("value", [])
        if not refs:
            raise ValueError("Could not find refs/heads/main in repo")
        return refs[0]

    def create_branch_and_push(
        self,
        branch_name: str,
        file_changes: list[dict],
        commit_message: str,
    ) -> dict:
        """Create a new branch from main and push file changes in a single push.

        Args:
            branch_name: e.g. "dte-weather-ops/policy-fix-abc123"
            file_changes: [{"path": "/policies/fix.tf", "content": "..."}]
            commit_message: commit message

        Returns:
            ADO push response
        """
        # Get the latest commit on main to branch from
        main_ref = self.get_default_branch_ref()
        old_object_id = main_ref["objectId"]

        # Build the push payload: create branch + add/edit files
        changes = []
        for fc in file_changes:
            changes.append({
                "changeType": "add",
                "item": {"path": fc["path"]},
                "newContent": {
                    "content": fc["content"],
                    "contentType": "rawtext",
                },
            })

        push_payload = {
            "refUpdates": [
                {
                    "name": f"refs/heads/{branch_name}",
                    "oldObjectId": "0" * 40,  # new branch
                    "newObjectId": old_object_id,  # placeholder — server computes
                }
            ],
            "commits": [
                {
                    "comment": commit_message,
                    "changes": changes,
                }
            ],
        }

        url = self._git_url("pushes")
        resp = requests.post(
            url,
            headers={**self._auth_header, "Content-Type": "application/json"},
            params={"api-version": self.api_version},
            json=push_payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def create_pull_request(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] = None,
    ) -> dict:
        """Create a pull request in ADO.

        Returns:
            ADO PR response with pullRequestId, url, etc.
        """
        pr_payload = {
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "title": title,
            "description": description,
        }

        if labels:
            pr_payload["labels"] = [{"name": lbl} for lbl in labels]

        url = self._git_url("pullrequests")
        resp = requests.post(
            url,
            headers={**self._auth_header, "Content-Type": "application/json"},
            params={"api-version": self.api_version},
            json=pr_payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ─── Full Flows ─────────────────────────────────────────

    def execute_pr_proposal(self, proposal: "AdoProposal") -> dict:
        """Full PR flow: create branch → push fix files → open PR.

        The ADO Terraform pipeline (terraform-pipeline.yml) triggers on
        the new PR automatically and runs terraform plan. Human reviews
        the plan output + code, then merges → pipeline runs terraform apply.
        """
        if not proposal.pr_file_changes:
            raise ValueError("PR proposal has no file changes — Lineman didn't generate fix code")

        # Step 1: Create branch from main and push fix files
        commit_msg = (
            f"[Cloud Weather Ops] {proposal.title}\n\n"
            f"Classification: {proposal.violation_class}\n"
            f"Policy: {proposal.policy_name}\n"
            f"Resource: {proposal.resource_id}\n"
            f"Inspector: {proposal.inspector_reasoning}\n\n"
            f"Auto-generated by Cloud Weather Ops. Human review required."
        )

        push_result = self.create_branch_and_push(
            branch_name=proposal.pr_source_branch,
            file_changes=proposal.pr_file_changes,
            commit_message=commit_msg,
        )

        # Step 2: Create the PR
        pr_description = (
            f"## Cloud Weather Ops — Policy Fix\n\n"
            f"**Classification**: {proposal.violation_class}\n"
            f"**Policy**: `{proposal.policy_name}`\n"
            f"**Resource**: `{proposal.resource_id}`\n"
            f"**Support Owner**: {proposal.support_owner}\n\n"
            f"### Inspector's Analysis\n{proposal.inspector_reasoning}\n\n"
            f"### Acceptance Criteria\n{proposal.acceptance_criteria}\n\n"
            f"---\n"
            f"*Auto-generated by the Cloud Weather Ops. "
            f"The Terraform pipeline will run `terraform plan` automatically. "
            f"Review the plan output before merging.*"
        )

        pr_result = self.create_pull_request(
            source_branch=proposal.pr_source_branch,
            target_branch=proposal.pr_target_branch,
            title=f"[Cloud Weather Ops] {proposal.title}",
            description=pr_description,
            labels=proposal.tags,
        )

        return {
            "push": {"pushId": push_result.get("pushId")},
            "pull_request": {
                "id": pr_result.get("pullRequestId"),
                "url": pr_result.get("url"),
                "web_url": f"{self.org_url}/{quote(self.project)}/_git/{quote(self.repo)}/pullrequest/{pr_result.get('pullRequestId')}",
            },
        }

    def execute_work_item_proposal(self, proposal: "AdoProposal") -> dict:
        """Create a work item (PBI/Bug/Task) in ADO from a proposal."""
        type_map = {
            ProposalType.WORK_ITEM_PBI.value: "Product Backlog Item",
            ProposalType.WORK_ITEM_BUG.value: "Bug",
            ProposalType.WORK_ITEM_TASK.value: "Task",
        }
        work_item_type = type_map.get(proposal.proposal_type, "Product Backlog Item")

        patch_document = [
            {"op": "add", "path": "/fields/System.Title", "value": proposal.title},
            {"op": "add", "path": "/fields/System.Description", "value": (
                f"<h2>Cloud Weather Ops Finding</h2>"
                f"<p><b>Classification</b>: {proposal.violation_class}</p>"
                f"<p><b>Policy</b>: <code>{proposal.policy_name}</code></p>"
                f"<p><b>Resource</b>: <code>{proposal.resource_id}</code></p>"
                f"<p><b>Support Owner</b>: {proposal.support_owner}</p>"
                f"<h3>Inspector's Analysis</h3><p>{proposal.inspector_reasoning}</p>"
                f"<h3>Description</h3><p>{proposal.description}</p>"
            )},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": proposal.priority},
            {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(proposal.tags)},
        ]

        if proposal.acceptance_criteria:
            patch_document.append({
                "op": "add",
                "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria",
                "value": proposal.acceptance_criteria,
            })

        if proposal.area_path:
            patch_document.append(
                {"op": "add", "path": "/fields/System.AreaPath", "value": proposal.area_path}
            )

        if proposal.iteration_path:
            patch_document.append(
                {"op": "add", "path": "/fields/System.IterationPath", "value": proposal.iteration_path}
            )

        result = self.create_work_item(work_item_type, patch_document)

        return {
            "work_item": {
                "id": result.get("id"),
                "type": work_item_type,
                "url": result.get("url"),
                "web_url": result.get("_links", {}).get("html", {}).get("href", ""),
            },
        }


# ─── Singleton client ──────────────────────────────────────
_ado_client: Optional[AdoClient] = None


def get_ado_client() -> AdoClient:
    global _ado_client
    if _ado_client is None:
        _ado_client = AdoClient()
    return _ado_client


# ═══════════════════════════════════════════════════════════════
# Proposal Data Model
# ═══════════════════════════════════════════════════════════════

@dataclass
class AdoProposal:
    """A proposed ADO action awaiting human approval."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = ProposalStatus.PENDING.value

    # Classification from The Regulator
    violation_class: str = ""
    policy_name: str = ""
    resource_id: str = ""
    support_owner: str = ""
    inspector_reasoning: str = ""

    # Proposed ADO action
    proposal_type: str = ""        # ProposalType value
    title: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    priority: int = 2              # 1=Critical, 2=High, 3=Medium, 4=Low
    area_path: str = ""
    iteration_path: str = ""
    tags: list = field(default_factory=list)

    # For PRs specifically
    pr_target_branch: str = "main"
    pr_source_branch: str = ""
    pr_file_changes: list = field(default_factory=list)  # [{path, content}]

    # After creation
    ado_id: Optional[str] = None   # Work item ID or PR ID from ADO
    ado_url: Optional[str] = None  # Link to the created item
    approved_by: str = ""
    approved_at: str = ""
    rejected_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─── In-Memory Proposal Store ──────────────────────────────
# In production, persist to Cosmos DB or blob storage.
# For PoC, in-memory is fine — proposals survive the session.
_proposals: dict[str, AdoProposal] = {}


def create_proposal(
    violation_class: str,
    policy_name: str,
    resource_id: str,
    support_owner: str,
    inspector_reasoning: str,
    title: str,
    description: str,
    acceptance_criteria: str = "",
    priority: int = 2,
    tags: list = None,
    pr_file_changes: list = None,
) -> AdoProposal:
    """Create a new proposal from The Regulator's classification.

    Maps violation class → proposal type:
      - policy_bug → pull_request (fix the policy definition)
      - misconfiguration → bug (fix the resource)
      - workaround_abuse → pbi (investigate and redesign)
      - intentional_exemption → task (verify documentation is current)
    """
    type_map = {
        ViolationClass.POLICY_BUG.value: ProposalType.PULL_REQUEST.value,
        ViolationClass.MISCONFIGURATION.value: ProposalType.WORK_ITEM_BUG.value,
        ViolationClass.WORKAROUND_ABUSE.value: ProposalType.WORK_ITEM_PBI.value,
        ViolationClass.INTENTIONAL_EXEMPTION.value: ProposalType.WORK_ITEM_TASK.value,
    }

    proposal = AdoProposal(
        violation_class=violation_class,
        policy_name=policy_name,
        resource_id=resource_id,
        support_owner=support_owner,
        inspector_reasoning=inspector_reasoning,
        proposal_type=type_map.get(violation_class, ProposalType.WORK_ITEM_PBI.value),
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria,
        priority=priority,
        tags=tags or ["dte-weather-ops", "ai-generated", violation_class],
        pr_source_branch=f"dte-weather-ops/policy-fix-{str(uuid.uuid4())[:8]}",
        pr_file_changes=pr_file_changes or [],
    )

    _proposals[proposal.id] = proposal
    return proposal


def get_proposals(status: str = None) -> list[dict]:
    """List proposals, optionally filtered by status."""
    results = []
    for p in _proposals.values():
        if status and p.status != status:
            continue
        results.append(p.to_dict())
    return sorted(results, key=lambda x: x["created_at"], reverse=True)


def get_proposal(proposal_id: str) -> Optional[AdoProposal]:
    """Get a single proposal by ID."""
    return _proposals.get(proposal_id)


def approve_proposal(proposal_id: str, approved_by: str = "ops-user") -> dict:
    """Human approves a proposal → creates the item in ADO if connected.

    If ADO is configured (env vars set): actually calls ADO REST API.
    If not configured: returns the payload that WOULD be sent (PoC mode).
    """
    proposal = _proposals.get(proposal_id)
    if not proposal:
        return {"error": f"Proposal {proposal_id} not found"}
    if proposal.status != ProposalStatus.PENDING.value:
        return {"error": f"Proposal is {proposal.status}, not pending"}

    proposal.status = ProposalStatus.APPROVED.value
    proposal.approved_by = approved_by
    proposal.approved_at = datetime.now(timezone.utc).isoformat()

    client = get_ado_client()

    if client.configured:
        # Live mode — actually create in ADO
        try:
            if proposal.proposal_type == ProposalType.PULL_REQUEST.value:
                ado_result = client.execute_pr_proposal(proposal)
                proposal.ado_id = str(ado_result["pull_request"]["id"])
                proposal.ado_url = ado_result["pull_request"]["web_url"]
            else:
                ado_result = client.execute_work_item_proposal(proposal)
                proposal.ado_id = str(ado_result["work_item"]["id"])
                proposal.ado_url = ado_result["work_item"]["web_url"]

            proposal.status = ProposalStatus.CREATED.value
            return {
                "status": "created",
                "proposal": proposal.to_dict(),
                "ado_result": ado_result,
                "message": f"Created in Azure DevOps: {proposal.ado_url}",
            }
        except Exception as e:
            proposal.status = ProposalStatus.FAILED.value
            logger.exception("Failed to create ADO item for proposal %s", proposal_id)
            return {
                "status": "failed",
                "proposal": proposal.to_dict(),
                "error": str(e),
                "message": f"ADO creation failed: {e}. Proposal marked as failed.",
            }
    else:
        # PoC mode — return what WOULD be sent
        ado_payload = _build_ado_payload(proposal)
        return {
            "status": "approved",
            "proposal": proposal.to_dict(),
            "ado_payload": ado_payload,
            "message": "Proposal approved. ADO not configured — showing payload that would be sent. Set ADO_ORG_URL, ADO_PROJECT, ADO_PAT to enable live creation.",
        }


def reject_proposal(proposal_id: str, reason: str = "") -> dict:
    """Human rejects a proposal."""
    proposal = _proposals.get(proposal_id)
    if not proposal:
        return {"error": f"Proposal {proposal_id} not found"}
    if proposal.status != ProposalStatus.PENDING.value:
        return {"error": f"Proposal is {proposal.status}, not pending"}

    proposal.status = ProposalStatus.REJECTED.value
    proposal.rejected_reason = reason

    return {
        "status": "rejected",
        "proposal": proposal.to_dict(),
        "message": f"Proposal rejected. Reason: {reason or 'No reason provided'}",
    }


def _build_ado_payload(proposal: AdoProposal) -> dict:
    """Build the Azure DevOps REST API payload for a proposal.

    This is what would be sent to:
      POST https://dev.azure.com/{org}/{project}/_apis/wit/workitems/${type}?api-version=7.1
    or for PRs:
      POST https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/pullrequests?api-version=7.1
    """
    if proposal.proposal_type == ProposalType.PULL_REQUEST.value:
        return _build_pr_payload(proposal)
    else:
        return _build_work_item_payload(proposal)


def _build_work_item_payload(proposal: AdoProposal) -> dict:
    """Build ADO work item creation payload (PBI, Bug, or Task)."""
    type_map = {
        ProposalType.WORK_ITEM_PBI.value: "Product Backlog Item",
        ProposalType.WORK_ITEM_BUG.value: "Bug",
        ProposalType.WORK_ITEM_TASK.value: "Task",
    }

    work_item_type = type_map.get(proposal.proposal_type, "Product Backlog Item")

    # ADO REST API uses JSON Patch format
    patch_document = [
        {"op": "add", "path": "/fields/System.Title", "value": proposal.title},
        {"op": "add", "path": "/fields/System.Description", "value": proposal.description},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": proposal.priority},
        {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(proposal.tags)},
    ]

    if proposal.acceptance_criteria:
        patch_document.append({
            "op": "add",
            "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria",
            "value": proposal.acceptance_criteria,
        })

    if proposal.area_path:
        patch_document.append({
            "op": "add", "path": "/fields/System.AreaPath", "value": proposal.area_path
        })

    if proposal.iteration_path:
        patch_document.append({
            "op": "add", "path": "/fields/System.IterationPath", "value": proposal.iteration_path
        })

    return {
        "api": f"_apis/wit/workitems/${work_item_type}",
        "method": "POST",
        "api_version": "7.1",
        "content_type": "application/json-patch+json",
        "body": patch_document,
        "metadata": {
            "violation_class": proposal.violation_class,
            "policy_name": proposal.policy_name,
            "resource_id": proposal.resource_id,
            "support_owner": proposal.support_owner,
            "inspector_reasoning": proposal.inspector_reasoning,
        },
    }


def _build_pr_payload(proposal: AdoProposal) -> dict:
    """Build ADO pull request creation payload (for PoC mode — shows what would happen)."""
    return {
        "steps": [
            {
                "step": 1,
                "action": "Create branch from main",
                "api": f"_apis/git/repositories/{{repo}}/pushes",
                "branch": f"refs/heads/{proposal.pr_source_branch}",
            },
            {
                "step": 2,
                "action": "Push fix files to branch",
                "files": [{"path": fc.get("path", ""), "content_preview": fc.get("content", "")[:200] + "..."} for fc in proposal.pr_file_changes],
            },
            {
                "step": 3,
                "action": "Create pull request",
                "api": "_apis/git/repositories/{repo}/pullrequests",
                "body": {
                    "sourceRefName": f"refs/heads/{proposal.pr_source_branch}",
                    "targetRefName": f"refs/heads/{proposal.pr_target_branch}",
                    "title": f"[Cloud Weather Ops] {proposal.title}",
                    "labels": proposal.tags,
                },
            },
            {
                "step": 4,
                "action": "Terraform pipeline auto-triggers on PR",
                "pipeline": "terraform-pipeline.yml",
                "runs": "terraform init → terraform validate → terraform plan",
            },
            {
                "step": 5,
                "action": "Human reviews PR + terraform plan output",
                "requires": "Manual approval in ADO",
            },
            {
                "step": 6,
                "action": "On merge → pipeline runs terraform apply",
                "target": "production",
            },
        ],
        "metadata": {
            "violation_class": proposal.violation_class,
            "policy_name": proposal.policy_name,
            "resource_id": proposal.resource_id,
            "support_owner": proposal.support_owner,
            "inspector_reasoning": proposal.inspector_reasoning,
        },
    }


def generate_proposals_from_inspection(inspection_result: dict) -> list[dict]:
    """Parse an Inspector agent response and generate proposals.

    The Regulator's response contains structured classifications.
    This function extracts them and creates proposals.

    Args:
        inspection_result: The crew's analysis containing Inspector classifications

    Returns:
        List of created proposals
    """
    # The Regulator's structured output would be parsed here.
    # For PoC, we create proposals from the known classification patterns.
    proposals = []

    # Example classifications that The Regulator would produce:
    classifications = inspection_result.get("classifications", [])

    for c in classifications:
        proposal = create_proposal(
            violation_class=c.get("class", ViolationClass.MISCONFIGURATION.value),
            policy_name=c.get("policy_name", ""),
            resource_id=c.get("resource_id", ""),
            support_owner=c.get("support_owner", ""),
            inspector_reasoning=c.get("reasoning", ""),
            title=c.get("title", "Cloud Weather Ops Finding"),
            description=c.get("description", ""),
            acceptance_criteria=c.get("acceptance_criteria", ""),
            priority=c.get("priority", 2),
            pr_file_changes=c.get("file_changes", []),
        )
        proposals.append(proposal.to_dict())

    return proposals


def parse_remediation_to_file_changes(remediation_text: str, policy_name: str = "") -> list[dict]:
    """Parse The Lineman's remediation output into file changes for a PR.

    The Lineman generates code blocks labeled main.tf, variables.tf,
    remediate.sh, and RUNBOOK.md. This extracts them into the format
    needed for ADO Git push: [{path, content}].

    The files are placed under a policy-specific directory in the repo:
      remediation/<policy-name>/main.tf
      remediation/<policy-name>/variables.tf
      etc.
    """
    file_changes = []

    # Map of known filenames to their repo paths
    safe_policy = re.sub(r'[^a-zA-Z0-9_-]', '-', policy_name).lower().strip('-') or "fix"
    base_path = f"/remediation/{safe_policy}"

    # Extract code blocks with labels like: **main.tf** or ```main.tf or ```hcl (main.tf)
    # Pattern: look for **filename** followed by a code block
    blocks = re.findall(
        r'\*\*([a-zA-Z0-9_.-]+)\*\*.*?```[a-z]*\s*\n(.*?)```',
        remediation_text,
        re.DOTALL,
    )

    for filename, content in blocks:
        file_changes.append({
            "path": f"{base_path}/{filename}",
            "content": content.strip(),
        })

    # If no labeled blocks found, try generic code blocks
    if not file_changes:
        generic_blocks = re.findall(r'```[a-z]*\s*\n(.*?)```', remediation_text, re.DOTALL)
        for i, content in enumerate(generic_blocks):
            ext = ".tf" if "resource " in content or "variable " in content else ".sh"
            file_changes.append({
                "path": f"{base_path}/fix-{i+1}{ext}",
                "content": content.strip(),
            })

    return file_changes
