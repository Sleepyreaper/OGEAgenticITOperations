#!/usr/bin/env python3
"""Test deterministic Finding prioritization
(app/operations/priority.py) -- band assignment/sort order, and
especially `is_customer_impacting`'s strict contract: True ONLY when a
Finding itself carries `customer_impacting=True`, NEVER inferred from
`executive_attention`, `severity`, or `category` alone. This directly
regression-tests a real defect where merely being flagged
`executive_attention` (or being in the `incident`/`reliability`
category) inflated the executive brief's "active customer-impacting
issue" count with things like an authorized stopped/deallocated VM, a
policy compliance gap, and a capacity/quota Finding -- none of which are
current customer impact.

Run: python3 tests/test_operations_priority.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.models import Finding, FindingCategory, Severity, format_utc_iso  # noqa: E402
from app.operations.priority import PriorityBand, is_customer_impacting, prioritize_findings  # noqa: E402

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


def make_finding(**overrides):
    defaults = dict(
        category=FindingCategory.RELIABILITY.value, severity=Severity.HIGH.value, status="open",
        title="t", summary="s", business_impact="b",
        first_seen=format_utc_iso(NOW), last_seen=format_utc_iso(NOW), source="azure_monitor_alert",
    )
    defaults.update(overrides)
    return Finding(**defaults)


# ─── is_customer_impacting -- strict, field-only contract ──────────────
print("\n\U0001f9ea Test 1: is_customer_impacting -- never inferred from executive_attention/severity/category alone")

plain_reliability = make_finding(category=FindingCategory.RELIABILITY.value, executive_attention=False)
test("a plain reliability Finding (no explicit evidence) is NOT customer_impacting", is_customer_impacting(plain_reliability) is False)

plain_incident = make_finding(category=FindingCategory.INCIDENT.value, executive_attention=False)
test("a plain incident Finding (category alone) is NOT customer_impacting -- category is never sufficient evidence on its own", is_customer_impacting(plain_incident) is False)

exec_attention_compliance = make_finding(category=FindingCategory.COMPLIANCE.value, severity=Severity.HIGH.value, executive_attention=True)
test("an executive_attention compliance Finding is NOT customer_impacting -- compliance can demand attention without being current customer impact", is_customer_impacting(exec_attention_compliance) is False)

exec_attention_capacity = make_finding(category=FindingCategory.CAPACITY.value, severity=Severity.HIGH.value, executive_attention=True)
test("an executive_attention capacity/quota Finding is NOT customer_impacting", is_customer_impacting(exec_attention_capacity) is False)

exec_attention_security = make_finding(category=FindingCategory.SECURITY.value, severity=Severity.CRITICAL.value, executive_attention=True)
test("an executive_attention CRITICAL security Finding is NOT customer_impacting -- a risk vector, not confirmed impact", is_customer_impacting(exec_attention_security) is False)

explicit_true = make_finding(category=FindingCategory.RELIABILITY.value, executive_attention=False, customer_impacting=True)
test("a Finding with customer_impacting=True set explicitly IS customer_impacting, regardless of category/executive_attention", is_customer_impacting(explicit_true) is True)

explicit_false_despite_attention = make_finding(category=FindingCategory.INCIDENT.value, severity=Severity.CRITICAL.value, executive_attention=True, customer_impacting=False)
test("customer_impacting=False overrides even a CRITICAL, executive_attention incident Finding", is_customer_impacting(explicit_false_despite_attention) is False)


# ─── prioritize_findings -- factors.customer_impact reflects the field ──
print("\n\U0001f9ea Test 2: prioritize_findings -- factors.customer_impact mirrors Finding.customer_impacting exactly")
impacting = make_finding(category=FindingCategory.INCIDENT.value, severity=Severity.MEDIUM.value, customer_impacting=True, discriminator="a")
non_impacting = make_finding(category=FindingCategory.INCIDENT.value, severity=Severity.MEDIUM.value, customer_impacting=False, discriminator="b")
prioritized = prioritize_findings([impacting, non_impacting], now=NOW + timedelta(hours=1))
by_id = {pf.finding.id: pf for pf in prioritized}
test("the customer_impacting=True Finding's factors.customer_impact is True", by_id[impacting.id].factors.customer_impact is True)
test("the customer_impacting=False Finding's factors.customer_impact is False", by_id[non_impacting.id].factors.customer_impact is False)
test(
    "the customer-impacting Finding outranks the otherwise-identical (MEDIUM severity) non-impacting one (P2 vs P3)",
    by_id[impacting.id].band == PriorityBand.P2_HIGH.value and by_id[non_impacting.id].band == PriorityBand.P3_MEDIUM.value,
)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
