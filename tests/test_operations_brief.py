#!/usr/bin/env python3
"""Test the executive brief service (app/operations/brief.py) -- overall
state derivation, truthful "not_configured"/"unknown" states (never a
fabricated number), bounded lists, and evidence sanitization (no
subscription id leaks).

Run: python3 tests/test_operations_brief.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.brief import build_brief  # noqa: E402
from app.operations.models import (  # noqa: E402
    CapacitySummary, ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus,
    SLOSummary, Severity,
)
from app.operations.service import CollectionEnvelope, summarize_coverage  # noqa: E402
from app.operations.snapshot import OperationsSnapshot  # noqa: E402

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


def make_finding(disc, *, category=FindingCategory.SECURITY.value, severity=Severity.HIGH.value, exec_att=True, approval=False, resource_id=None):
    return Finding(
        category=category, severity=severity, status=FindingStatus.OPEN.value,
        title=f"Finding {disc}", summary="s", business_impact="impact text",
        first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
        source=EvidenceSource.RESOURCE_GRAPH.value, confidence=ConfidenceLevel.CONFIRMED.value, resource_id=resource_id,
        evidence=[EvidenceReference(source=EvidenceSource.RESOURCE_GRAPH.value, title="t", observed_at="2025-06-01T00:00:00Z", resource_id=resource_id)],
        executive_attention=exec_att, approval_required=approval, discriminator=disc,
    )


def make_snapshot(findings_with_workflow, envelopes, *, status="ok"):
    return OperationsSnapshot(
        id="snap-test", generated_at="2025-06-01T00:00:00.000Z", subscription_ids=("sub1",), status=status,
        envelopes=envelopes, findings=findings_with_workflow, coverage=summarize_coverage(envelopes),
        source_errors=[], summary={},
    )


def wrap(finding, *, workflow_status="new", customer_impact=True):
    return {
        "finding": finding.to_dict(),
        "workflow": {"status": workflow_status, "assigned_owner": "", "disposition_reason": "", "snooze_until": None, "first_seen_at": None, "created_at": None, "updated_at": None},
        "priority": {"band": "P1", "factors": {"customer_impact": customer_impact, "severity_rank": 0, "slo_state": None, "slo_state_rank": 2, "age_hours": 1.0, "confidence_rank": 0}},
    }


_REALISTIC_NOT_CONFIGURED_ENVELOPES = [
    CollectionEnvelope(source="workload_slo", status="not_configured", collected_at="2025-06-01T00:00:00.000Z", error="no SLOs configured"),
    CollectionEnvelope(source="capacity", status="not_configured", collected_at="2025-06-01T00:00:00.000Z", error="no locations supplied"),
]


print("\n\U0001f9ea Test 1: build_brief -- 'impact' state for an open, high-severity, customer-impacting finding")
f1 = make_finding("f1", category=FindingCategory.INCIDENT.value, severity=Severity.CRITICAL.value, exec_att=True, resource_id="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg1/providers/x")
snap = make_snapshot([wrap(f1)], _REALISTIC_NOT_CONFIGURED_ENVELOPES)
brief = build_brief(snap, now=NOW)
test("overall_state is 'impact'", brief["overall_state"] == "impact")
test("business_impact count is 1", brief["business_impact"]["active_customer_impacting_count"] == 1)
test("headline references the top finding's title", f1.title in brief["headline"])
test("evidence in business_impact details never includes resource_id (no subscription id leak)", all("resource_id" not in e for e in brief["business_impact"]["details"][0]["evidence"]))


print("\n\U0001f9ea Test 2: build_brief -- 'healthy' state when nothing is notable")
f2 = make_finding("f2", category=FindingCategory.COST.value, severity=Severity.LOW.value, exec_att=False, approval=False)
snap2 = make_snapshot([wrap(f2, customer_impact=False)], _REALISTIC_NOT_CONFIGURED_ENVELOPES)
brief2 = build_brief(snap2, now=NOW)
test("overall_state is 'healthy'", brief2["overall_state"] == "healthy")
test("headline is the all-clear message", "No active customer-impacting issues" in brief2["headline"])
test("business_impact count is 0", brief2["business_impact"]["active_customer_impacting_count"] == 0)


print("\n\U0001f9ea Test 3: build_brief -- 'unknown' when the snapshot status is 'error'")
snap3 = make_snapshot([], [CollectionEnvelope(source="azure_monitor_alerts", status="error", collected_at="2025-06-01T00:00:00.000Z", error="boom")], status="error")
brief3 = build_brief(snap3, now=NOW)
test("overall_state is 'unknown'", brief3["overall_state"] == "unknown")
test("headline reflects insufficient coverage", "Insufficient source coverage" in brief3["headline"])


print("\n\U0001f9ea Test 4: build_brief -- reliability/capacity 'not_configured' truthfully (no fake numbers)")
snap4 = make_snapshot([], [
    CollectionEnvelope(source="workload_slo", status="not_configured", collected_at="2025-06-01T00:00:00.000Z", error="no SLOs configured"),
    CollectionEnvelope(source="capacity", status="not_configured", collected_at="2025-06-01T00:00:00.000Z", error="no locations supplied"),
])
brief4 = build_brief(snap4, now=NOW)
test("reliability.slo_configured is False", brief4["reliability"]["slo_configured"] is False)
test("reliability.state is 'not_configured'", brief4["reliability"]["state"] == "not_configured")
test("reliability.error_budget_remaining_pct is None (never fabricated)", brief4["reliability"]["error_budget_remaining_pct"] is None)
test("capacity.configured is False", brief4["capacity"]["configured"] is False)
test("capacity.state is 'not_configured'", brief4["capacity"]["state"] == "not_configured")
test("capacity.minimum_headroom_pct is None (never fabricated)", brief4["capacity"]["minimum_headroom_pct"] is None)
test("overall_state is 'healthy' (not_configured alone isn't an attention trigger)", brief4["overall_state"] == "healthy")


print("\n\U0001f9ea Test 5: build_brief -- reliability breached/capacity critical drive 'attention'")
slo_breached = SLOSummary(
    workload="checkout", state="breached", objective_pct=99.9, observed_pct=95.0, window_hours=24,
    criticality="internal", evaluated_at="2025-06-01T00:00:00.000Z", error_budget_remaining_pct=0.0,
)
cap_critical = CapacitySummary(
    resource_scope="compute:eastus", metric="cores", current=95, limit=100, threshold_state="critical",
    evaluated_at="2025-06-01T00:00:00.000Z", headroom_pct=5.0,
)
snap5 = make_snapshot([], [
    CollectionEnvelope(source="workload_slo", status="ok", collected_at="2025-06-01T00:00:00.000Z", summaries=[slo_breached]),
    CollectionEnvelope(source="capacity", status="ok", collected_at="2025-06-01T00:00:00.000Z", summaries=[cap_critical]),
])
brief5 = build_brief(snap5, now=NOW)
test("reliability.state is 'breached'", brief5["reliability"]["state"] == "breached")
test("capacity.state is 'critical'", brief5["capacity"]["state"] == "critical")
test("capacity.minimum_headroom_pct reflects the summary", brief5["capacity"]["minimum_headroom_pct"] == 5.0)
test("capacity.nearest_constraint identifies the scope/metric", brief5["capacity"]["nearest_constraint"] == "compute:eastus/cores")
test("overall_state is 'attention' (breached SLO + critical capacity, no direct customer-impact finding)", brief5["overall_state"] == "attention")


print("\n\U0001f9ea Test 6: build_brief -- decisions_required/attention_items bounded to 3, exclude snoozed")
findings = [make_finding(f"appr-{i}", approval=True, exec_att=False, category=FindingCategory.COMPLIANCE.value) for i in range(5)]
wrapped = [wrap(f, customer_impact=False) for f in findings]
wrapped[0]["workflow"]["status"] = "snoozed"  # should be excluded from decisions_required
snap6 = make_snapshot(wrapped, [])
brief6 = build_brief(snap6, now=NOW)
test("decisions_required is capped at 3", len(brief6["decisions_required"]) == 3)
test("a snoozed approval-required item is excluded", findings[0].id not in {d["id"] for d in brief6["decisions_required"]})


print("\n\U0001f9ea Test 7: build_brief -- data_freshness reflects age_seconds")
brief7 = build_brief(snap2, now=NOW)
test("age_seconds is 0 when now == generated_at", brief7["data_freshness"]["age_seconds"] == 0.0)


print("\n\U0001f9ea Test 8: build_brief -- a Defender source error with NO findings must never be reported 'healthy'")
# Regression for: brief.py used to only look at capacity/reliability's
# OWN envelope status (and business_impact/attention/decisions) when
# deciding overall_state -- a source with no dedicated brief section
# (e.g. defender_alerts, cost_management_budget, azure_backup, ...)
# that failed to collect, but happened to have produced zero Findings
# from any OTHER source either, fell through every check and was
# reported overall_state == 'healthy' with the literal headline "...all
# monitored sources report healthy" -- even though one source's
# evidence was entirely missing. source_coverage.error_count > 0 must
# now always push overall_state to at least 'attention'.
snap8 = make_snapshot([], _REALISTIC_NOT_CONFIGURED_ENVELOPES + [
    CollectionEnvelope(source="defender_alerts", status="error", collected_at="2025-06-01T00:00:00.000Z", error="Microsoft.Security/alerts returned HTTP 503"),
], status="partial")
brief8 = build_brief(snap8, now=NOW)
test("overall_state is 'attention', never 'healthy', when a source errored", brief8["overall_state"] == "attention")
test("source_coverage.error_count is 1 (the failing defender_alerts source)", brief8["source_coverage"]["error_count"] == 1)
test("headline never claims 'all monitored sources report healthy'", "all monitored sources report healthy" not in brief8["headline"])
test("headline honestly names the incomplete evidence coverage", "incomplete" in brief8["headline"].lower())
test("headline names the specific failing source", "defender_alerts" in brief8["headline"])
test("business_impact is still truthfully 0 (no fabricated impact from an unrelated source error)", brief8["business_impact"]["active_customer_impacting_count"] == 0)

print("\n\U0001f9ea Test 8b: build_brief -- healthy is ONLY reported when error_count == 0")
snap8b = make_snapshot([], _REALISTIC_NOT_CONFIGURED_ENVELOPES)  # no error envelopes at all
brief8b = build_brief(snap8b, now=NOW)
test("error_count is 0 with no error envelopes", brief8b["source_coverage"]["error_count"] == 0)
test("overall_state is 'healthy' only when error_count == 0", brief8b["overall_state"] == "healthy")
test("the all-clear headline is only used when truly nothing failed", "all monitored sources report healthy" in brief8b["headline"])

print("\n\U0001f9ea Test 8c: build_brief -- an executive_attention Finding still takes headline priority over a coverage error")
f8c = make_finding("f8c", category=FindingCategory.COMPLIANCE.value, severity=Severity.MEDIUM.value, exec_att=True, approval=False)
snap8c = make_snapshot([wrap(f8c, customer_impact=False)], _REALISTIC_NOT_CONFIGURED_ENVELOPES + [
    CollectionEnvelope(source="azure_backup", status="error", collected_at="2025-06-01T00:00:00.000Z", error="boom"),
])
brief8c = build_brief(snap8c, now=NOW)
test("overall_state is 'attention'", brief8c["overall_state"] == "attention")
test("headline still leads with the concrete attention item, not the coverage gap", f8c.title in brief8c["headline"])


print("\n\U0001f9ea Test 9: build_brief -- output schema stays UI/API-compatible (docs/OPERATIONS_API.md)")
expected_brief_keys = {
    "overall_state", "headline", "updated_at", "data_freshness", "business_impact", "reliability", "capacity",
    "changes_since_yesterday", "decisions_required", "attention_items", "source_coverage", "snapshot_id",
}
test("build_brief's top-level keys are unchanged", set(brief8.keys()) == expected_brief_keys)
test("overall_state is still one of the 4 documented values", brief8["overall_state"] in ("healthy", "attention", "impact", "unknown"))


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
