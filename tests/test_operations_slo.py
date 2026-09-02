#!/usr/bin/env python3
"""Test configurable workload SLOs (app/operations/collectors/slo.py) --
definition loading/validation, and the healthy/at_risk/breached/
insufficient_data/not_configured states. query_logs_fn is injected; no
real Log Analytics/network call is made.

Run: python3 tests/test_operations_slo.py
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import slo  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402
from app.operations.models import ConfidenceLevel, FindingCategory  # noqa: E402

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


def rows_fn(rows):
    return lambda query, workspace_id, timespan: rows


def make_definition(**overrides):
    defaults = dict(workload="checkout-api", query="AppRequests | summarize good=count(), total=count()",
                     objective_pct=99.9, window_hours=720, at_risk_burn_rate=2.0)
    defaults.update(overrides)
    return slo.SLODefinition(**defaults)


# ─── load_slo_definitions -- explicit not-configured vs. configured ────
print("\n\U0001f9ea Test 1: load_slo_definitions -- empty when neither source is configured")
test("no config_path/config_json -> [] (explicit not-configured signal)", slo.load_slo_definitions() == [])

print("\n\U0001f9ea Test 2: load_slo_definitions -- config_json (inline)")
valid_json = json.dumps({"slos": [{
    "workload": "checkout-api", "query": "AppRequests | summarize good=count(), total=count()",
    "objective_pct": 99.9, "window_hours": 720, "criticality": "customer_facing",
}]})
defs = slo.load_slo_definitions(config_json=valid_json)
test("returns one SLODefinition", len(defs) == 1)
test("workload is parsed", defs[0].workload == "checkout-api")
test("objective_pct is parsed as a float", defs[0].objective_pct == 99.9)

try:
    slo.load_slo_definitions(config_json="not valid json")
    test("invalid JSON in config_json raises ValueError", False)
except ValueError:
    test("invalid JSON in config_json raises ValueError", True)

try:
    slo.load_slo_definitions(config_json=json.dumps({"not_slos": []}))
    test("JSON missing the top-level 'slos' key raises ValueError", False)
except ValueError:
    test("JSON missing the top-level 'slos' key raises ValueError", True)

try:
    slo.load_slo_definitions(config_json=json.dumps({"slos": [{"workload": "x"}]}))  # missing query/objective_pct
    test("an SLO entry missing required fields raises ValueError", False)
except ValueError:
    test("an SLO entry missing required fields raises ValueError", True)

print("\n\U0001f9ea Test 3: load_slo_definitions -- config_path (file)")
example_path = REPO_ROOT / "config" / "slo_definitions.example.json"
test("the checked-in example config parses without error", len(slo.load_slo_definitions(config_path=str(example_path))) > 0)

try:
    slo.load_slo_definitions(config_path="/no/such/file.json")
    test("a missing config_path raises ValueError", False)
except ValueError:
    test("a missing config_path raises ValueError", True)


# ─── SLODefinition validation ──────────────────────────────────────────
print("\n\U0001f9ea Test 4: SLODefinition -- strict field validation")
try:
    make_definition(objective_pct=150)
    test("objective_pct > 100 raises ValueError", False)
except ValueError:
    test("objective_pct > 100 raises ValueError", True)

try:
    make_definition(window_hours=0)
    test("a non-positive window_hours raises ValueError", False)
except ValueError:
    test("a non-positive window_hours raises ValueError", True)

try:
    make_definition(criticality="not_a_criticality")
    test("an unrecognized criticality raises ValueError", False)
except ValueError:
    test("an unrecognized criticality raises ValueError", True)


# ─── evaluate_slo -- healthy / at_risk / breached / insufficient_data ──
print("\n\U0001f9ea Test 5: evaluate_slo -- healthy (observed above objective, low burn)")
healthy_def = make_definition(objective_pct=99.9, at_risk_burn_rate=2.0)
healthy_summary = slo.evaluate_slo(healthy_def, query_logs_fn=rows_fn([{"good": 9999, "total": 10000}]))
test("state is healthy", healthy_summary.state == "healthy")
test("observed_pct is computed correctly", healthy_summary.observed_pct == 99.99)
test("good_count/total_count are populated", healthy_summary.good_count == 9999 and healthy_summary.total_count == 10000)

print("\n\U0001f9ea Test 6: evaluate_slo -- breached (observed below objective)")
breached_summary = slo.evaluate_slo(healthy_def, query_logs_fn=rows_fn([{"good": 9900, "total": 10000}]))
test("state is breached", breached_summary.state == "breached")
test("error_budget_remaining_pct is clamped to 0 when fully exhausted", breached_summary.error_budget_remaining_pct == 0.0)

print("\n\U0001f9ea Test 7: evaluate_slo -- at_risk (meets objective, but burn rate crosses the threshold)")
at_risk_def = make_definition(objective_pct=99.0, at_risk_burn_rate=0.5)
at_risk_summary = slo.evaluate_slo(at_risk_def, query_logs_fn=rows_fn([{"good": 9940, "total": 10000}]))
test("state is at_risk", at_risk_summary.state == "at_risk")
test("burn_rate crosses the configured at_risk threshold", at_risk_summary.burn_rate >= at_risk_def.at_risk_burn_rate)
test("observed_pct still meets the objective", at_risk_summary.observed_pct >= at_risk_def.objective_pct)

print("\n\U0001f9ea Test 8: evaluate_slo -- insufficient_data (zero total, never a fabricated 100%/0%)")
insufficient_summary = slo.evaluate_slo(healthy_def, query_logs_fn=rows_fn([{"good": 0, "total": 0}]))
test("state is insufficient_data", insufficient_summary.state == "insufficient_data")
test("observed_pct is None, not a fabricated number", insufficient_summary.observed_pct is None)
test("good_count/total_count are None, not fabricated zeros-as-facts", insufficient_summary.good_count is None and insufficient_summary.total_count is None)

print("\n\U0001f9ea Test 9: evaluate_slo -- explicit errors, never a fabricated result")
try:
    slo.evaluate_slo(healthy_def, query_logs_fn=rows_fn([]))
    test("zero rows raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("zero rows raises OperationsCollectionError", True)

try:
    slo.evaluate_slo(healthy_def, query_logs_fn=rows_fn([{"wrong_column": 1}]))
    test("missing good/total columns raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("missing good/total columns raises OperationsCollectionError", True)

try:
    slo.evaluate_slo(healthy_def, query_logs_fn=rows_fn([{"good": 100, "total": 50}]))  # good > total
    test("good > total raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("good > total raises OperationsCollectionError", True)


def failing_query(*args, **kwargs):
    raise RuntimeError("workspace unreachable")


try:
    slo.evaluate_slo(healthy_def, query_logs_fn=failing_query)
    test("a Log Analytics query failure raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("a Log Analytics query failure raises OperationsCollectionError", True)


# ─── slo_summaries_to_findings ─────────────────────────────────────────
print("\n\U0001f9ea Test 10: slo_summaries_to_findings -- Findings only for at_risk/breached")
findings = slo.slo_summaries_to_findings([healthy_summary, breached_summary, at_risk_summary, insufficient_summary])
test("exactly two Findings (breached + at_risk)", len(findings) == 2)
test("all SLO Findings use category reliability", all(f.category == FindingCategory.RELIABILITY.value for f in findings))
test("confidence is derived (deterministic math over platform query results)", all(f.confidence == ConfidenceLevel.DERIVED.value for f in findings))
test("a breached customer-facing SLO demands executive attention", any(f.executive_attention for f in findings if "breached" in f.title))

breached_finding = next(f for f in findings if "breached" in f.title)
at_risk_finding = next(f for f in findings if "at_risk" in f.title)
test("a breached customer-facing SLO IS customer_impacting -- an actual SLO breach, not merely at_risk", breached_finding.customer_impacting is True)
test("an at_risk (not yet breached) SLO is NOT customer_impacting", at_risk_finding.customer_impacting is False)

print("\n\U0001f9ea Test 10b: slo_summaries_to_findings -- a breached NON-customer-facing SLO is not customer_impacting")
internal_breached_def = make_definition(workload="internal-batch", criticality="internal", objective_pct=99.9, at_risk_burn_rate=2.0)
internal_breached_summary = slo.evaluate_slo(internal_breached_def, query_logs_fn=rows_fn([{"good": 9900, "total": 10000}]))
test("sanity: this summary is indeed breached", internal_breached_summary.state == "breached")
internal_breached_finding = slo.slo_summaries_to_findings([internal_breached_summary])[0]
test("a breached internal-criticality SLO is NOT customer_impacting (only customer_facing breaches count)", internal_breached_finding.customer_impacting is False)
test("it still demands no less severity (high) even though it isn't customer_impacting", internal_breached_finding.severity == "high")


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
