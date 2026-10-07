"""Tier-2 escalation: run the OGE squad for an SRE Agent hand-off.

``escalate`` records the hand-off in the ledger, runs the grounded
analysis pipeline in a background thread and waits up to ``wait_seconds``
for it. Long debates keep running after the caller returns; the SRE Agent
(or UI) polls ``get_escalation``. Nothing here changes Azure: the output is
a proposal that a human approves.
"""
import os
import re
import threading
import traceback
from datetime import datetime, timezone

from app import ado_integration
from app.activity import store as activity_store_module
from app.agents import analysis as analysis_service
from app.operations.models import FindingCategory, Severity
from app.zeroops import cost as cost_model
from app.zeroops import scenarios as scenario_mod
from app.zeroops.ledger import get_ledger

MAX_WAIT_SECONDS = 55
DEFAULT_WAIT_SECONDS = 45

_EVENT_KIND_FALLBACKS = {
    "opened": "investigation_opened",
    "escalation_received": "review_requested",
    "proposal_created": "review_requested",
}
_SOURCE_ORIGINS = {
    "sre-agent": "mcp",
    "detector": "detector",
    "simulated": "simulated",
    "api": "api",
}
_SAFE_ERROR_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_SCENARIO_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NON_PROPOSAL_RUN_STATUSES = {"insufficient_evidence", "invalid_output", "failed"}


class RetryableActivityError(RuntimeError):
    """The ledger state is durable, but its corresponding Activity receipt is not."""

    retryable = True
    code = "activity_receipt_unavailable"


def _subscription_ids() -> list:
    raw = os.environ.get("ZEROOPS_SUBSCRIPTION_ID", "").strip() or os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _activity_event_kind(preferred: str) -> str:
    if preferred in activity_store_module.EVENT_KINDS:
        return preferred
    fallback = _EVENT_KIND_FALLBACKS[preferred]
    if fallback in activity_store_module.EVENT_KINDS:
        return fallback
    raise RuntimeError(f"activity store cannot persist event kind {preferred!r}")


def _activity_origin(source: str) -> str:
    return _SOURCE_ORIGINS.get(source, "api")


def _activity_provenance(source: str) -> str:
    return "simulated" if source == "simulated" else "reported"


def _activity_actor(source: str) -> str:
    if source == "detector":
        return "detector"
    if source == "simulated":
        return "system"
    return "operator"


def _activity_scenario_id(value: str):
    return value if isinstance(value, str) and _SAFE_SCENARIO_TOKEN.fullmatch(value) else None


def _ensure_activity(record: dict, *, received: bool = True) -> dict:
    """Attach a bounded Activity investigation to a ledger row.

    The receipt records only request metadata represented by the closed
    Activity vocabulary. Ledger question, summary, incident reference, model
    output, and remediation script are intentionally not copied.
    """
    created = not record.get("investigation_id")
    if not created:
        investigation_id = record["investigation_id"]
    else:
        store = activity_store_module.get_activity_store()
        opened = store.open_investigation(
            origin=_activity_origin(record.get("source") or "api"),
            scenario_id=_activity_scenario_id(record.get("scenario_id")),
            escalation_id=record["id"],
        )
        investigation_id = opened["id"]
        store.append_event(
            investigation_id,
            actor="system",
            kind=_activity_event_kind("opened"),
            provenance=_activity_provenance(record.get("source") or "api"),
            payload={"source": record.get("source") or "api"},
        )
        record = get_ledger().update(record["id"], investigation_id=investigation_id)

    if received and created:
        activity_store_module.get_activity_store().append_event(
            investigation_id,
            actor=_activity_actor(record.get("source") or "api"),
            kind=_activity_event_kind("escalation_received"),
            provenance=_activity_provenance(record.get("source") or "api"),
            payload={
                "status": "received",
                "source": record.get("source") or "api",
                "category": (record.get("result") or {}).get("category"),
            },
        )
    return record


def _ensure_activity_for_mutation(record: dict, *, operation: str) -> dict:
    try:
        return _ensure_activity(record)
    except RuntimeError as exc:
        raise RetryableActivityError(
            f"{operation} could not complete its Activity setup; no proposal or decision was recorded. "
            f"Retry the same request. Activity error: {exc}"
        ) from exc


