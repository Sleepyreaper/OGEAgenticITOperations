"""Approval-tier policy: a small, deterministic classifier for "does this
action need a human?" -- shared by the read-only operations-analysis
layer (app/agents/analysis.py) and the existing ADO proposal flow
(app/ado_integration.py).

Six explicit tiers, never an opaque score:

  read_only          -- purely informational (review/monitor/document);
                         never requires approval, never auto-executes
                         anything (there's nothing to execute).
  autonomous         -- a narrowly-scoped action a caller *could* run
                         without a human, but ONLY when BOTH (a) the
                         caller is actually execution-capable and (b) the
                         action is explicitly present in a config
                         allowlist. Absent either, it requires approval
                         like any other tier -- there is no default-on
                         autonomous behavior anywhere in this app today.
  draft_ticket       -- propose an ADO work item (PBI/Bug/Task).
  draft_pr           -- propose an ADO pull request (policy/Terraform fix).
  reversible_nonprod -- a reversible action against a non-production
                         resource (restart/resize/scale a dev/staging
                         resource).
  production_write   -- any write/delete, network topology change, RBAC/
                         role assignment change, or cost commitment.
                         ALWAYS human_approved -- no allowlist, no
                         exception, regardless of caller/config.

Classification is keyword-based and deterministic (never inferred by a
model) -- see classify_action_text. This module has no side effects and
makes no Azure/ADO calls; it only computes metadata that callers attach
to their own responses.
"""

from enum import Enum

__all__ = [
    "ApprovalTier",
    "TaskAdherenceError",
    "classify_action_text",
    "approval_metadata",
    "analysis_action_metadata",
    "proposal_approval_tier",
]


class ApprovalTier(str, Enum):
    READ_ONLY = "read_only"
    AUTONOMOUS = "autonomous"
    DRAFT_TICKET = "draft_ticket"
    DRAFT_PR = "draft_pr"
    REVERSIBLE_NONPROD = "reversible_nonprod"
    PRODUCTION_WRITE = "production_write"


# These two are the only sets classify_action_text/approval_metadata
# reason about; every other tier follows the "requires human approval
# unless allowlisted" rule.
_ALWAYS_HUMAN_APPROVED = frozenset({ApprovalTier.PRODUCTION_WRITE})
_NEVER_REQUIRES_APPROVAL = frozenset({ApprovalTier.READ_ONLY})


class TaskAdherenceError(RuntimeError):
    """Raised when computed action metadata would violate a hard
    invariant this module enforces in code (not just by convention) --
    e.g. a read-only analysis surface producing an auto_executable
    action. Should never actually be raised in practice; it exists as a
    fail-loud guard against a future edit accidentally breaking that
    invariant, per the task-adherence requirement in
    docs/AGENT_INTELLIGENCE.md."""


# Keyword tables for classify_action_text, checked in this priority order
# (most-restrictive tier first) so an action description mentioning both
# a "restart" AND "production" is correctly classified as the more
# restrictive production_write, never the more permissive
# reversible_nonprod. All matching is on lowercased text -- deliberately
# simple/deterministic, never model-inferred.
_PRODUCTION_WRITE_KEYWORDS = (
    "delete", "deprovision", "decommission", "rbac", "role assignment",
    "network security group", "nsg rule", "firewall rule", "production",
    "prod environment", "reserved instance", "cost commitment",
    "budget commitment", "disable network", "public ip", "dns record",
    "vnet peering", "key rotation", "certificate revocation",
)
_DRAFT_PR_KEYWORDS = (
    "pull request", " pr ", "terraform", "policy-as-code", "policy as code",
    "infrastructure as code", "iac change", "merge request",
)
_DRAFT_TICKET_KEYWORDS = (
    "work item", "ticket", "backlog item", "pbi", "bug report", "task item",
    "file a bug", "open a bug", "create a task",
)
_REVERSIBLE_NONPROD_KEYWORDS = (
    "restart", "reboot", "scale", "resize", "non-production", "nonprod",
    "staging environment", "dev environment", "test environment",
)
_READ_ONLY_KEYWORDS = (
    "review", "investigate", "monitor", "watch", "document", "no action",
    "escalate to", "notify", "report to", "acknowledge",
)


