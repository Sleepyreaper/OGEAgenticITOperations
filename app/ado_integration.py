"""Azure DevOps integration — work item + PR creation for Ops Council Phase 2.

The Inspector classifies policy violations → proposes ADO actions → human approves →
this module creates the work items or PRs in Azure DevOps.

Flow:
  1. Inspector classifies violation (policy bug / misconfiguration / workaround abuse)
  2. System generates a PROPOSAL (PBI, Bug, or PR) with full content
  3. Human reviews proposal in the Ops Council UI
  4. On approval, this module calls ADO REST API to create the artifact
  5. On rejection, proposal is archived with reason

Auth: PAT stored in Key Vault (ADO-PAT secret). For customer deployments,
replace with Managed Identity + ADO service connection.
"""

import os
import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class ProposalType(str, Enum):
    """What the Inspector is proposing."""
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
    """The Inspector's classification framework."""
    POLICY_BUG = "policy_bug"
    MISCONFIGURATION = "misconfiguration"
    INTENTIONAL_EXEMPTION = "intentional_exemption"
    WORKAROUND_ABUSE = "workaround_abuse"


@dataclass
class AdoProposal:
    """A proposed ADO action awaiting human approval."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = ProposalStatus.PENDING.value

    # Classification from The Inspector
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
    """Create a new proposal from The Inspector's classification.

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
        tags=tags or ["ops-council", "ai-generated", violation_class],
        pr_source_branch=f"ops-council/policy-fix-{str(uuid.uuid4())[:8]}",
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
    """Human approves a proposal. Returns the proposal ready for ADO creation."""
    proposal = _proposals.get(proposal_id)
    if not proposal:
        return {"error": f"Proposal {proposal_id} not found"}
    if proposal.status != ProposalStatus.PENDING.value:
        return {"error": f"Proposal is {proposal.status}, not pending"}

    proposal.status = ProposalStatus.APPROVED.value
    proposal.approved_by = approved_by
    proposal.approved_at = datetime.now(timezone.utc).isoformat()

    # In a connected environment, this would call _create_in_ado(proposal)
    # For PoC: mark as approved, return the payload that WOULD be sent
    ado_payload = _build_ado_payload(proposal)

    return {
        "status": "approved",
        "proposal": proposal.to_dict(),
        "ado_payload": ado_payload,
        "message": "Proposal approved. In production, this would create the item in Azure DevOps.",
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
    """Build ADO pull request creation payload."""
    return {
        "api": "_apis/git/repositories/{repo}/pullrequests",
        "method": "POST",
        "api_version": "7.1",
        "content_type": "application/json",
        "body": {
            "sourceRefName": f"refs/heads/{proposal.pr_source_branch}",
            "targetRefName": f"refs/heads/{proposal.pr_target_branch}",
            "title": proposal.title,
            "description": proposal.description,
            "labels": [{"name": tag} for tag in proposal.tags],
        },
        "file_changes": proposal.pr_file_changes,
        "metadata": {
            "violation_class": proposal.violation_class,
            "policy_name": proposal.policy_name,
            "resource_id": proposal.resource_id,
            "support_owner": proposal.support_owner,
            "inspector_reasoning": proposal.inspector_reasoning,
            "note": "In production: create branch → push file changes → create PR",
        },
    }


def generate_proposals_from_inspection(inspection_result: dict) -> list[dict]:
    """Parse an Inspector agent response and generate proposals.

    The Inspector's response contains structured classifications.
    This function extracts them and creates proposals.

    Args:
        inspection_result: The crew's analysis containing Inspector classifications

    Returns:
        List of created proposals
    """
    # The Inspector's structured output would be parsed here.
    # For PoC, we create proposals from the known classification patterns.
    proposals = []

    # Example classifications that The Inspector would produce:
    classifications = inspection_result.get("classifications", [])

    for c in classifications:
        proposal = create_proposal(
            violation_class=c.get("class", ViolationClass.MISCONFIGURATION.value),
            policy_name=c.get("policy_name", ""),
            resource_id=c.get("resource_id", ""),
            support_owner=c.get("support_owner", ""),
            inspector_reasoning=c.get("reasoning", ""),
            title=c.get("title", "Ops Council Finding"),
            description=c.get("description", ""),
            acceptance_criteria=c.get("acceptance_criteria", ""),
            priority=c.get("priority", 2),
            pr_file_changes=c.get("file_changes", []),
        )
        proposals.append(proposal.to_dict())

    return proposals
