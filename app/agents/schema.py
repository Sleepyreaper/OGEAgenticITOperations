"""Structured output contract for grounded agent analysis
(app/agents/analysis.py).

Every agent answer this app treats as "the" grounded conclusion (as
opposed to free-text debate chatter) must conform to
``AGENT_ANALYSIS_JSON_SCHEMA`` below -- a small, closed schema with NO
optional fields (every property is in ``required``, matching Azure
OpenAI/OpenAI "strict" json_schema mode, which requires exactly that).

``parse_structured_response`` is the single entry point that turns a
model's raw text into a validated ``AgentAnalysisResult`` -- it is used
BOTH when the backend successfully requested structured output (as a
final sanity check; a provider is not obligated to guarantee strict-mode
conformance) AND as the fallback parser when the backend had to retry
without ``response_format`` (e.g. a deployment that doesn't support
it). A response that fails validation raises ``AnalysisSchemaError`` --
it is NEVER coerced into a fabricated "successful" result.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "CONFIDENCE_LEVELS",
    "URGENCY_LEVELS",
    "AGENT_ANALYSIS_JSON_SCHEMA",
    "AnalysisSchemaError",
    "RecommendedAction",
    "AgentAnalysisResult",
    "parse_structured_response",
    "validate_evidence_ids",
]

CONFIDENCE_LEVELS = ("high", "medium", "low")
# immediate = act now; scheduled = plan the work; monitor = watch only,
# no action required yet. A small, closed, explainable vocabulary --
# never a free-text urgency string.
URGENCY_LEVELS = ("immediate", "scheduled", "monitor")

_ACTION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "One concrete, specific next step."},
        "owner": {"type": "string", "description": "Team/role that should own this action, or empty string if unknown."},
        "urgency": {"type": "string", "enum": list(URGENCY_LEVELS)},
        "approval_required": {"type": "boolean", "description": "The model's own opinion; the server independently classifies an approval tier for every action regardless of this value."},
    },
    "required": ["description", "owner", "urgency", "approval_required"],
    "additionalProperties": False,
}

AGENT_ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string", "description": "One-sentence headline conclusion."},
        "business_impact": {"type": "string", "description": "Concrete business impact in plain language, or empty string if none identified."},
        "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS)},
        "evidence_ids": {
            "type": "array", "items": {"type": "string"},
            "description": "Finding IDs copied verbatim from the evidence bundle that support this conclusion.",
        },
        "missing_evidence": {
            "type": "array", "items": {"type": "string"},
            "description": "Specific evidence that would strengthen this conclusion but wasn't available in the bundle.",
        },
        "recommended_actions": {"type": "array", "items": _ACTION_ITEM_SCHEMA},
        "narrative": {"type": "string", "description": "2-5 sentence grounded explanation citing the evidence_ids."},
    },
    "required": [
        "conclusion", "business_impact", "confidence", "evidence_ids",
        "missing_evidence", "recommended_actions", "narrative",
    ],
    "additionalProperties": False,
}


class AnalysisSchemaError(ValueError):
    """Model output could not be parsed/validated against
    AGENT_ANALYSIS_JSON_SCHEMA. Callers must treat this as an explicit
    failure -- never a successful grounded answer with guessed fields."""


@dataclass(frozen=True)
class RecommendedAction:
    description: str
    owner: str
    urgency: str
    approval_required: bool

    def __post_init__(self):
        if not isinstance(self.description, str) or not self.description.strip():
            raise AnalysisSchemaError("recommended_actions[].description must be a non-empty string")
        if not isinstance(self.owner, str):
            raise AnalysisSchemaError("recommended_actions[].owner must be a string")
        if self.urgency not in URGENCY_LEVELS:
            raise AnalysisSchemaError(f"recommended_actions[].urgency {self.urgency!r} must be one of {URGENCY_LEVELS}")
        if not isinstance(self.approval_required, bool):
            raise AnalysisSchemaError("recommended_actions[].approval_required must be a boolean")

    def to_dict(self) -> dict:
        return {
            "description": self.description, "owner": self.owner,
            "urgency": self.urgency, "approval_required": self.approval_required,
        }


@dataclass(frozen=True)
class AgentAnalysisResult:
    conclusion: str
    business_impact: str
    confidence: str
    evidence_ids: tuple = field(default_factory=tuple)
    missing_evidence: tuple = field(default_factory=tuple)
    recommended_actions: tuple = field(default_factory=tuple)
    narrative: str = ""

    def __post_init__(self):
        if not isinstance(self.conclusion, str) or not self.conclusion.strip():
            raise AnalysisSchemaError("conclusion must be a non-empty string")
        if not isinstance(self.business_impact, str):
            raise AnalysisSchemaError("business_impact must be a string")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise AnalysisSchemaError(f"confidence {self.confidence!r} must be one of {CONFIDENCE_LEVELS}")
        for i, item in enumerate(self.evidence_ids):
            if not isinstance(item, str):
                raise AnalysisSchemaError(f"evidence_ids[{i}] must be a string")
        for i, item in enumerate(self.missing_evidence):
            if not isinstance(item, str):
                raise AnalysisSchemaError(f"missing_evidence[{i}] must be a string")
        for i, item in enumerate(self.recommended_actions):
            if not isinstance(item, RecommendedAction):
                raise AnalysisSchemaError(f"recommended_actions[{i}] must be a RecommendedAction instance")
        if not isinstance(self.narrative, str):
            raise AnalysisSchemaError("narrative must be a string")

    def to_dict(self) -> dict:
        return {
            "conclusion": self.conclusion,
            "business_impact": self.business_impact,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "missing_evidence": list(self.missing_evidence),
            "recommended_actions": [a.to_dict() for a in self.recommended_actions],
            "narrative": self.narrative,
        }


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _extract_json_object(raw_text: str) -> dict:
    """Best-effort extraction of exactly one JSON object from `raw_text`,
    trying (in order): the whole text as-is, a ```json fenced block, and
    the first balanced {...} span found by bracket-depth counting
    (respecting quoted strings). Raises AnalysisSchemaError -- never
    returns a best-guess partial document -- if none of those parse as a
    JSON object."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AnalysisSchemaError("model returned an empty response")

    candidates = [raw_text.strip()]
    fence_match = _FENCE_RE.search(raw_text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    balanced = _first_balanced_object(raw_text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise AnalysisSchemaError("no valid JSON object found in model output")


def _first_balanced_object(text: str) -> Optional[str]:
    """Return the first top-level {...} substring of `text` with balanced
    braces, respecting (but not interpreting escapes deeply inside)
    double-quoted strings -- good enough to strip leading/trailing prose
    a model added around an otherwise-valid JSON object."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


_TOP_LEVEL_REQUIRED = tuple(AGENT_ANALYSIS_JSON_SCHEMA["required"])
_ACTION_REQUIRED = tuple(_ACTION_ITEM_SCHEMA["required"])


def _require_exact_keys(obj: dict, required: tuple, *, where: str) -> None:
    if not isinstance(obj, dict):
        raise AnalysisSchemaError(f"{where} must be a JSON object, got {type(obj).__name__}")
    missing = [key for key in required if key not in obj]
    if missing:
        raise AnalysisSchemaError(f"{where} is missing required field(s): {missing}")
    unexpected = sorted(set(obj) - set(required))
    if unexpected:
        raise AnalysisSchemaError(f"{where} has unexpected field(s): {unexpected}")


def _build_action(obj: dict, *, index: int) -> RecommendedAction:
    _require_exact_keys(obj, _ACTION_REQUIRED, where=f"recommended_actions[{index}]")
    return RecommendedAction(
        description=obj["description"], owner=obj["owner"],
        urgency=obj["urgency"], approval_required=obj["approval_required"],
    )


def parse_structured_response(raw_text: str) -> AgentAnalysisResult:
    """Parse and strictly validate a model's raw text response against
    AGENT_ANALYSIS_JSON_SCHEMA. Raises AnalysisSchemaError (with a
    precise, actionable message) on any extraction/type/enum/required-
    field failure -- this function never returns a partially-valid or
    best-guess result."""
    obj = _extract_json_object(raw_text)
    _require_exact_keys(obj, _TOP_LEVEL_REQUIRED, where="response")

    if not isinstance(obj["evidence_ids"], list) or not all(isinstance(i, str) for i in obj["evidence_ids"]):
        raise AnalysisSchemaError("evidence_ids must be a JSON array of strings")
    if not isinstance(obj["missing_evidence"], list) or not all(isinstance(i, str) for i in obj["missing_evidence"]):
        raise AnalysisSchemaError("missing_evidence must be a JSON array of strings")
    if not isinstance(obj["recommended_actions"], list):
        raise AnalysisSchemaError("recommended_actions must be a JSON array")

    actions = tuple(_build_action(item, index=i) for i, item in enumerate(obj["recommended_actions"]))

    try:
        return AgentAnalysisResult(
            conclusion=obj["conclusion"],
            business_impact=obj["business_impact"],
            confidence=obj["confidence"],
            evidence_ids=tuple(obj["evidence_ids"]),
            missing_evidence=tuple(obj["missing_evidence"]),
            recommended_actions=actions,
            narrative=obj["narrative"],
        )
    except AnalysisSchemaError:
        raise
    except (TypeError, ValueError) as exc:
        raise AnalysisSchemaError(f"response failed schema validation: {exc}") from exc


def validate_evidence_ids(evidence_ids, known_ids) -> tuple:
    """Split `evidence_ids` into (valid_ids, unsupported_ids) against the
    evidence bundle's own known finding ids. Never silently drops an
    unsupported citation -- callers must surface `unsupported_ids`
    explicitly (see app/agents/analysis.py)."""
    known = set(known_ids)
    valid = [eid for eid in evidence_ids if eid in known]
    unsupported = [eid for eid in evidence_ids if eid not in known]
    return valid, unsupported
