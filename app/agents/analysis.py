"""Evidence-grounded agent analysis -- the orchestrator behind
/api/operations/analyze and /api/operations/briefing (see
app/agents/analysis_routes.py).

Pipeline: OperationsSnapshot (app/operations/snapshot.py, already
deterministic/cached) -> bounded EvidenceBundle (app/agents/evidence.py)
-> selective routing (app/agents/routing.py) -> one or more specialist
model calls + an optional debate/rebuttal round + an optional
coordinator synthesis (app/agents/backend.py, structured output per
app/agents/schema.py) -> citation validation against the bundle's own
finding ids -> deterministic approval-tier metadata per recommended
action (app/approval.py) -> deterministic evaluation (app/agents/evaluation.py).

A genuinely empty evidence bundle (zero findings matched the request)
short-circuits to a deterministic "insufficient evidence" answer with
ZERO model calls -- there is nothing grounded to reason over, so this
module never fabricates a confident-sounding answer in that case.
"""

import json as _json
from typing import Optional

from app import telemetry
from app.agents import backend as backend_module
from app.agents import evaluation as evaluation_module
from app.agents import routing as routing_module
from app.agents import schema as schema_module
from app.agents.evidence import EvidenceBundle, EvidenceBundleError, build_evidence_bundle
from app.approval import analysis_action_metadata
from app.config import settings
from app.operations.config import OperationsConfig
from app.operations.models import format_utc_iso, utc_now
from app.operations.snapshot import OperationsSnapshot, get_snapshot

__all__ = [
    "AnalysisError",
    "EvidenceBundleError",
    "SpecialistOutcome",
    "analyze_operations",
    "build_briefing",
]


class AnalysisError(ValueError):
    """Bad request-level input: missing question/subscription, or an
    unrecognized requested agent key."""


_SCHEMA_INSTRUCTION = (
    "Respond with ONLY a single JSON object matching this exact schema (no prose, no markdown "
    "fences, no extra keys): " + _json.dumps(schema_module.AGENT_ANALYSIS_JSON_SCHEMA) + "\n"
    "Every 'evidence_ids' entry MUST be a finding id copied verbatim from the 'items[].id' fields "
    "in the EVIDENCE BUNDLE below -- never invent an id, and never cite an id you did not see there. "
    "If the evidence bundle doesn't support a confident conclusion, say so in 'narrative' and list "
    "what's missing in 'missing_evidence' rather than guessing."
)


def _build_messages(agent_cfg, *, question: str, bundle: EvidenceBundle, extra_instruction: str = "") -> list:
    messages = [{"role": "system", "content": agent_cfg.system_prompt}]
    messages.append({"role": "user", "content": f"EVIDENCE BUNDLE (the only ground truth you may cite):\n{bundle.to_prompt_json()}"})
    messages.append({"role": "user", "content": question})
    messages.append({"role": "user", "content": _SCHEMA_INSTRUCTION})
    if agent_cfg.response_instruction:
        messages.append({"role": "user", "content": f"RESPONSE INSTRUCTION: {agent_cfg.response_instruction}"})
    if extra_instruction:
        messages.append({"role": "user", "content": extra_instruction})
    return messages


class SpecialistOutcome:
    """One agent call's outcome -- the parsed/validated structured result
    when parsing succeeded, or an explicit schema_error when it didn't
    (never a fabricated result)."""

    def __init__(self, *, agent_key, agent_name, role, model, structured_output_used, schema_valid, result, raw_text, usage, schema_error=None):
        self.agent_key = agent_key
        self.agent_name = agent_name
        self.role = role
        self.model = model
        self.structured_output_used = structured_output_used
        self.schema_valid = schema_valid
        self.result = result
        self.raw_text = raw_text
        self.usage = usage
        self.schema_error = schema_error

    def to_dict(self) -> dict:
        return {
            "agent_key": self.agent_key,
            "agent": self.agent_name,
            "role": self.role,
            "model": self.model,
            "structured_output_used": self.structured_output_used,
            "schema_valid": self.schema_valid,
            "result": self.result.to_dict() if self.result is not None else None,
            "schema_error": self.schema_error,
            "raw_text_snippet": None if self.schema_valid else (self.raw_text or "")[:300],
            "usage": self.usage,
        }


