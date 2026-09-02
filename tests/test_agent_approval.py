#!/usr/bin/env python3
"""Test approval-tier policy (app/approval.py) -- deterministic
classification, the six explicit tiers, and the task-adherence
guarantee that a read-only analysis surface can never mark an action
auto_executable.

Run: python3 tests/test_agent_approval.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.approval import (  # noqa: E402
    ApprovalTier, analysis_action_metadata, approval_metadata, classify_action_text, proposal_approval_tier,
)

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


print("\n\U0001f9ea Test 1: classify_action_text -- deterministic keyword classification")
test("delete -> production_write", classify_action_text("Delete the orphaned disk") == ApprovalTier.PRODUCTION_WRITE)
test("RBAC change -> production_write", classify_action_text("Update the RBAC role assignment for this group") == ApprovalTier.PRODUCTION_WRITE)
test("production keyword -> production_write", classify_action_text("Apply this change directly to the production VM") == ApprovalTier.PRODUCTION_WRITE)
test("pull request -> draft_pr", classify_action_text("Open a pull request with the Terraform fix") == ApprovalTier.DRAFT_PR)
test("work item -> draft_ticket", classify_action_text("File a work item to track the investigation") == ApprovalTier.DRAFT_TICKET)
test("restart a dev VM -> reversible_nonprod", classify_action_text("Restart the dev environment VM") == ApprovalTier.REVERSIBLE_NONPROD)
test("review/investigate -> read_only", classify_action_text("Review the alert history and monitor for recurrence") == ApprovalTier.READ_ONLY)
test("empty description -> draft_ticket (never assumed safe)", classify_action_text("") == ApprovalTier.DRAFT_TICKET)
test("unclassifiable text -> draft_ticket (never assumed safe)", classify_action_text("Do the thing with the widget") == ApprovalTier.DRAFT_TICKET)


print("\n\U0001f9ea Test 2: classify_action_text -- most-restrictive tier wins on ambiguous text")
test(
    "restart + production together classify as production_write, not reversible_nonprod",
    classify_action_text("Restart the production database server") == ApprovalTier.PRODUCTION_WRITE,
)


print("\n\U0001f9ea Test 3: approval_metadata -- production_write is ALWAYS human_approved, no allowlist exception")
meta_prod = approval_metadata(ApprovalTier.PRODUCTION_WRITE, allowlisted=True, execution_capable=True)
test("production_write requires human approval even when allowlisted+execution_capable", meta_prod["human_approval_required"] is True)
test("production_write is never auto_executable", meta_prod["auto_executable"] is False)


print("\n\U0001f9ea Test 4: approval_metadata -- read_only never requires approval, never auto-executes")
meta_read_only = approval_metadata(ApprovalTier.READ_ONLY)
test("read_only never requires approval", meta_read_only["human_approval_required"] is False)
test("read_only is never auto_executable ('safe to view' != 'safe to auto-run')", meta_read_only["auto_executable"] is False)


print("\n\U0001f9ea Test 5: approval_metadata -- confirmation tiers require approval UNLESS allowlisted+execution_capable")
meta_draft_default = approval_metadata(ApprovalTier.DRAFT_TICKET)
test("draft_ticket requires approval by default", meta_draft_default["human_approval_required"] is True)
test("draft_ticket is not auto_executable by default", meta_draft_default["auto_executable"] is False)

meta_draft_allowlisted = approval_metadata(ApprovalTier.DRAFT_TICKET, allowlisted=True, execution_capable=True)
test("draft_ticket does NOT require approval when allowlisted+execution_capable", meta_draft_allowlisted["human_approval_required"] is False)
test("draft_ticket CAN be auto_executable only when both allowlisted and execution_capable", meta_draft_allowlisted["auto_executable"] is True)

meta_draft_allowlisted_not_capable = approval_metadata(ApprovalTier.DRAFT_TICKET, allowlisted=True, execution_capable=False)
test("allowlisted alone (no execution_capable) never auto-executes", meta_draft_allowlisted_not_capable["auto_executable"] is False)


print("\n\U0001f9ea Test 6: task adherence -- analysis_action_metadata NEVER returns auto_executable=True")
sample_descriptions = [
    "Review the finding and monitor for recurrence",
    "Restart the dev environment VM",
    "Open a pull request with the Terraform fix",
    "File a work item to track the investigation",
    "Delete the orphaned disk",
    "Update the RBAC role assignment for this group",
    "Apply a reserved instance cost commitment for this workload",
    "",
    "Something totally unclassifiable and vague",
]
all_never_executable = True
for description in sample_descriptions:
    metadata = analysis_action_metadata(description)
    if metadata["auto_executable"] is not False:
        all_never_executable = False
test("every sampled action description -> auto_executable is always False", all_never_executable)

meta_for_delete = analysis_action_metadata("Delete the orphaned disk")
test("a production_write-classified action still surfaces tier metadata", meta_for_delete["tier"] == ApprovalTier.PRODUCTION_WRITE.value)
test("production_write action from analysis still requires human approval", meta_for_delete["human_approval_required"] is True)


print("\n\U0001f9ea Test 7: proposal_approval_tier maps ADO proposal types deterministically")
test("pull_request -> draft_pr", proposal_approval_tier("pull_request") == ApprovalTier.DRAFT_PR)
test("pbi -> draft_ticket", proposal_approval_tier("pbi") == ApprovalTier.DRAFT_TICKET)
test("bug -> draft_ticket", proposal_approval_tier("bug") == ApprovalTier.DRAFT_TICKET)
test("task -> draft_ticket", proposal_approval_tier("task") == ApprovalTier.DRAFT_TICKET)
test("unknown proposal type -> draft_ticket (safe default)", proposal_approval_tier("something-else") == ApprovalTier.DRAFT_TICKET)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