def _proposal_block_reason(result: dict, latest_run: dict = None) -> str:
    if result.get("schema_valid") is not True or result.get("synthesis_error"):
        return "invalid_analysis_result"
    run_status = (latest_run or {}).get("status")
    if run_status in _NON_PROPOSAL_RUN_STATUSES:
        return run_status
    evidence_ids = result.get("valid_evidence_ids")
    if not isinstance(evidence_ids, list) or not any(
        isinstance(evidence_id, str) and evidence_id.strip() for evidence_id in evidence_ids
    ):
        return "no_validated_evidence"
    return ""


def _safe_error_code(exc: Exception) -> str:
    token = type(exc).__name__.lower()
    return token if _SAFE_ERROR_TOKEN.fullmatch(token) else "analysis_failed"


def _incident_cost(*, source: str, sre_profile: str, squad_usage: dict = None) -> dict:
    cost = cost_model.incident_cost(sre_profile=sre_profile, squad_usage=squad_usage)
    cost["sre_agent"]["status"] = "hypothetical"
    cost["sre_agent"]["activity_observed"] = False
    cost["sre_agent"]["source_applicable"] = source == "sre-agent"
    cost["squad"]["status"] = "measured"
    return cost


def _compose_question(question: str, report_summary: str, incident_ref: str, source: str) -> str:
    parts = [question.strip()]
    if incident_ref:
        parts.append(f"Incident reference: {incident_ref}.")
    if report_summary:
        label = {
            "sre-agent": "Authenticated MCP key-holder report",
            "detector": "Detector hand-off report",
            "simulated": "Simulated report (not provider history). Azure SRE Agent triage so far",
            "api": "API caller report",
        }.get(source, "Caller report")
        parts.append(f"{label}: {report_summary.strip()[:2000]}")
    parts.append(
        "Reason about root cause and business impact, and return "
        "recommended actions a human can approve. Do not assume any action has already been taken."
    )
    return "\n".join(parts)


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _fallback_final(analysis: dict) -> dict:
    """When the coordinator's synthesis fails schema validation, fall back to the most confident
    schema-valid specialist answer (post-debate rebuttals preferred) so the SRE Agent still gets a
    grounded conclusion. Citations are re-checked against the evidence bundle."""
    known = {item.get("id") for item in ((analysis.get("evidence_bundle") or {}).get("items") or [])}
    candidates = []
    for round_rank, round_name in ((2, "rebuttals"), (1, "specialists")):
        for outcome in (analysis.get(round_name) or {}).values():
            if outcome and outcome.get("schema_valid") and outcome.get("result"):
                rank = _CONFIDENCE_RANK.get(outcome["result"].get("confidence"), 0)
                candidates.append((rank, round_rank, outcome))
    if not candidates:
        return {}
    _, _, best = max(candidates, key=lambda c: (c[0], c[1]))
    result = best["result"]
    return {
        "agent": best.get("agent"), "agent_key": best.get("agent_key"), "schema_valid": True,
        "conclusion": result.get("conclusion"), "business_impact": result.get("business_impact"),
        "confidence": result.get("confidence"), "narrative": result.get("narrative"),
        "recommended_actions": result.get("recommended_actions") or [],
        "valid_evidence_ids": [i for i in result.get("evidence_ids") or [] if i in known],
        "missing_evidence": result.get("missing_evidence") or [],
    }


def _compact_result(analysis: dict) -> dict:
    final = analysis.get("final") or {}
    synthesis_error = None
    synthesis_valid = final.get("schema_valid")
    if final and final.get("schema_valid") is False:
        synthesis_error = final.get("schema_error") or "coordinator synthesis failed schema validation"
        fallback = _fallback_final(analysis)
        if fallback:
            final = {**fallback, "fallback_from": final.get("agent_key")}
    return {
        "agent": final.get("agent"), "agent_key": final.get("agent_key"),
        "schema_valid": False if synthesis_valid is False else final.get("schema_valid"),
        "conclusion": final.get("conclusion"),
        "business_impact": final.get("business_impact"), "confidence": final.get("confidence"),
        "narrative": final.get("narrative"), "recommended_actions": final.get("recommended_actions") or [],
        "valid_evidence_ids": final.get("valid_evidence_ids") or [], "missing_evidence": final.get("missing_evidence") or [],
        "fallback_from": final.get("fallback_from"), "synthesis_error": synthesis_error,
        "specialists": sorted((analysis.get("specialists") or {}).keys()),
        "debate_used": bool(analysis.get("rebuttals")),
        "evaluation": analysis.get("evaluation"),
        "usage_summary": analysis.get("usage_summary"),
        "snapshot_id": analysis.get("snapshot_id"),
    }