def _call_specialist(agent_key: str, *, question: str, bundle: EvidenceBundle, backend, extra_instruction: str = "") -> SpecialistOutcome:
    agent_cfg = settings.agents[agent_key]
    messages = _build_messages(agent_cfg, question=question, bundle=bundle, extra_instruction=extra_instruction)
    completion = backend.complete(
        agent_cfg, messages, json_schema=schema_module.AGENT_ANALYSIS_JSON_SCHEMA, schema_name="agent_analysis_result"
    )
    try:
        result = schema_module.parse_structured_response(completion.raw_text)
        schema_valid, schema_error = True, None
    except schema_module.AnalysisSchemaError as exc:
        result, schema_valid, schema_error = None, False, str(exc)

    return SpecialistOutcome(
        agent_key=agent_key, agent_name=agent_cfg.name, role=agent_cfg.role, model=agent_cfg.deployment,
        structured_output_used=completion.structured_output_used, schema_valid=schema_valid, result=result,
        raw_text=completion.raw_text, usage=completion.usage, schema_error=schema_error,
    )


def _summarize_outcomes(outcomes: dict) -> str:
    parts = []
    for outcome in outcomes.values():
        if outcome.schema_valid:
            parts.append(_json.dumps({"agent": outcome.agent_name, "result": outcome.result.to_dict()}))
        else:
            parts.append(_json.dumps({"agent": outcome.agent_name, "schema_valid": False, "error": outcome.schema_error}))
    return "\n".join(parts)


def _rebuttal_instruction(round1_summary: str) -> str:
    return (
        "Here is what the rest of the crew concluded in Round 1 (JSON per-agent):\n"
        f"{round1_summary}\n"
        "Reconsider your own answer in light of theirs -- agree, disagree, or refine. Still respond "
        "with ONLY the same JSON schema, citing evidence_ids from the evidence bundle."
    )


def _synthesis_instruction(specialists: dict, rebuttals: Optional[dict]) -> str:
    text = "Here is what each specialist concluded (JSON per-agent):\n" + _summarize_outcomes(specialists)
    if rebuttals:
        text += "\n\nHere is each specialist's rebuttal after seeing the others (JSON per-agent):\n" + _summarize_outcomes(rebuttals)
    text += (
        "\n\nSynthesize ONE coordinated conclusion from the above. Resolve any disagreement explicitly "
        "in 'narrative'. Still respond with ONLY the same JSON schema, citing evidence_ids copied from "
        "the evidence bundle (not invented from the specialists' text)."
    )
    return text


def _action_payload(action, metadata: dict) -> dict:
    return {**action.to_dict(), "approval": metadata}


def _final_result_payload(outcome: SpecialistOutcome, *, valid_ids: list, unsupported_ids: list, action_metadata: list) -> dict:
    if not outcome.schema_valid:
        return {
            "agent": outcome.agent_name, "agent_key": outcome.agent_key, "schema_valid": False,
            "schema_error": outcome.schema_error, "raw_text_snippet": (outcome.raw_text or "")[:300],
        }
    result = outcome.result
    actions = [_action_payload(action, metadata) for action, metadata in zip(result.recommended_actions, action_metadata)]
    return {
        "agent": outcome.agent_name, "agent_key": outcome.agent_key, "schema_valid": True,
        "conclusion": result.conclusion, "business_impact": result.business_impact,
        "confidence": result.confidence, "narrative": result.narrative,
        "evidence_ids": result.evidence_ids, "unsupported_evidence_ids": unsupported_ids,
        "valid_evidence_ids": valid_ids, "missing_evidence": result.missing_evidence,
        "recommended_actions": actions, "structured_output_used": outcome.structured_output_used,
    }


def _model_metadata(backend_obj) -> dict:
    return {
        "backend": getattr(backend_obj, "name", "unknown"),
        "agent_definition_version": settings.agent_definition_version,
        "prompt_versions": {key: cfg.prompt_version for key, cfg in settings.agents.items()},
    }


