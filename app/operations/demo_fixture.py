"""Centralized Demo-mode fixture for the Operations Center / Executive
Brief UI (see docs/OPERATIONS_API.md's "Demo fixture" section and
``GET /api/operations/demo`` in app/operations/routes.py).

This is the ONLY place demo/simulated data for the operations product
surface is authored -- templates/index.html's JavaScript must never
hardcode a fake DOM value or scoring formula for Demo mode; it always
renders whatever this module returns, using the exact same render
functions Live mode uses for /api/operations/brief, /queue, /handoff,
and /snapshot.

How this stays honest instead of just "pretty fake JSON": every field
below is produced by feeding hand-authored, schema-valid
``Finding``/``EvidenceReference``/``SLOSummary``/``CapacitySummary``
objects through the EXACT SAME deterministic pipeline real Azure
evidence goes through --
``app.operations.priority.prioritize_findings``,
``app.operations.brief.build_brief``, ``app.operations.queue.build_queue``,
``app.operations.handoff.build_handoff`` (including a real, disposable
``OperationsStateStore`` seeded with a scripted history of workflow
actions and one prior handoff, so "new/changed since prior handoff" is
computed live, never faked) -- and, for the one narrative "model
output" example, ``app.agents.routing.route``,
``app.agents.evidence.build_evidence_bundle``,
``app.agents.evaluation.evaluate``, and ``app.approval.analysis_action_metadata``.
The ONLY fabricated content anywhere in this module is a handful of
narrative strings a real model call would otherwise produce -- every
one of those is confined to ``_analysis_example``/``_briefing_example``
and the returned payload marks them ``"simulated": true`` so the UI can
label them accordingly (see requirement: "Demo vs Live remains explicit
everywhere. Never show simulated values as live.").

``GET /api/operations/demo`` returns ``build_demo_payload()`` unchanged
-- no LLM call, no Azure call, consistent with app/operations/routes.py's
"no LLM call" invariant (see that module's docstring).
"""

import os
import tempfile
from datetime import timedelta
from typing import Optional

from app.agents import evaluation as evaluation_module
from app.agents import routing as routing_module
from app.agents import schema as schema_module
from app.agents.evidence import build_evidence_bundle
from app.approval import analysis_action_metadata
from app.config import settings
from app.operations.brief import build_brief
from app.operations.handoff import build_handoff
from app.operations.models import (
    CapacitySummary,
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    SLOSummary,
    Severity,
    format_utc_iso,
    utc_now,
)
from app.operations.priority import prioritize_findings
from app.operations.queue import build_queue
from app.operations.service import CollectionEnvelope, summarize_coverage
from app.operations.snapshot import OperationsSnapshot
from app.operations.state import OperationsStateStore, merge_workflow_state
from app.profiles import REPO_ROOT

__all__ = ["build_demo_payload"]

_DEMO_SUBSCRIPTION_IDS = ("demo-subscription-0000-0000",)
_DEMO_RG = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups"

# Envelope statuses that represent a genuine collection attempt -- mirrors
# app.operations.snapshot._snapshot_status's public documented semantics
# (reimplemented here, not imported, since that helper is intentionally
# private to that module).
_APPLICABLE_STATUSES = {"ok", "error"}


def _status_from_envelopes(envelopes: list) -> str:
    applicable = [e for e in envelopes if e.status in _APPLICABLE_STATUSES]
    if not applicable:
        return "ok"
    error_count = sum(1 for e in applicable if e.status == "error")
    if error_count == len(applicable):
        return "error"
    return "partial" if error_count else "ok"


def _build_summary(ordered: list, coverage: dict) -> dict:
    """Mirrors app.operations.snapshot._build_summary's shape (kept as a
    small local copy rather than importing a private helper)."""
    by_severity: dict = {}
    by_category: dict = {}
    by_workflow_status: dict = {}
    executive_attention_count = 0
    approval_required_count = 0
    for item in ordered:
        finding = item["finding"]
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1
        by_category[finding["category"]] = by_category.get(finding["category"], 0) + 1
        workflow_status = item["workflow"]["status"]
        by_workflow_status[workflow_status] = by_workflow_status.get(workflow_status, 0) + 1
        if finding["executive_attention"]:
            executive_attention_count += 1
        if finding["approval_required"]:
            approval_required_count += 1
    return {
        "total_findings": len(ordered),
        "by_severity": by_severity,
        "by_category": by_category,
        "by_workflow_status": by_workflow_status,
        "executive_attention_count": executive_attention_count,
        "approval_required_count": approval_required_count,
        "source_coverage": coverage,
    }


