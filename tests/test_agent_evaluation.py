#!/usr/bin/env python3
"""Test lightweight deterministic evaluation (app/agents/evaluation.py)
-- citation validity/coverage, schema validity, unsupported-citation
counting, action-policy adherence, and the in-process aggregate
counters. No prompt/response content is ever computed/stored here.

Run: python3 tests/test_agent_evaluation.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.agents import evaluation  # noqa: E402
from app.agents.schema import AgentAnalysisResult, RecommendedAction  # noqa: E402

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


evaluation.reset_for_tests()


print("\n\U0001f9ea Test 1: schema_valid=False short-circuits to a zeroed-out, but still valid, EvaluationResult")
result1 = evaluation.evaluate(
    result=None, schema_valid=False, bundle_known_ids={"a", "b"}, action_metadata=[], debate_used=False, agents_consulted=1,
)
test("schema_valid is False", result1.schema_valid is False)
test("citation_count is 0", result1.citation_count == 0)
test("citation_validity_pct is None (nothing to compute a rate over)", result1.citation_validity_pct is None)
test("action_policy_adherent defaults True when there are no actions to violate", result1.action_policy_adherent is True)


print("\n\U0001f9ea Test 2: citation validity/coverage computed against the bundle's own known ids")
valid_result = AgentAnalysisResult(
    conclusion="c", business_impact="b", confidence="high",
    evidence_ids=("a", "b", "made-up"), missing_evidence=(), recommended_actions=(), narrative="n",
)
result2 = evaluation.evaluate(
    result=valid_result, schema_valid=True, bundle_known_ids={"a", "b", "c"}, action_metadata=[],
    debate_used=False, agents_consulted=1,
)
test("citation_count counts every cited id, valid or not", result2.citation_count == 3)
test("valid_citation_count counts only ids present in the bundle", result2.valid_citation_count == 2)
test("unsupported_citation_count counts the rest", result2.unsupported_citation_count == 1)
test("citation_validity_pct == 2/3 rounded", abs(result2.citation_validity_pct - 66.7) < 0.1)
test("citation_coverage_pct == 2/3 of the bundle's 3 known ids", abs(result2.citation_coverage_pct - 66.7) < 0.1)


print("\n\U0001f9ea Test 3: action_policy_adherent reflects every action's auto_executable flag")
action = RecommendedAction(description="d", owner="o", urgency="monitor", approval_required=False)
result_with_action = AgentAnalysisResult(
    conclusion="c", business_impact="b", confidence="low", evidence_ids=(), missing_evidence=(),
    recommended_actions=(action,), narrative="n",
)
result3_ok = evaluation.evaluate(
    result=result_with_action, schema_valid=True, bundle_known_ids=set(),
    action_metadata=[{"auto_executable": False}], debate_used=False, agents_consulted=1,
)
test("all actions non-executable -> adherent", result3_ok.action_policy_adherent is True)

result3_violation = evaluation.evaluate(
    result=result_with_action, schema_valid=True, bundle_known_ids=set(),
    action_metadata=[{"auto_executable": True}], debate_used=False, agents_consulted=1,
)
test("an auto_executable=True action -> flagged as NOT adherent", result3_violation.action_policy_adherent is False)


print("\n\U0001f9ea Test 4: to_dict() exposes only counts/booleans/percentages -- no content fields")
d = result2.to_dict()
test("to_dict has no 'narrative'/'conclusion'/free-text keys", not ({"narrative", "conclusion", "business_impact"} & set(d)))


print("\n\U0001f9ea Test 5: aggregate counters accumulate across record_evaluation() calls")
evaluation.reset_for_tests()
before = evaluation.get_aggregate_summary()
test("counters start at zero after reset", before["total_analyses"] == 0)

evaluation.record_evaluation(result2)
evaluation.record_evaluation(result1)
after = evaluation.get_aggregate_summary()
test("total_analyses incremented for every record_evaluation() call", after["total_analyses"] == 2)
test("schema_valid_count reflects only the schema-valid one", after["schema_valid_count"] == 1)
test("schema_invalid_count reflects only the schema-invalid one", after["schema_invalid_count"] == 1)
test("total_unsupported_citations accumulates", after["total_unsupported_citations"] == 1)

evaluation.reset_for_tests()


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