def _insufficient_evidence_response(*, question: str, bundle: EvidenceBundle, snapshot: OperationsSnapshot, now) -> dict:
    result = schema_module.AgentAnalysisResult(
        conclusion="No matching evidence found for this request.",
        business_impact="",
        confidence="low",
        evidence_ids=(),
        missing_evidence=("matching findings in the current snapshot",),
        recommended_actions=(),
        narrative=(
            "The evidence bundle built from the current snapshot contained zero findings matching "
            "the requested filters, so no grounded conclusion can be produced. Broaden the filters "
            "or re-run after the next collection cycle."
        ),
    )
    evaluation_result = evaluation_module.evaluate(
        result=result, schema_valid=True, bundle_known_ids=set(), action_metadata=[],
        debate_used=False, agents_consulted=0,
    )
    evaluation_module.record_evaluation(evaluation_result)
    return {
        "question": question,
        "generated_at": format_utc_iso(now),
        "snapshot_id": snapshot.id,
        "routing": {
            "specialist_agents": [], "coordinator_included": False, "debate": False,
            "factors": {"reason": "no evidence matched the requested filters"},
        },
        "evidence_bundle": bundle.to_dict(),
        "specialists": {},
        "rebuttals": None,
        "final": {
            "agent": "system", "agent_key": "none", "schema_valid": True,
            "conclusion": result.conclusion, "business_impact": result.business_impact,
            "confidence": result.confidence, "narrative": result.narrative,
            "evidence_ids": [], "unsupported_evidence_ids": [], "valid_evidence_ids": [],
            "missing_evidence": list(result.missing_evidence), "recommended_actions": [],
            "structured_output_used": False,
        },
        "evaluation": evaluation_result.to_dict(),
        "model_metadata": {
            "backend": "none", "agent_definition_version": settings.agent_definition_version, "prompt_versions": {},
        },
    }


def analyze_operations(
    *,
    question: str,
    subscription_ids: list,
    category: str = None,
    severity: str = None,
    status: str = None,
    finding_id: str = None,
    requested_agents: list = None,
    force_debate: bool = False,
    force_refresh: bool = False,
    max_items: int = None,
    backend=None,
    config: Optional[OperationsConfig] = None,
    snapshot: Optional[OperationsSnapshot] = None,
    now=None,
) -> dict:
    """Build a bounded evidence bundle from the current operations
    snapshot, route it to the right specialist(s) (+ debate/coordinator
    when warranted), and return one grounded, citation-checked,
    evaluated response. Raises AnalysisError (bad request input) or
    EvidenceBundleError (bad filter / unknown finding_id) -- both plain
    ValueError subclasses the Flask route layer maps to 400/404 -- or
    lets a backend's own exception (e.g. NotImplementedError from the
    Foundry stub) propagate unchanged."""
    if not question or not question.strip():
        raise AnalysisError("question is required")
    if not subscription_ids or not any(subscription_ids):
        raise AnalysisError("no subscription configured -- pass subscription_ids or configure AZURE_SUBSCRIPTION_ID")

    known_agent_keys = set(settings.agents) - {routing_module.COORDINATOR_KEY}
    if requested_agents:
        unknown = [a for a in requested_agents if a not in known_agent_keys]
        if unknown:
            raise AnalysisError(
                f"unknown specialist agent(s) {unknown}; must be one of {sorted(known_agent_keys)} "
                f"(the coordinator, {routing_module.COORDINATOR_KEY!r}, is added automatically when needed)"
            )

    now = now or utc_now()
    snapshot = snapshot if snapshot is not None else get_snapshot(subscription_ids, config=config, force_refresh=force_refresh)
    bundle_kwargs = {"category": category, "severity": severity, "status": status, "finding_id": finding_id}
    if max_items is not None:
        bundle_kwargs["max_items"] = max_items
    bundle = build_evidence_bundle(snapshot, **bundle_kwargs)

    if not bundle.items:
        return _insufficient_evidence_response(question=question, bundle=bundle, snapshot=snapshot, now=now)

    routing_decision = routing_module.route(bundle, requested_agents=requested_agents, force_debate=force_debate)
    telemetry.record_routing_decision(
        debate=routing_decision.debate, specialist_count=len(routing_decision.specialist_agents),
        coordinator_included=routing_decision.coordinator_included,
    )

    backend_obj = backend or backend_module.get_backend()

    specialist_outcomes = {
        agent_key: _call_specialist(agent_key, question=question, bundle=bundle, backend=backend_obj)
        for agent_key in routing_decision.specialist_agents
    }

    rebuttal_outcomes = None
    if routing_decision.debate and len(specialist_outcomes) >= 2:
        round1_summary = _summarize_outcomes(specialist_outcomes)
        rebuttal_outcomes = {
            agent_key: _call_specialist(
                agent_key, question=question, bundle=bundle, backend=backend_obj,
                extra_instruction=_rebuttal_instruction(round1_summary),
            )
            for agent_key in routing_decision.specialist_agents
        }

    if routing_decision.coordinator_included:
        synthesis_extra = _synthesis_instruction(specialist_outcomes, rebuttal_outcomes)
        final_outcome = _call_specialist(
            routing_module.COORDINATOR_KEY, question=question, bundle=bundle, backend=backend_obj,
            extra_instruction=synthesis_extra,
        )
    else:
        final_outcome = specialist_outcomes[routing_decision.specialist_agents[0]]

    known_ids = bundle.known_ids()
    if final_outcome.schema_valid:
        valid_ids, unsupported_ids = schema_module.validate_evidence_ids(final_outcome.result.evidence_ids, known_ids)
        action_metadata = [analysis_action_metadata(action.description) for action in final_outcome.result.recommended_actions]
    else:
        valid_ids, unsupported_ids, action_metadata = [], [], []

    evaluation_result = evaluation_module.evaluate(
        result=final_outcome.result, schema_valid=final_outcome.schema_valid, bundle_known_ids=known_ids,
        action_metadata=action_metadata, debate_used=bool(rebuttal_outcomes), agents_consulted=len(specialist_outcomes),
    )
    evaluation_module.record_evaluation(evaluation_result)

    return {
        "question": question,
        "generated_at": format_utc_iso(now),
        "snapshot_id": snapshot.id,
        "routing": routing_decision.to_dict(),
        "evidence_bundle": bundle.to_dict(),
        "specialists": {key: outcome.to_dict() for key, outcome in specialist_outcomes.items()},
        "rebuttals": {key: outcome.to_dict() for key, outcome in rebuttal_outcomes.items()} if rebuttal_outcomes else None,
        "final": _final_result_payload(final_outcome, valid_ids=valid_ids, unsupported_ids=unsupported_ids, action_metadata=action_metadata),
        "evaluation": evaluation_result.to_dict(),
        "model_metadata": _model_metadata(backend_obj),
    }


