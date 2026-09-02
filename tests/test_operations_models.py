#!/usr/bin/env python3
"""Test the evidence model's schema validation, JSON serialization,
deterministic ID generation, and priority sorting (app/operations/models.py,
app/operations/identifiers.py, app/operations/priority.py).

No Azure/network calls -- pure data structures.

Run: python3 tests/test_operations_models.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.models import (  # noqa: E402
    ActionItem,
    CapacitySummary,
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    SLOSummary,
    Severity,
    ensure_utc_iso,
    format_utc_iso,
    parse_utc_iso,
    utc_now,
)
from app.operations.priority import prioritize_findings  # noqa: E402

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


NOW = datetime.now(timezone.utc)


def make_finding(**overrides):
    defaults = dict(
        category=FindingCategory.INCIDENT.value,
        severity=Severity.HIGH.value,
        status=FindingStatus.OPEN.value,
        title="A firing alert",
        summary="Something is on fire.",
        business_impact="Customers may see errors.",
        first_seen=NOW - timedelta(hours=2),
        last_seen=NOW,
        source=EvidenceSource.MANUAL.value,
    )
    defaults.update(overrides)
    return Finding(**defaults)


# ─── Timestamp helpers ───────────────────────────────────────────────
print("\n\U0001f9ea Test 1: timestamp helpers -- UTC-only, canonical formatting")
try:
    format_utc_iso(datetime(2024, 1, 1))
    test("format_utc_iso raises on naive datetime", False)
except ValueError:
    test("format_utc_iso raises on naive datetime", True)

test(
    "format_utc_iso normalizes a non-UTC tz to UTC 'Z' form",
    format_utc_iso(datetime(2024, 1, 1, 5, 0, 0, tzinfo=timezone(timedelta(hours=5)))) == "2024-01-01T00:00:00.000Z",
)

try:
    parse_utc_iso("2024-01-01T00:00:00")  # no offset
    test("parse_utc_iso rejects a naive ISO string (no offset/'Z')", False)
except ValueError:
    test("parse_utc_iso rejects a naive ISO string (no offset/'Z')", True)

test(
    "ensure_utc_iso accepts a 'Z'-suffixed string and round-trips through parse_utc_iso",
    parse_utc_iso(ensure_utc_iso("2024-06-01T12:30:00Z")) == datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc),
)
test(
    "ensure_utc_iso accepts a tz-aware datetime directly",
    ensure_utc_iso(datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc)) == "2024-06-01T12:30:00.000Z",
)
test("utc_now() is timezone-aware", utc_now().tzinfo is not None)


# ─── EvidenceReference validation + sanitization ─────────────────────
print("\n\U0001f9ea Test 2: EvidenceReference -- validation and raw_excerpt sanitization")
try:
    EvidenceReference(source="not_a_real_source", title="t", observed_at=NOW)
    test("EvidenceReference rejects an unrecognized source", False)
except ValueError:
    test("EvidenceReference rejects an unrecognized source", True)

try:
    EvidenceReference(source=EvidenceSource.MANUAL.value, title="   ", observed_at=NOW)
    test("EvidenceReference rejects a blank title", False)
except ValueError:
    test("EvidenceReference rejects a blank title", True)

ref = EvidenceReference(
    source=EvidenceSource.MANUAL.value, title="t", observed_at=NOW,
    raw_excerpt="Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
)
test("raw_excerpt redacts an Authorization header value", "abcdefghijklmnopqrstuvwxyz123456" not in ref.raw_excerpt)
test("raw_excerpt keeps the REDACTED marker", "REDACTED" in ref.raw_excerpt)

long_ref = EvidenceReference(source=EvidenceSource.MANUAL.value, title="t", observed_at=NOW, raw_excerpt="x" * 1000)
test("raw_excerpt is bounded in length", len(long_ref.raw_excerpt) < 1000)
test("raw_excerpt truncation is noted", "truncated" in long_ref.raw_excerpt)

test("EvidenceReference.to_dict round-trips the core fields", ref.to_dict()["source"] == EvidenceSource.MANUAL.value)


# ─── Finding validation ───────────────────────────────────────────────
print("\n\U0001f9ea Test 3: Finding -- strict validation")
try:
    make_finding(category="not_a_category")
    test("Finding rejects an unrecognized category", False)
except ValueError:
    test("Finding rejects an unrecognized category", True)

try:
    make_finding(severity="not_a_severity")
    test("Finding rejects an unrecognized severity", False)
except ValueError:
    test("Finding rejects an unrecognized severity", True)

try:
    make_finding(last_seen=NOW - timedelta(hours=5))  # before first_seen
    test("Finding rejects last_seen before first_seen", False)
except ValueError:
    test("Finding rejects last_seen before first_seen", True)

try:
    make_finding(affected_resource_count=-1)
    test("Finding rejects a negative affected_resource_count", False)
except ValueError:
    test("Finding rejects a negative affected_resource_count", True)

try:
    Finding(
        category=FindingCategory.INCIDENT.value, severity=Severity.HIGH.value, status=FindingStatus.OPEN.value,
        title="t", summary="s", business_impact="b", first_seen=NOW, last_seen=NOW,
        source=EvidenceSource.MANUAL.value, evidence=["not an EvidenceReference"],
    )
    test("Finding rejects a non-EvidenceReference in evidence[]", False)
except TypeError:
    test("Finding rejects a non-EvidenceReference in evidence[]", True)

f = make_finding()
test("Finding.to_dict includes a stable id", bool(f.to_dict()["id"]))
test("Finding.to_dict serializes evidence as a list of dicts", f.to_dict()["evidence"] == [])


# ─── Deterministic IDs ────────────────────────────────────────────────
print("\n\U0001f9ea Test 4: deterministic Finding IDs")
f1 = make_finding(discriminator="alert-123")
f2 = make_finding(discriminator="alert-123", first_seen=NOW - timedelta(days=3))  # different timestamp, same identity
test("same (category, source, resource_id, discriminator) -> same id regardless of timestamps", f1.id == f2.id)

f3 = make_finding(discriminator="alert-456")
test("a different discriminator produces a different id", f1.id != f3.id)

f4 = make_finding(discriminator="alert-123", resource_id="/subscriptions/x/y/z")
test("a different resource_id produces a different id", f1.id != f4.id)

f5 = make_finding(discriminator="alert-123", category=FindingCategory.CAPACITY.value)
test("id is prefixed with a category-derived short code", f5.id.startswith("cap-"))
test("incident-category id uses the 'inc-' prefix", f1.id.startswith("inc-"))

explicit = make_finding(id="my-explicit-id", discriminator="alert-123")
test("an explicitly supplied id is not overwritten", explicit.id == "my-explicit-id")


# ─── ActionItem ───────────────────────────────────────────────────────
print("\n\U0001f9ea Test 5: ActionItem -- validation and deterministic id")
item = ActionItem(finding_id=f1.id, title="Roll back the change")
test("ActionItem computes a stable id", item.id.startswith("act-"))
item2 = ActionItem(finding_id=f1.id, title="Roll back the change")
test("same finding_id/title -> same ActionItem id", item.id == item2.id)
try:
    ActionItem(finding_id=f1.id, title="t", status="not_a_status")
    test("ActionItem rejects an unrecognized status", False)
except ValueError:
    test("ActionItem rejects an unrecognized status", True)


# ─── SLOSummary / CapacitySummary validation ─────────────────────────
print("\n\U0001f9ea Test 6: SLOSummary / CapacitySummary -- range/state validation")
try:
    SLOSummary(
        workload="w", state="not_a_state", objective_pct=99.9, observed_pct=99.9,
        window_hours=24, criticality="customer_facing", evaluated_at=NOW,
    )
    test("SLOSummary rejects an unrecognized state", False)
except ValueError:
    test("SLOSummary rejects an unrecognized state", True)

try:
    SLOSummary(
        workload="w", state="healthy", objective_pct=150, observed_pct=99.9,
        window_hours=24, criticality="customer_facing", evaluated_at=NOW,
    )
    test("SLOSummary rejects objective_pct > 100", False)
except ValueError:
    test("SLOSummary rejects objective_pct > 100", True)

try:
    CapacitySummary(
        resource_scope="compute:eastus", metric="cores", current=50, limit=100,
        threshold_state="not_a_state", evaluated_at=NOW,
    )
    test("CapacitySummary rejects an unrecognized threshold_state", False)
except ValueError:
    test("CapacitySummary rejects an unrecognized threshold_state", True)

try:
    CapacitySummary(
        resource_scope="compute:eastus", metric="cores", current=50, limit=100,
        threshold_state="healthy", evaluated_at=NOW,
        forecast_state="not_available", forecast_exhaustion_at=NOW,
    )
    test("CapacitySummary rejects forecast_exhaustion_at set without forecast_state == 'available'", False)
except ValueError:
    test("CapacitySummary rejects forecast_exhaustion_at set without forecast_state == 'available'", True)


# ─── Priority sorting (bands + explainable factors, not an opaque score) ──
print("\n\U0001f9ea Test 7: prioritize_findings -- bands, factor transparency, tie-breaking")
low = make_finding(category=FindingCategory.COST.value, severity=Severity.LOW.value, first_seen=NOW - timedelta(hours=1))
critical_new = make_finding(severity=Severity.CRITICAL.value, first_seen=NOW - timedelta(hours=1))
critical_old = make_finding(severity=Severity.CRITICAL.value, first_seen=NOW - timedelta(hours=100), discriminator="old-one")
medium = make_finding(category=FindingCategory.CAPACITY.value, severity=Severity.MEDIUM.value, first_seen=NOW - timedelta(hours=5))

ranked = prioritize_findings([low, critical_new, medium, critical_old], now=NOW)
bands = [p.band for p in ranked]
test("critical findings rank P1", bands[0] == "P1" and bands[1] == "P1")
test("medium finding ranks P3", "P3" in bands)
test("low, non-customer-impacting finding ranks P4 (lowest)", bands[-1] == "P4")
test("older critical finding sorts before the newer one (age tie-break)", ranked[0].finding.id == critical_old.id)
test("factors are exposed (not an opaque score)", hasattr(ranked[0].factors, "severity_rank") and hasattr(ranked[0].factors, "age_hours"))
test("factors.to_dict() is JSON-safe", isinstance(ranked[0].factors.to_dict(), dict))

# SLO linkage via metadata["workload"]
slo_finding = make_finding(
    category=FindingCategory.CAPACITY.value, severity=Severity.LOW.value,
    metadata={"workload": "checkout-api"}, discriminator="slo-linked",
)
ranked_slo = prioritize_findings([slo_finding], slo_state_by_workload={"checkout-api": "breached"}, now=NOW)
test("a low-severity Finding linked to a breached SLO is boosted to P1", ranked_slo[0].band == "P1")
test("the SLO state is surfaced on factors", ranked_slo[0].factors.slo_state == "breached")

unrelated_finding = make_finding(category=FindingCategory.COST.value, severity=Severity.LOW.value, discriminator="no-slo")
ranked_unrelated = prioritize_findings([unrelated_finding], slo_state_by_workload={"checkout-api": "breached"}, now=NOW)
test("a Finding with no related workload is unaffected by an unrelated SLO breach", ranked_unrelated[0].band == "P4")


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