def _build_findings(now) -> dict:
    """The demo evidence set: one Finding per FindingCategory value (13
    categories), a believable mix of ages/severities/workflow stories --
    see this module's docstring for the overall narrative."""

    def ev(source, title, observed_at, resource_id=None, reference=None):
        return EvidenceReference(source=source, title=title, observed_at=observed_at, resource_id=resource_id, reference=reference)

    findings = {}

    findings["hero_security"] = Finding(
        category=FindingCategory.SECURITY.value, severity=Severity.CRITICAL.value, status=FindingStatus.OPEN.value,
        title="NSG 'nsg-web-pe' allows inbound SSH (22) from any source",
        summary="Inbound security rule 'allow-any-ssh' on nsg-web-pe permits 0.0.0.0/0 -> tcp/22; added roughly 8 minutes ago.",
        business_impact="Publicly reachable SSH into the production web tier -- a single exposed or guessed credential becomes a full breach vector.",
        first_seen=now - timedelta(minutes=8), last_seen=now - timedelta(minutes=8),
        source="legacy_security_drift", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Web-Prod-RG/providers/Microsoft.Network/networkSecurityGroups/nsg-web-pe",
        recommended_action="Restrict nsg-web-pe's inbound rule to the corporate VPN CIDR and complete a key rotation for any credentials that may have been probed.",
        approval_required=True, executive_attention=True,
        evidence=[ev(EvidenceSource.RESOURCE_GRAPH.value, "Security rule 'allow-any-ssh' permits source '*' to destination port 22",
                     now - timedelta(minutes=8), f"{_DEMO_RG}/Web-Prod-RG/providers/Microsoft.Network/networkSecurityGroups/nsg-web-pe",
                     "Microsoft.Network/networkSecurityGroups/nsg-web-pe/securityRules/allow-any-ssh")],
    )

    findings["incident_checkout"] = Finding(
        category=FindingCategory.INCIDENT.value, severity=Severity.HIGH.value, status=FindingStatus.MITIGATING.value,
        title="App Service plan asp-checkout-prod elevated 5xx rate",
        summary="5xx error rate for asp-checkout-prod has been elevated (currently 3.1%, baseline 0.2%) since first detected.",
        business_impact="Checkout API customers intermittently see failed submissions during the affected window.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(minutes=5),
        source="azure_monitor_alerts", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Web-Prod-RG/providers/Microsoft.Web/serverfarms/asp-checkout-prod",
        recommended_action="Roll back the last checkout-service deployment if the error rate doesn't recover within 30 minutes; page payments on-call if 5xx rate exceeds 5%.",
        approval_required=False, executive_attention=True,
        # This narrative fixture IS the deterministic evidence: an
        # elevated 5xx rate actively causing customer-visible failed
        # checkout submissions right now -- exactly the kind of explicit,
        # confirmed impact app.operations.priority.is_customer_impacting
        # requires (never merely executive_attention/category/severity;
        # see reliability_checkout below, which is a real risk but NOT
        # yet an actual breach, and is deliberately customer_impacting=False).
        customer_impacting=True,
        evidence=[ev(EvidenceSource.AZURE_MONITOR_ALERT.value, "5xx rate alert fired: 3.1% over 15m window (threshold 1.0%)",
                     now - timedelta(hours=20), f"{_DEMO_RG}/Web-Prod-RG/providers/Microsoft.Web/serverfarms/asp-checkout-prod")],
    )

    findings["reliability_checkout"] = Finding(
        category=FindingCategory.RELIABILITY.value, severity=Severity.HIGH.value, status=FindingStatus.ACKNOWLEDGED.value,
        title="checkout-api error budget burn accelerating",
        summary="checkout-api's 30-day error budget remaining dropped from 40% to 22.5% over the last 24h.",
        business_impact="If the burn rate continues, checkout-api breaches its 99.9% availability SLO within roughly 36 hours.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(minutes=30),
        source="workload_slo", confidence=ConfidenceLevel.DERIVED.value,
        metadata={"workload": "checkout-api"},
        recommended_action="Investigate the correlated checkout deployment/5xx incident as the likely burn driver before the SLO breaches.",
        approval_required=False, executive_attention=True,
        # Deliberately customer_impacting=False (the default): the SLO
        # is at_risk/burning fast but has NOT actually breached yet --
        # "breaches ... within roughly 36 hours" is a forecast, not a
        # confirmed breach. Contrast with incident_checkout above, which
        # IS actively causing customer-visible failures right now.
        evidence=[ev(EvidenceSource.LOG_ANALYTICS_SLO.value, "checkout-api 30d SLO evaluation: 99.4% observed vs 99.9% objective",
                     now - timedelta(minutes=30))],
    )

    findings["capacity_quota"] = Finding(
        category=FindingCategory.CAPACITY.value, severity=Severity.HIGH.value, status=FindingStatus.OPEN.value,
        title="Standard Dv5 vCPU quota in eastus2 at 95% utilization",
        summary="380 of 400 Standard Dv5-family vCPUs are allocated in eastus2; headroom is 5%.",
        business_impact="The next autoscale event or deployment in eastus2 may fail with a quota error -- exactly when checkout-api may need to scale out.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(hours=1),
        source="capacity", confidence=ConfidenceLevel.CONFIRMED.value,
        recommended_action="File an Azure quota increase request for Standard Dv5 in eastus2 before the ~2-day forecasted exhaustion.",
        approval_required=True, executive_attention=True,
        evidence=[ev(EvidenceSource.ARM_COMPUTE_USAGE.value, "usage.compute eastus2 standardDv5Family: 380/400 vCPUs",
                     now - timedelta(hours=1))],
    )

    findings["cost_orphaned_disks"] = Finding(
        category=FindingCategory.COST.value, severity=Severity.LOW.value, status=FindingStatus.OPEN.value,
        title="3 orphaned managed disks in Analytics-RG",
        summary="3 unattached managed disks (no parent VM) have persisted for 45+ days in Analytics-RG.",
        business_impact="Approximately $102/month in recoverable waste.",
        first_seen=now - timedelta(days=45), last_seen=now - timedelta(days=1),
        source="legacy_resource_hygiene", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Analytics-RG",
        recommended_action="Snapshot then delete the unattached disks after a 30-day confirmation window with the Analytics team.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.RESOURCE_GRAPH.value, "3 Microsoft.Compute/disks with managedBy == null in Analytics-RG",
                     now - timedelta(days=1), f"{_DEMO_RG}/Analytics-RG")],
    )

    findings["compliance_storage"] = Finding(
        category=FindingCategory.COMPLIANCE.value, severity=Severity.MEDIUM.value, status=FindingStatus.OPEN.value,
        title="Storage account stgdemoreports01 fails 'require-private-endpoint' policy",
        summary="Azure Policy reports stgdemoreports01 as non-compliant with the org's 'require-private-endpoint' initiative.",
        business_impact="Reports data is reachable over the public endpoint until a private endpoint is provisioned.",
        first_seen=now - timedelta(minutes=90), last_seen=now - timedelta(minutes=90),
        source="legacy_policy_compliance", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Analytics-RG/providers/Microsoft.Storage/storageAccounts/stgdemoreports01",
        recommended_action="Provision a private endpoint for stgdemoreports01 and disable public network access.",
        approval_required=True, executive_attention=False,
        evidence=[ev(EvidenceSource.POLICY_INSIGHTS.value, "Policy assignment 'require-private-endpoint': NonCompliant",
                     now - timedelta(minutes=90), f"{_DEMO_RG}/Analytics-RG/providers/Microsoft.Storage/storageAccounts/stgdemoreports01")],
    )

    findings["ownership_legacy_rg"] = Finding(
        category=FindingCategory.OWNERSHIP.value, severity=Severity.LOW.value, status=FindingStatus.OPEN.value,
        title="Resource group Legacy-Migration-RG has no support-owner tag",
        summary="Legacy-Migration-RG is missing the required 'support-owner' tag.",
        business_impact="If a resource in this group has an incident, no team is automatically paged.",
        first_seen=now - timedelta(days=5), last_seen=now - timedelta(days=5),
        source="legacy_ownership", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Legacy-Migration-RG",
        recommended_action="Confirm the resource group is a decommission target; if not, assign a support-owner tag.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.RESOURCE_GRAPH.value, "Resource group tags: {} (no support-owner key)",
                     now - timedelta(days=5), f"{_DEMO_RG}/Legacy-Migration-RG")],
    )

    findings["change_nsg_apply"] = Finding(
        category=FindingCategory.CHANGE.value, severity=Severity.INFORMATIONAL.value, status=FindingStatus.OPEN.value,
        title="Terraform apply updated inbound rules on nsg-web-pe",
        summary="A Terraform apply (webapp-network module) modified nsg-web-pe's security rules about 25 minutes before the SSH-exposure finding was detected.",
        business_impact="Likely root cause of the newly-detected SSH exposure finding.",
        first_seen=now - timedelta(minutes=25), last_seen=now - timedelta(minutes=25),
        source="activity_log_change_health", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Web-Prod-RG/providers/Microsoft.Network/networkSecurityGroups/nsg-web-pe",
        recommended_action="Correlate with the change-approval record for this Terraform apply; confirm whether the rule change was intentional.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.ACTIVITY_LOG.value, "Microsoft.Network/networkSecurityGroups/write by svc-terraform-pipeline",
                     now - timedelta(minutes=25), f"{_DEMO_RG}/Web-Prod-RG/providers/Microsoft.Network/networkSecurityGroups/nsg-web-pe")],
    )

    findings["patch_legacy_etl"] = Finding(
        category=FindingCategory.PATCH.value, severity=Severity.HIGH.value, status=FindingStatus.OPEN.value,
        title="vm-legacy-etl01 missing 3 critical OS patches",
        summary="vm-legacy-etl01 has 3 critical-severity OS patches outstanding, the oldest published 34 days ago.",
        business_impact="Unpatched critical CVEs increase breach risk on a VM with access to the ETL data pipeline.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(hours=20),
        source="azure_update_manager", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/Legacy-Migration-RG/providers/Microsoft.Compute/virtualMachines/vm-legacy-etl01",
        recommended_action="Schedule a maintenance window with the ETL app owner to apply the outstanding patches.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.UPDATE_MANAGER.value, "3 Critical classification patches missing, oldest 34 days old",
                     now - timedelta(hours=20), f"{_DEMO_RG}/Legacy-Migration-RG/providers/Microsoft.Compute/virtualMachines/vm-legacy-etl01")],
    )

    findings["automation_runbook_failed"] = Finding(
        category=FindingCategory.AUTOMATION.value, severity=Severity.MEDIUM.value, status=FindingStatus.OPEN.value,
        title="Runbook 'nightly-cleanup' failed its last 3 executions",
        summary="Automation runbook 'nightly-cleanup' has failed 3 consecutive nightly runs with a permissions error.",
        business_impact="Stale temp resources are no longer being cleaned up nightly, quietly adding cost.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(hours=8),
        source="azure_automation_job", confidence=ConfidenceLevel.CONFIRMED.value,
        recommended_action="Grant the runbook's managed identity Contributor on the target resource group and re-run manually to confirm the fix.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.AUTOMATION_JOB.value, "Job status Failed x3: AuthorizationFailed on target RG",
                     now - timedelta(hours=8))],
    )

    findings["telemetry_gap"] = Finding(
        category=FindingCategory.TELEMETRY.value, severity=Severity.LOW.value, status=FindingStatus.OPEN.value,
        title="12 VMs missing diagnostic settings",
        summary="12 of 64 monitored VMs have no diagnostic settings forwarding to Log Analytics.",
        business_impact="Any incident on these 12 VMs would be diagnosed blind, with no Activity/Metric telemetry history.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(hours=20),
        source="telemetry_coverage", confidence=ConfidenceLevel.DERIVED.value,
        recommended_action="Apply the standard diagnostic-settings policy assignment to the 12 gapped VMs.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.TELEMETRY_COVERAGE.value, "12/64 VMs: no diagnostic setting targeting Log Analytics",
                     now - timedelta(hours=20))],
    )

    findings["backup_failed_job"] = Finding(
        category=FindingCategory.BACKUP.value, severity=Severity.MEDIUM.value, status=FindingStatus.OPEN.value,
        title="Backup job for vm-sap-batch01 failed 2 consecutive nights",
        summary="Azure Backup job for vm-sap-batch01 failed with an inconsistent-snapshot error on the last 2 nightly runs.",
        business_impact="Recovery point objective for the SAP batch VM is currently 3 days stale instead of 1.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(hours=10),
        source="azure_backup_job", confidence=ConfidenceLevel.CONFIRMED.value,
        resource_id=f"{_DEMO_RG}/SAP-Production-RG/providers/Microsoft.Compute/virtualMachines/vm-sap-batch01",
        recommended_action="Re-run the backup job manually and open a support case if the inconsistent-snapshot error recurs.",
        approval_required=False, executive_attention=False,
        evidence=[ev(EvidenceSource.BACKUP_JOB.value, "Backup job status Failed x2: InconsistentSnapshot",
                     now - timedelta(hours=10), f"{_DEMO_RG}/SAP-Production-RG/providers/Microsoft.Compute/virtualMachines/vm-sap-batch01")],
    )

    findings["certificate_expiry"] = Finding(
        category=FindingCategory.CERTIFICATE.value, severity=Severity.MEDIUM.value, status=FindingStatus.OPEN.value,
        title="TLS certificate for api.demo-contoso.com expires in 9 days",
        summary="Key Vault certificate 'api-demo-contoso-com' expires in 9 days with no auto-rotation configured.",
        business_impact="Certificate expiry would take the public API gateway offline for all customers.",
        first_seen=now - timedelta(hours=20), last_seen=now - timedelta(hours=20),
        source="key_vault_expiry", confidence=ConfidenceLevel.CONFIRMED.value,
        recommended_action="Rotate the certificate now or confirm auto-rotation is configured before the 9-day deadline.",
        approval_required=False, executive_attention=True,
        evidence=[ev(EvidenceSource.KEY_VAULT_EXPIRY.value, "Certificate 'api-demo-contoso-com': notAfter in 9 days, no rotation policy",
                     now - timedelta(hours=20))],
    )

    return findings