_BRIEFING_QUESTION = (
    "Produce an executive operations briefing: what matters right now, the business impact, and the "
    "recommended next actions, grounded strictly in the evidence bundle provided."
)


def build_briefing(
    *,
    subscription_ids: list,
    category: str = None,
    severity: str = None,
    status: str = None,
    force_debate: bool = False,
    force_refresh: bool = False,
    backend=None,
    config: Optional[OperationsConfig] = None,
    snapshot: Optional[OperationsSnapshot] = None,
    now=None,
) -> dict:
    """A thinner reshaping of analyze_operations() emphasizing ONE
    coordinator voice (see docs/AGENT_INTELLIGENCE.md's routing policy:
    "the executive brief exposes one coordinator voice; specialist
    details are supporting analysis, not persona theater") -- specialist
    detail is collapsed to a short {agent, role, confidence, conclusion}
    bullet, never the full narrative/raw text."""
    full = analyze_operations(
        question=_BRIEFING_QUESTION, subscription_ids=subscription_ids, category=category, severity=severity,
        status=status, requested_agents=None, force_debate=force_debate, force_refresh=force_refresh,
        backend=backend, config=config, snapshot=snapshot, now=now,
    )
    supporting_analysis = []
    for agent_key, outcome in full["specialists"].items():
        supporting_analysis.append({
            "agent_key": agent_key,
            "agent": outcome["agent"],
            "role": outcome["role"],
            "schema_valid": outcome["schema_valid"],
            "confidence": outcome["result"]["confidence"] if outcome["schema_valid"] else None,
            "conclusion": outcome["result"]["conclusion"] if outcome["schema_valid"] else None,
        })
    return {
        "generated_at": full["generated_at"],
        "snapshot_id": full["snapshot_id"],
        "routing": full["routing"],
        "coordinator": full["final"],
        "supporting_analysis": supporting_analysis,
        "evaluation": full["evaluation"],
        "model_metadata": full["model_metadata"],
    }
