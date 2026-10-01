"""Tier-2 escalation: run the OGE squad for an SRE Agent hand-off.

``escalate`` records the hand-off in the ledger, runs the grounded
analysis pipeline in a background thread and waits up to ``wait_seconds``
for it. Long debates keep running after the caller returns; the SRE Agent
(or UI) polls ``get_escalation``. Nothing here changes Azure: the output is
a proposal that a human approves.
"""
import os
import threading
import traceback
from datetime import datetime, timezone

from app import ado_integration
from app.agents import analysis as analysis_service
from app.operations.models import FindingCategory, Severity
from app.zeroops import cost as cost_model
from app.zeroops import scenarios as scenario_mod
from app.zeroops.ledger import get_ledger

MAX_WAIT_SECONDS = 55
DEFAULT_WAIT_SECONDS = 45


def _subscription_ids() -> list:
    raw = os.environ.get("ZEROOPS_SUBSCRIPTION_ID", "").strip() or os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _compose_question(question: str, sre_summary: str, incident_ref: str) -> str:
    parts = [question.strip()]
    if incident_ref:
        parts.append(f"Incident reference: {incident_ref}.")
    if sre_summary:
        parts.append(f"Azure SRE Agent triage so far: {sre_summary.strip()[:2000]}")
    parts.append(
        "You are tier 2 for the Azure SRE Agent: reason about root cause and business impact, and return "
        "recommended actions a human can approve. Do not assume any action has already been taken."
    )
    return "\n".join(parts)


def _compact_result(analysis: dict) -> dict:
    final = analysis.get("final") or {}
    return {
        "agent": final.get("agent"), "agent_key": final.get("agent_key"),
        "schema_valid": final.get("schema_valid"), "conclusion": final.get("conclusion"),
        "business_impact": final.get("business_impact"), "confidence": final.get("confidence"),
        "narrative": final.get("narrative"), "recommended_actions": final.get("recommended_actions") or [],
        "valid_evidence_ids": final.get("valid_evidence_ids") or [], "missing_evidence": final.get("missing_evidence") or [],
        "specialists": sorted((analysis.get("specialists") or {}).keys()),
        "debate_used": bool(analysis.get("rebuttals")),
        "evaluation": analysis.get("evaluation"),
        "usage_summary": analysis.get("usage_summary"),
        "snapshot_id": analysis.get("snapshot_id"),
    }


def _run(esc_id: str, *, question: str, scenario, severity, category, debate: bool) -> None:
    ledger = get_ledger()
    try:
        ledger.update(esc_id, status="analyzing")
        analysis = analysis_service.analyze_operations(
            question=question, subscription_ids=_subscription_ids(), severity=severity or None,
            category=category or (scenario.category if scenario else None) or None,
            requested_agents=list(scenario.expected_agents) if scenario and scenario.expected_agents else None,
            force_debate=debate,
        )
        result = _compact_result(analysis)
        if scenario:
            result["remediation_script"] = scenario.to_dict()["remediation_script"]
        cost = cost_model.incident_cost(
            sre_profile=scenario.sre_task_profile if scenario else "incident_investigation",
            squad_usage=analysis.get("usage_summary"),
        )
        ledger.update(esc_id, status="proposed", routing=analysis.get("routing") or {}, result=result, cost=cost)
    except Exception as exc:  # noqa: BLE001 -- background boundary: record the failure explicitly in the ledger
        traceback.print_exc()
        ledger.update(esc_id, status="failed", error=str(exc)[:1000],
                      cost=cost_model.incident_cost(sre_profile=scenario.sre_task_profile if scenario else "incident_investigation"))


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
    worker = threading.Thread(
        target=_run, name=f"zeroops-{record['id']}", daemon=True,
        kwargs={"esc_id": record["id"], "question": _compose_question(question, sre_summary, incident_ref),
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
    if record.get("proposal_id"):
        return {"escalation": record, "proposal": _proposal_dict(record["proposal_id"])}
    result = record.get("result") or {}
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
        acceptance_criteria="A human reviewed the evidence and approved the change; the triggering alert has cleared.",
        priority=2,
        tags=["zeroops", "sre-agent-escalation", record.get("scenario_id") or "escalation"],
    )
    record = ledger.update(esc_id, proposal_id=proposal.id)
    return {"escalation": record, "proposal": proposal.to_dict()}


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
    if decision in ("approve", "reject") and record["status"] != "proposed":
        raise ValueError(f"escalation {esc_id} is {record['status']!r}; only 'proposed' escalations can be approved/rejected")
    ado = None
    proposal_id = record.get("proposal_id")
    if proposal_id and ado_integration.get_proposal(proposal_id):
        if decision == "approve":
            ado = ado_integration.approve_proposal(proposal_id, approved_by=by)
        elif decision == "reject":
            ado = ado_integration.reject_proposal(proposal_id, reason=reason or f"rejected by {by}")
    status = {"approve": "approved", "reject": "rejected", "resolve": "resolved"}[decision]
    result = dict(record.get("result") or {})
    result.setdefault("decisions", []).append({"decision": decision, "by": by, "reason": reason,
                                                 "at": datetime.now(timezone.utc).isoformat()})
    record = ledger.update(esc_id, status=status, result=result)
    return {"escalation": record, "ado": ado}