def _build_envelopes(findings: dict, now) -> list:
    iso_now = format_utc_iso(now)
    slo_summary = SLOSummary(
        workload="checkout-api", state="at_risk", objective_pct=99.9, observed_pct=99.4, window_hours=720,
        criticality="customer_facing", evaluated_at=iso_now, good_count=715637, total_count=719900,
        error_budget_remaining_pct=22.5, burn_rate=2.3,
    )
    capacity_summary = CapacitySummary(
        resource_scope="compute:eastus2/standardDv5Family", metric="vCPU quota", current=380.0, limit=400.0,
        threshold_state="critical", evaluated_at=iso_now, headroom_pct=5.0, forecast_state="available",
        forecast_exhaustion_at=format_utc_iso(now + timedelta(days=2)),
    )
    return [
        CollectionEnvelope(source="legacy_security_drift", status="ok", collected_at=iso_now, findings=[findings["hero_security"]]),
        CollectionEnvelope(source="azure_monitor_alerts", status="ok", collected_at=iso_now, findings=[findings["incident_checkout"]]),
        CollectionEnvelope(source="workload_slo", status="ok", collected_at=iso_now, findings=[findings["reliability_checkout"]], summaries=[slo_summary]),
        CollectionEnvelope(source="capacity", status="ok", collected_at=iso_now, findings=[findings["capacity_quota"]], summaries=[capacity_summary]),
        CollectionEnvelope(source="legacy_resource_hygiene", status="ok", collected_at=iso_now, findings=[findings["cost_orphaned_disks"]]),
        CollectionEnvelope(source="legacy_policy_compliance", status="ok", collected_at=iso_now, findings=[findings["compliance_storage"]]),
        CollectionEnvelope(source="legacy_ownership", status="ok", collected_at=iso_now, findings=[findings["ownership_legacy_rg"]]),
        CollectionEnvelope(source="activity_log_change_health", status="ok", collected_at=iso_now, findings=[findings["change_nsg_apply"]]),
        CollectionEnvelope(source="azure_update_manager", status="ok", collected_at=iso_now, findings=[findings["patch_legacy_etl"]]),
        CollectionEnvelope(source="azure_automation_job", status="ok", collected_at=iso_now, findings=[findings["automation_runbook_failed"]]),
        CollectionEnvelope(source="telemetry_coverage", status="ok", collected_at=iso_now, findings=[findings["telemetry_gap"]]),
        CollectionEnvelope(source="azure_backup_job", status="ok", collected_at=iso_now, findings=[findings["backup_failed_job"]]),
        CollectionEnvelope(source="key_vault_expiry", status="ok", collected_at=iso_now, findings=[findings["certificate_expiry"]]),
        CollectionEnvelope(
            source="cost_management_usage", status="error", collected_at=iso_now, error=(
                "Cost Management API returned 403: the assigned Reader role is missing "
                "Microsoft.CostManagement/query/action; grant 'Cost Management Reader' to the Managed Identity."
            ),
        ),
        CollectionEnvelope(
            source="microsoft_defender_assessment", status="not_configured", collected_at=iso_now,
            error="Microsoft Defender for Cloud is not enabled on this subscription.",
        ),
    ]