_NO_EVIDENCE_REASON = "no evidence matched the requested filters"


def _filter_ladder(severity, category) -> list:
    """Callers (notably the Azure SRE Agent) often attach their own severity/category guess, which
    can be stricter than what the snapshot holds. Try the exact filters first, then progressively
    relax them so an escalation reaches the squad instead of dying on "no matching evidence"."""
    ladder = [(severity, category), (None, category), (None, None)]
    seen, out = set(), []
    for step in ladder:
        if step not in seen:
            seen.add(step)
            out.append(step)
    return out


def _analyze(*, question: str, scenario, severity, category, debate: bool, investigation_id: str, origin: str):
    requested = list(scenario.expected_agents) if scenario and scenario.expected_agents else None
    analysis, relaxed = None, []
    for index, (sev, cat) in enumerate(_filter_ladder(severity or None, category or None)):
        analysis = analysis_service.analyze_operations(
            question=question, subscription_ids=_subscription_ids(), severity=sev, category=cat,
            requested_agents=requested, force_debate=debate, force_refresh=index == 0,
            investigation_id=investigation_id, origin=origin, trigger="escalation",
        )
        if ((analysis.get("routing") or {}).get("factors") or {}).get("reason") != _NO_EVIDENCE_REASON:
            break
        relaxed.append({"severity": sev, "category": cat})
    return analysis, relaxed


def _run(esc_id: str, *, question: str, scenario, severity, category, debate: bool) -> None:
    ledger = get_ledger()
    try:
        record = ledger.update(esc_id, status="analyzing")
        analysis, relaxed = _analyze(
            question=question, scenario=scenario, severity=severity,
            category=category or (scenario.category if scenario else None), debate=debate,
            investigation_id=record["investigation_id"],
            origin=_activity_origin(record["source"]),
        )
        result = _compact_result(analysis)
        if relaxed:
            result["relaxed_filters"] = relaxed
        if scenario:
            result["remediation_script"] = scenario.to_dict()["remediation_script"]
        cost = _incident_cost(
            source=record["source"],
            sre_profile=scenario.sre_task_profile if scenario else "incident_investigation",
            squad_usage=analysis.get("usage_summary"),
        )
        latest_run = activity_store_module.get_activity_store().get_latest_run(record["investigation_id"])
        block_reason = _proposal_block_reason(result, latest_run)
        status = "proposed" if not block_reason else "failed"
        ledger.update(
            esc_id,
            status=status,
            routing=analysis.get("routing") or {},
            result=result,
            cost=cost,
            error=block_reason or None,
        )
    except Exception as exc:  # noqa: BLE001 -- background boundary: record the failure explicitly in the ledger
        traceback.print_exc()
        record = ledger.get(esc_id) or {}
        ledger.update(
            esc_id,
            status="failed",
            error=str(exc)[:1000] or _safe_error_code(exc),
            cost=_incident_cost(
                source=record.get("source") or "api",
                sre_profile=scenario.sre_task_profile if scenario else "incident_investigation",
            ),
        )