def classify_action_text(description: str) -> ApprovalTier:
    """Deterministic, keyword-based approval-tier classification of a
    free-text action description. Unclassified/ambiguous text defaults
    to draft_ticket (require confirmation) rather than read_only --
    "safe to view" is never assumed for text this module can't
    positively classify as informational."""
    text = f" {(description or '').strip().lower()} "
    if not text.strip():
        return ApprovalTier.DRAFT_TICKET
    if any(keyword in text for keyword in _PRODUCTION_WRITE_KEYWORDS):
        return ApprovalTier.PRODUCTION_WRITE
    if any(keyword in text for keyword in _DRAFT_PR_KEYWORDS):
        return ApprovalTier.DRAFT_PR
    if any(keyword in text for keyword in _REVERSIBLE_NONPROD_KEYWORDS):
        return ApprovalTier.REVERSIBLE_NONPROD
    if any(keyword in text for keyword in _DRAFT_TICKET_KEYWORDS):
        return ApprovalTier.DRAFT_TICKET
    if any(keyword in text for keyword in _READ_ONLY_KEYWORDS):
        return ApprovalTier.READ_ONLY
    return ApprovalTier.DRAFT_TICKET


def approval_metadata(tier: ApprovalTier, *, allowlisted: bool = False, execution_capable: bool = False) -> dict:
    """Deterministic approval metadata for `tier`.

    `execution_capable` must be True ONLY for a caller that can actually
    execute the action (this app has no such caller today -- every
    surface that reaches this function is read-only/proposal-only). When
    False (the default), `auto_executable` is always False regardless of
    tier/allowlist -- see analysis_action_metadata, which hardcodes it.
    """
    if tier in _ALWAYS_HUMAN_APPROVED:
        human_approval_required = True
        auto_executable = False
    elif tier in _NEVER_REQUIRES_APPROVAL:
        human_approval_required = False
        auto_executable = False  # "safe to view" is not "safe to auto-run"
    else:
        # autonomous / draft_ticket / draft_pr / reversible_nonprod: all
        # require confirmation UNLESS explicitly allowlisted.
        human_approval_required = not allowlisted
        auto_executable = bool(execution_capable and allowlisted)

    return {
        "tier": tier.value,
        "human_approval_required": human_approval_required,
        "auto_executable": auto_executable,
        "allowlisted": allowlisted,
    }


def analysis_action_metadata(description: str) -> dict:
    """Approval metadata for one recommended_action surfaced by the
    read-only operations-analysis endpoint (app/agents/analysis.py).

    `execution_capable=False` is hardcoded here -- never passed in --
    because that endpoint has no code path that executes anything. This
    IS the task-adherence guarantee: a read-only analysis can never mark
    an action auto_executable=True, regardless of the model's own
    self-reported `approval_required` or how safe the action sounds.
    """
    tier = classify_action_text(description)
    metadata = approval_metadata(tier, allowlisted=False, execution_capable=False)
    if metadata["auto_executable"]:
        # Unreachable given execution_capable=False above -- fail loudly
        # rather than silently if a future edit ever breaks this.
        raise TaskAdherenceError(
            "read-only analysis produced an auto_executable action; this must never happen"
        )
    return metadata


# app.ado_integration.ProposalType values -> ApprovalTier. Every ADO
# proposal is already human-gated (PENDING until a human calls
# approve_proposal/reject_proposal) -- this mapping only ATTACHES
# descriptive tier metadata to that existing flow, it does not change it.
_PROPOSAL_TYPE_TIER = {
    "pull_request": ApprovalTier.DRAFT_PR,
    "pbi": ApprovalTier.DRAFT_TICKET,
    "bug": ApprovalTier.DRAFT_TICKET,
    "task": ApprovalTier.DRAFT_TICKET,
}


def proposal_approval_tier(proposal_type: str) -> ApprovalTier:
    """Deterministic tier for an app.ado_integration.AdoProposal, from its
    `proposal_type` string value."""
    return _PROPOSAL_TYPE_TIER.get(proposal_type, ApprovalTier.DRAFT_TICKET)