# Prior handoff is deliberately BEFORE the "old" findings' first_seen (-20h)
# and AFTER the "brand new" findings' first_seen (-25min/-8min/-90min) --
# see this module's docstring: this is what makes new_since_prior/
# changed_since_prior in the returned handoff a genuine computation, not a
# hand-picked list.
_PRIOR_HANDOFF_OFFSET_HOURS = 15


def _seed_workflow_history(store: OperationsStateStore, findings: dict, now) -> None:
    """Script a believable shift history against a real, disposable
    OperationsStateStore: some findings already triaged before the prior
    handoff, two updated afterward (-> changed_since_prior), one snoozed,
    one dismissed with a reason -- then record that prior handoff itself.
    Every timestamp passed as `now=` backdates the action for real (see
    OperationsStateStore.apply_action's `now` parameter); nothing here is
    a hand-written workflow dict."""
    prior_at = now - timedelta(hours=_PRIOR_HANDOFF_OFFSET_HOURS)

    store.apply_action(findings["incident_checkout"].id, "assign", actor="oncall-bot", owner="web-platform-oncall", now=now - timedelta(hours=20))
    store.apply_action(findings["incident_checkout"].id, "acknowledge", actor="web-platform-oncall", now=now - timedelta(hours=19))
    store.apply_action(findings["incident_checkout"].id, "start", actor="web-platform-oncall", now=now - timedelta(hours=18))

    store.apply_action(findings["reliability_checkout"].id, "assign", actor="sre-lead", owner="sre-lead", now=now - timedelta(hours=10))
    store.apply_action(findings["reliability_checkout"].id, "acknowledge", actor="sre-lead", now=now - timedelta(hours=9))

    store.apply_action(findings["backup_failed_job"].id, "assign", actor="storage-ops", owner="storage-ops", now=now - timedelta(hours=9))
    store.apply_action(findings["backup_failed_job"].id, "acknowledge", actor="storage-ops", now=now - timedelta(hours=9))

    store.apply_action(
        findings["patch_legacy_etl"].id, "snooze", actor="patch-owner",
        snooze_until=format_utc_iso(now + timedelta(days=3)),
        reason="Waiting on the ETL app owner's next change window", now=now - timedelta(hours=6),
    )

    store.apply_action(
        findings["ownership_legacy_rg"].id, "dismiss", actor="governance-bot",
        reason="Confirmed decommission target; owner assignment not required", now=now - timedelta(hours=2),
    )

    open_at_prior_handoff = [
        findings[key].id for key in (
            "incident_checkout", "reliability_checkout", "capacity_quota", "cost_orphaned_disks",
            "patch_legacy_etl", "automation_runbook_failed", "telemetry_gap", "backup_failed_job",
            "certificate_expiry",
        )
    ]
    store.record_handoff(
        created_by="overnight-oncall", content_hash="0" * 32, open_finding_ids=open_at_prior_handoff,
        summary={"open_item_count": len(open_at_prior_handoff)}, now=prior_at,
    )