def escalate(*, question: str = "", source: str = "sre-agent", incident_ref: str = "", sre_summary: str = "",
             scenario_id: str = "", severity: str = "", category: str = "", debate: bool = False,
             wait_seconds: int = DEFAULT_WAIT_SECONDS) -> dict:
    scenario = None
    if scenario_id:
        scenario = scenario_mod.get_scenario(scenario_id)
        question = question or scenario.escalation_question
    if not question or not question.strip():
        raise ValueError("question is required (or pass a known scenario_id)")
    if severity and severity not in {m.value for m in Severity}:
        raise ValueError(f"severity {severity!r} must be one of {[m.value for m in Severity]}")
    if category and category not in {m.value for m in FindingCategory}:
        raise ValueError(f"category {category!r} must be one of {sorted(m.value for m in FindingCategory)}")
    if not _subscription_ids():
        raise ValueError("no subscription configured (ZEROOPS_SUBSCRIPTION_ID or AZURE_SUBSCRIPTION_ID)")

    ledger = get_ledger()
    record = ledger.create(source=source, question=question, incident_ref=incident_ref,
                           scenario_id=scenario.id if scenario else "", sre_summary=sre_summary)
    record = _ensure_activity(record)
    worker = threading.Thread(
        target=_run, name=f"zeroops-{record['id']}", daemon=True,
        kwargs={"esc_id": record["id"], "question": _compose_question(question, sre_summary, incident_ref, source),
                "scenario": scenario, "severity": severity, "category": category, "debate": bool(debate)},
    )
    worker.start()
    worker.join(timeout=max(0, min(int(wait_seconds or 0), MAX_WAIT_SECONDS)))
    return ledger.get(record["id"])


def get_escalation(esc_id: str):
    return get_ledger().get(esc_id)


def propose_fix(esc_id: str, *, requested_by: str = "azure-sre-agent") -> dict:
    """Turn a finished escalation into a human-approval proposal (ADO-backed when configured)."""
    ledger = get_ledger()
    record = ledger.get(esc_id)
    if record is None:
        raise KeyError(esc_id)
    if record["status"] not in ("proposed",):
        raise ValueError(f"escalation {esc_id} is {record['status']!r}; a fix can only be proposed once analysis finished")
    result = record.get("result") or {}
    block_reason = _proposal_block_reason(result)
    if block_reason:
        if block_reason == "invalid_analysis_result":
            message = "has no schema-valid synthesis"
        else:
            message = f"is not proposal-eligible: {block_reason}"
        raise ValueError(f"escalation {esc_id} {message}; a fix cannot be proposed")
    record = _ensure_activity_for_mutation(record, operation="Proposal")
    try:
        latest_run = activity_store_module.get_activity_store().get_latest_run(record["investigation_id"])
    except RuntimeError as exc:
        detail = (
            "the saved proposal remains in the ZeroOps ledger"
            if record.get("proposal_id")
            else "no proposal was created"
        )
        raise RetryableActivityError(
            f"The latest Activity run could not be checked; {detail}. Retry the same request. Activity error: {exc}"
        ) from exc
    block_reason = _proposal_block_reason(result, latest_run)
    if block_reason:
        raise ValueError(f"escalation {esc_id} has no proposal-eligible result: {block_reason}")
    if record.get("proposal_id"):
        _record_proposal_activity(record)
        return {"escalation": record, "proposal": _proposal_dict(record["proposal_id"])}
    actions = result.get("recommended_actions") or []
    lines = [
        f"Escalated by: {requested_by} ({record['source']}), incident {record.get('incident_ref') or 'n/a'}",
        "", f"Conclusion: {result.get('conclusion') or 'n/a'}",
        f"Business impact: {result.get('business_impact') or 'n/a'}", "", "Recommended actions:",
    ]
    lines += [f"- [{a.get('urgency', '')}] {a.get('description', '')} (owner: {a.get('owner', '') or 'unassigned'})" for a in actions] or ["- none"]
    if result.get("remediation_script"):
        lines += ["", "Remediation script (review before running):", result["remediation_script"]]
    proposal = ado_integration.create_proposal(
        violation_class="misconfiguration",
        policy_name=f"zeroops:{record.get('scenario_id') or 'escalation'}",
        resource_id=record.get("incident_ref") or record["id"],
        support_owner="ops-team",
        inspector_reasoning=(result.get("narrative") or "")[:2000],
        title=f"[ZeroOps] {(result.get('conclusion') or record['question'])[:120]}",
        description="\n".join(lines),
        acceptance_criteria=(
            "A human reviewed the evidence and recorded a proposal decision. "
            "Execution and verification are tracked separately outside this workflow."
        ),
        priority=2,
        tags=["zeroops", "sre-agent-escalation", record.get("scenario_id") or "escalation"],
    )
    record = ledger.update(esc_id, proposal_id=proposal.id)
    _record_proposal_activity(record)
    return {"escalation": record, "proposal": proposal.to_dict()}


