#!/usr/bin/env python3
"""Test the structured-output schema/parser (app/agents/schema.py) --
valid parsing, the fenced/prose fallback extraction, strict rejection of
malformed/extra-key output, and citation validation.

Run: python3 tests/test_agent_schema.py
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.agents import schema  # noqa: E402

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


VALID_PAYLOAD = {
    "conclusion": "Cost is spiking in eastus2.",
    "business_impact": "Budget will be exceeded this month.",
    "confidence": "high",
    "evidence_ids": ["cst-abc123"],
    "missing_evidence": [],
    "recommended_actions": [
        {"description": "Right-size the VM SKU", "owner": "platform-team", "urgency": "scheduled", "approval_required": True},
    ],
    "narrative": "Budget usage crossed 100% per cst-abc123.",
}


print("\n\U0001f9ea Test 1: parse_structured_response -- exact JSON, no wrapping")
result = schema.parse_structured_response(json.dumps(VALID_PAYLOAD))
test("conclusion round-trips", result.conclusion == VALID_PAYLOAD["conclusion"])
test("confidence round-trips", result.confidence == "high")
test("evidence_ids round-trips as a tuple", result.evidence_ids == ("cst-abc123",))
test("recommended_actions[0] is a RecommendedAction", isinstance(result.recommended_actions[0], schema.RecommendedAction))
test("recommended_actions[0].urgency round-trips", result.recommended_actions[0].urgency == "scheduled")
test("to_dict round-trips back to the same shape", result.to_dict()["evidence_ids"] == ["cst-abc123"])


print("\n\U0001f9ea Test 2: parse_structured_response -- fallback extraction from prose/fences")
fenced = "Sure, here you go:\n```json\n" + json.dumps(VALID_PAYLOAD) + "\n```\nLet me know if you need more."
result2 = schema.parse_structured_response(fenced)
test("fenced JSON block is extracted and parses", result2.conclusion == VALID_PAYLOAD["conclusion"])

prose_wrapped = "Here is my answer: " + json.dumps(VALID_PAYLOAD) + " -- hope that helps!"
result3 = schema.parse_structured_response(prose_wrapped)
test("balanced-brace extraction works when JSON is wrapped in prose", result3.conclusion == VALID_PAYLOAD["conclusion"])


print("\n\U0001f9ea Test 3: parse_structured_response -- malformed output is REJECTED, never coerced")
try:
    schema.parse_structured_response("I don't have enough information to answer that.")
    test("plain prose with no JSON raises AnalysisSchemaError", False)
except schema.AnalysisSchemaError:
    test("plain prose with no JSON raises AnalysisSchemaError", True)

missing_field = dict(VALID_PAYLOAD)
del missing_field["confidence"]
try:
    schema.parse_structured_response(json.dumps(missing_field))
    test("missing required field raises AnalysisSchemaError", False)
except schema.AnalysisSchemaError as exc:
    test("missing required field raises AnalysisSchemaError", "confidence" in str(exc))

extra_field = dict(VALID_PAYLOAD)
extra_field["extra_made_up_field"] = "nope"
try:
    schema.parse_structured_response(json.dumps(extra_field))
    test("unexpected extra field raises AnalysisSchemaError (strict, no silent drop)", False)
except schema.AnalysisSchemaError as exc:
    test("unexpected extra field raises AnalysisSchemaError (strict, no silent drop)", "extra_made_up_field" in str(exc))

bad_confidence = dict(VALID_PAYLOAD)
bad_confidence["confidence"] = "super-duper-sure"
try:
    schema.parse_structured_response(json.dumps(bad_confidence))
    test("invalid confidence enum value raises AnalysisSchemaError", False)
except schema.AnalysisSchemaError:
    test("invalid confidence enum value raises AnalysisSchemaError", True)

bad_urgency = json.loads(json.dumps(VALID_PAYLOAD))
bad_urgency["recommended_actions"][0]["urgency"] = "yesterday"
try:
    schema.parse_structured_response(json.dumps(bad_urgency))
    test("invalid urgency enum value raises AnalysisSchemaError", False)
except schema.AnalysisSchemaError:
    test("invalid urgency enum value raises AnalysisSchemaError", True)

not_a_bool = json.loads(json.dumps(VALID_PAYLOAD))
not_a_bool["recommended_actions"][0]["approval_required"] = "yes"
try:
    schema.parse_structured_response(json.dumps(not_a_bool))
    test("non-boolean approval_required raises AnalysisSchemaError", False)
except schema.AnalysisSchemaError:
    test("non-boolean approval_required raises AnalysisSchemaError", True)

empty_conclusion = dict(VALID_PAYLOAD)
empty_conclusion["conclusion"] = "   "
try:
    schema.parse_structured_response(json.dumps(empty_conclusion))
    test("blank conclusion raises AnalysisSchemaError", False)
except schema.AnalysisSchemaError:
    test("blank conclusion raises AnalysisSchemaError", True)


print("\n\U0001f9ea Test 4: AGENT_ANALYSIS_JSON_SCHEMA is strict-mode compliant")
schema_dict = schema.AGENT_ANALYSIS_JSON_SCHEMA
test("additionalProperties is False at the top level", schema_dict["additionalProperties"] is False)
test("every declared property is in required (strict mode requirement)",
     set(schema_dict["properties"]) == set(schema_dict["required"]))
action_schema = schema_dict["properties"]["recommended_actions"]["items"]
test("recommended_actions item schema is also strict",
     action_schema["additionalProperties"] is False and set(action_schema["properties"]) == set(action_schema["required"]))


print("\n\U0001f9ea Test 5: validate_evidence_ids -- unsupported citations are never silently dropped")
valid, unsupported = schema.validate_evidence_ids(["cst-abc123", "made-up-id"], {"cst-abc123"})
test("valid citation recognized", valid == ["cst-abc123"])
test("unsupported citation surfaced explicitly, not dropped", unsupported == ["made-up-id"])

valid2, unsupported2 = schema.validate_evidence_ids([], {"cst-abc123"})
test("no citations -> empty valid/unsupported lists", valid2 == [] and unsupported2 == [])


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