def _build_snapshot(all_findings: list, envelopes: list, store: OperationsStateStore, now) -> OperationsSnapshot:
    prioritized = prioritize_findings(all_findings, now=now)
    merged = merge_workflow_state([pf.finding for pf in prioritized], store, now=now)
    merged_by_id = {item["finding"]["id"]: item for item in merged}
    ordered = []
    for pf in prioritized:
        item = merged_by_id[pf.finding.id]
        item["priority"] = {"band": pf.band, "factors": pf.factors.to_dict()}
        ordered.append(item)

    coverage = summarize_coverage(envelopes)
    generated_at = format_utc_iso(now)
    return OperationsSnapshot(
        id="snap-demo0000000000",
        generated_at=generated_at,
        subscription_ids=_DEMO_SUBSCRIPTION_IDS,
        status=_status_from_envelopes(envelopes),
        envelopes=envelopes,
        findings=ordered,
        coverage=coverage,
        source_errors=[{"source": e.source, "status": e.status, "error": e.error} for e in envelopes if e.status == "error"],
        summary=_build_summary(ordered, coverage),
    )


def _analysis_example(snapshot: OperationsSnapshot, hero_id: str, now) -> dict:
    """A simulated (`"simulated": true`) example of what
    ``POST /api/operations/analyze`` returns for the hero finding --
    every field EXCEPT the narrative/conclusion/business_impact/
    recommended-action text is computed by the real routing/evidence/
    approval/evaluation logic (see module docstring)."""
    bundle = build_evidence_bundle(snapshot, finding_id=hero_id)
    routing_decision = routing_module.route(bundle)
    agent_key = routing_decision.specialist_agents[0]
    agent_cfg = settings.agents[agent_key]

    result = schema_module.AgentAnalysisResult(
        conclusion="Immediate breach exposure: SSH is open to the entire internet on the production web tier.",
        business_impact="Any internet host can attempt SSH against nsg-web-pe; a single guessed or leaked credential grants a foothold into production.",
        confidence="high",
        evidence_ids=(hero_id,),
        missing_evidence=(),
        recommended_actions=(
            schema_module.RecommendedAction(
                description="Update the NSG rule on nsg-web-pe to restrict inbound SSH to the corporate VPN CIDR range.",
                owner="network-security@contoso.com", urgency="immediate", approval_required=True,
            ),
            schema_module.RecommendedAction(
                description="Complete a key rotation for any credentials reachable via nsg-web-pe in case of probing.",
                owner="security-ops@contoso.com", urgency="immediate", approval_required=True,
            ),
        ),
        narrative=(
            "[SIMULATED -- demo mode never calls a model] The correlated change finding shows a Terraform apply "
            "modified nsg-web-pe's rules about 25 minutes before this exposure was detected, which is the leading "
            "root-cause hypothesis. Recommend restricting the rule and rotating credentials immediately; this is a "
            "production network change and requires human approval before it's applied."
        ),
    )
    known_ids = bundle.known_ids()
    valid_ids, unsupported_ids = schema_module.validate_evidence_ids(result.evidence_ids, known_ids)
    action_metadata = [analysis_action_metadata(action.description) for action in result.recommended_actions]
    evaluation_result = evaluation_module.evaluate(
        result=result, schema_valid=True, bundle_known_ids=known_ids, action_metadata=action_metadata,
        debate_used=False, agents_consulted=1,
    )
    actions_payload = [{**action.to_dict(), "approval": metadata} for action, metadata in zip(result.recommended_actions, action_metadata)]

    return {
        "simulated": True,
        "question": "What is the risk from this finding and what should we do?",
        "generated_at": format_utc_iso(now),
        "snapshot_id": snapshot.id,
        "routing": routing_decision.to_dict(),
        "evidence_bundle": bundle.to_dict(),
        "specialists": {
            agent_key: {
                "agent_key": agent_key, "agent": agent_cfg.name, "role": agent_cfg.role, "model": agent_cfg.deployment,
                "structured_output_used": False, "schema_valid": True, "result": result.to_dict(), "schema_error": None,
                "raw_text_snippet": None, "usage": {"prompt_tokens": 0, "completion_tokens": 0, "estimated_cost_usd": 0.0},
            },
        },
        "rebuttals": None,
        "final": {
            "agent": agent_cfg.name, "agent_key": agent_key, "schema_valid": True,
            "conclusion": result.conclusion, "business_impact": result.business_impact, "confidence": result.confidence,
            "narrative": result.narrative, "evidence_ids": list(result.evidence_ids),
            "unsupported_evidence_ids": unsupported_ids, "valid_evidence_ids": valid_ids,
            "missing_evidence": list(result.missing_evidence), "recommended_actions": actions_payload,
            "structured_output_used": False,
        },
        "evaluation": evaluation_result.to_dict(),
        "model_metadata": {
            "backend": "demo-fixture (simulated, no model call)",
            "agent_definition_version": settings.agent_definition_version, "prompt_versions": {},
        },
    }