def _record_proposal_activity(record: dict) -> None:
    try:
        store = activity_store_module.get_activity_store()
        investigation_id = record["investigation_id"]
        store.update_investigation(investigation_id, phase="needs_review")
        store.append_event(
            investigation_id,
            actor="operator",
            kind=_activity_event_kind("proposal_created"),
            provenance="observed",
            payload={"status": "pending", "approval_required": True},
            idempotent=True,
        )
    except RuntimeError as exc:
        raise RetryableActivityError(
            "The proposal is saved in the ZeroOps ledger, but its Activity receipt could not be persisted. "
            f"Retry the same request. Activity error: {exc}"
        ) from exc


def _proposal_dict(proposal_id: str):
    proposal = ado_integration.get_proposal(proposal_id)
    return proposal.to_dict() if proposal else {"id": proposal_id, "note": "proposal held by another worker/instance"}


def decide(esc_id: str, *, decision: str, by: str = "ops-user", reason: str = "") -> dict:
    """Human gate: approve or reject the squad's proposal. Recorded in the ledger."""
    if decision not in ("approve", "reject", "resolve"):
        raise ValueError("decision must be 'approve', 'reject' or 'resolve'")
    ledger = get_ledger()
    record = ledger.get(esc_id)
    if record is None:
        raise KeyError(esc_id)
    status = {"approve": "approved", "reject": "rejected", "resolve": "resolved"}[decision]
    phase = {"approve": "approved", "reject": "rejected", "resolve": "closed_unverified"}[decision]
    decisions = (record.get("result") or {}).get("decisions") or []
    latest_decision = decisions[-1] if decisions else None
    same_recorded_decision = (
        record["status"] == status
        and isinstance(latest_decision, dict)
        and latest_decision.get("decision") == decision
    )
    retrying_recorded_decision = False
    if same_recorded_decision:
        record = _ensure_activity_for_mutation(record, operation="Decision")
        try:
            receipt_exists = activity_store_module.get_activity_store().has_event(
                record["investigation_id"],
                actor="reviewer",
                kind="decision_recorded",
                provenance="approved" if decision == "approve" else "reported",
                payload={"decision": decision, "phase": phase},
            )
        except RuntimeError as exc:
            raise RetryableActivityError(
                "The decision remains saved in the ZeroOps ledger, but its Activity receipt could not be checked. "
                f"Retry the same decision. Activity error: {exc}"
            ) from exc
        if receipt_exists and decision in ("approve", "reject"):
            raise ValueError(
                f"escalation {esc_id} is {record['status']!r}; only 'proposed' escalations can be approved/rejected"
            )
        retrying_recorded_decision = True
    if decision in ("approve", "reject") and record["status"] != "proposed" and not retrying_recorded_decision:
        raise ValueError(f"escalation {esc_id} is {record['status']!r}; only 'proposed' escalations can be approved/rejected")
    if not retrying_recorded_decision:
        record = _ensure_activity_for_mutation(record, operation="Decision")
    ado = None
    proposal_id = record.get("proposal_id")
    if proposal_id:
        ado = ado_integration.get_proposal(proposal_id)
        if ado is not None and not retrying_recorded_decision:
            if decision == "approve":
                ado = ado_integration.approve_proposal(proposal_id, approved_by=by)
            elif decision == "reject":
                ado = ado_integration.reject_proposal(proposal_id, reason=reason or f"rejected by {by}")
    if not retrying_recorded_decision:
        result = dict(record.get("result") or {})
        result.setdefault("decisions", []).append({
            "decision": decision, "by": by, "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        record = ledger.update(esc_id, status=status, result=result)
    try:
        store = activity_store_module.get_activity_store()
        store.update_investigation(record["investigation_id"], phase=phase)
        store.append_event(
            record["investigation_id"],
            actor="reviewer",
            kind="decision_recorded",
            provenance="approved" if decision == "approve" else "reported",
            payload={"decision": decision, "phase": phase},
            idempotent=True,
        )
    except RuntimeError as exc:
        raise RetryableActivityError(
            "The decision is saved in the ZeroOps ledger, but its Activity receipt could not be persisted. "
            f"Retry the same decision. Activity error: {exc}"
        ) from exc
    return {"escalation": record, "ado": ado}