def _briefing_example(snapshot: OperationsSnapshot, now) -> dict:
    """A simulated (`"simulated": true`) example of what
    ``POST /api/operations/briefing`` returns -- one coordinator voice
    plus collapsed supporting_analysis bullets, matching
    app.agents.analysis.build_briefing's shape exactly."""
    bundle = build_evidence_bundle(snapshot)
    routing_decision = routing_module.route(bundle)
    coordinator_cfg = settings.agents[routing_module.COORDINATOR_KEY]

    hero = next(item for item in snapshot.findings if item["finding"]["category"] == FindingCategory.SECURITY.value)
    capacity = next(item for item in snapshot.findings if item["finding"]["category"] == FindingCategory.CAPACITY.value)
    reliability = next(item for item in snapshot.findings if item["finding"]["category"] == FindingCategory.RELIABILITY.value)
    evidence_ids = tuple(dict.fromkeys(
        eid for eid in (hero["finding"]["id"], capacity["finding"]["id"], reliability["finding"]["id"]) if eid in bundle.known_ids()
    ))

    result = schema_module.AgentAnalysisResult(
        conclusion="One active breach exposure, one at-risk customer-facing SLO, and one capacity constraint need attention this shift.",
        business_impact="A publicly-open SSH rule is the most urgent item; checkout-api's SLO burn and eastus2 capacity headroom compound the same incident.",
        confidence="high",
        evidence_ids=evidence_ids,
        missing_evidence=(),
        recommended_actions=(
            schema_module.RecommendedAction(
                description="Restrict the nsg-web-pe NSG rule to the corporate VPN CIDR immediately; this is a production network change.",
                owner="network-security@contoso.com", urgency="immediate", approval_required=True,
            ),
            schema_module.RecommendedAction(
                description="File the Standard Dv5/eastus2 quota increase request today, ahead of the 2-day forecasted exhaustion.",
                owner="cloud-platform@contoso.com", urgency="scheduled", approval_required=True,
            ),
        ),
        narrative=(
            "[SIMULATED -- demo mode never calls a model] The three findings share one thread: a change to nsg-web-pe "
            "correlates with the new security exposure, and checkout-api's SLO burn plus the eastus2 capacity "
            "constraint both point at the same service under load. Recommend addressing the NSG exposure first, "
            "then the quota request so a mitigating scale-out isn't blocked."
        ),
    )
    known_ids = bundle.known_ids()
    valid_ids, unsupported_ids = schema_module.validate_evidence_ids(result.evidence_ids, known_ids)
    action_metadata = [analysis_action_metadata(action.description) for action in result.recommended_actions]
    evaluation_result = evaluation_module.evaluate(
        result=result, schema_valid=True, bundle_known_ids=known_ids, action_metadata=action_metadata,
        debate_used=routing_decision.debate, agents_consulted=len(routing_decision.specialist_agents),
    )
    actions_payload = [{**action.to_dict(), "approval": metadata} for action, metadata in zip(result.recommended_actions, action_metadata)]

    supporting_analysis = []
    for agent_key in routing_decision.specialist_agents:
        cfg = settings.agents[agent_key]
        supporting_analysis.append({
            "agent_key": agent_key, "agent": cfg.name, "role": cfg.role, "schema_valid": True,
            "confidence": "high", "conclusion": f"[SIMULATED] {cfg.name}'s domain findings are reflected in the coordinator's conclusion above.",
        })

    return {
        "simulated": True,
        "generated_at": format_utc_iso(now),
        "snapshot_id": snapshot.id,
        "routing": routing_decision.to_dict(),
        "coordinator": {
            "agent": coordinator_cfg.name, "agent_key": routing_module.COORDINATOR_KEY, "schema_valid": True,
            "conclusion": result.conclusion, "business_impact": result.business_impact, "confidence": result.confidence,
            "narrative": result.narrative, "evidence_ids": list(result.evidence_ids),
            "unsupported_evidence_ids": unsupported_ids, "valid_evidence_ids": valid_ids,
            "missing_evidence": list(result.missing_evidence), "recommended_actions": actions_payload,
            "structured_output_used": False,
        },
        "supporting_analysis": supporting_analysis,
        "evaluation": evaluation_result.to_dict(),
        "model_metadata": {
            "backend": "demo-fixture (simulated, no model call)",
            "agent_definition_version": settings.agent_definition_version, "prompt_versions": {},
        },
    }


def build_demo_payload(now: Optional[object] = None) -> dict:
    """Build the full Demo-mode payload for the Operations product
    surface: `{"meta", "snapshot", "brief", "queue", "handoff",
    "analysis_example", "briefing_example"}` -- the same schemas
    `/api/operations/{snapshot,brief,queue,handoff}` and
    `/api/operations/{analyze,briefing}` return for Live mode, so
    templates/index.html renders Demo and Live data with the exact same
    functions. See module docstring for how this stays honest rather
    than hand-faked."""
    now = now or utc_now()
    findings = _build_findings(now)
    all_findings = list(findings.values())
    envelopes = _build_envelopes(findings, now)

    # A real, disposable OperationsStateStore -- created under a unique
    # name inside the repo (never /tmp) and removed in `finally`, so a
    # burst of concurrent demo requests never collide and nothing is
    # left behind on disk.
    fd, db_path = tempfile.mkstemp(prefix=".ops_demo_fixture_", suffix=".db", dir=str(REPO_ROOT))
    os.close(fd)
    os.remove(db_path)
    store = OperationsStateStore(db_path)
    try:
        _seed_workflow_history(store, findings, now)
        snapshot = _build_snapshot(all_findings, envelopes, store, now)
        brief = build_brief(snapshot, now=now)
        queue = build_queue(snapshot.findings, page=1, page_size=50)
        handoff = build_handoff(snapshot, state_store=store, now=now)
        hero_id = findings["hero_security"].id
        analysis_example = _analysis_example(snapshot, hero_id, now)
        briefing_example = _briefing_example(snapshot, now)
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    return {
        "meta": {
            "demo": True,
            "label": (
                "Simulated demo data -- not live Azure. Findings/priorities/workflow/handoff are computed by the "
                "same deterministic logic real evidence goes through; only the finding content and the two AI "
                "narrative examples (analysis_example/briefing_example) are hand-authored."
            ),
            "hero_finding_id": hero_id,
            "generated_at": format_utc_iso(now),
        },
        "snapshot": {
            "id": snapshot.id, "generated_at": snapshot.generated_at, "status": snapshot.status,
            "subscription_ids": list(snapshot.subscription_ids), "coverage": snapshot.coverage,
            "summary": snapshot.summary, "source_errors": snapshot.source_errors,
        },
        "brief": brief,
        "queue": queue,
        "handoff": handoff,
        "analysis_example": analysis_example,
        "briefing_example": briefing_example,
    }
