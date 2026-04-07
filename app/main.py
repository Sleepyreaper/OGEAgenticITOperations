"""OGE Ops Council — Flask application."""

from flask import Flask, render_template, request, jsonify, Response
import json
import traceback
from app.agents.runner import run_council, run_council_streaming, call_agent
from app.agents.demos import DEMO_SCENARIOS
from app.config import settings
from app import azure_data
from app import ado_integration


def create_app():
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )

    # ─── Pages ──────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html", demos=DEMO_SCENARIOS)

    # ─── API ────────────────────────────────────────────────

    @app.route("/api/demos", methods=["GET"])
    def list_demos():
        return jsonify(DEMO_SCENARIOS)

    @app.route("/api/ask", methods=["POST"])
    def ask():
        """Run a question through the Ops Council.

        Body: { "question": str, "context_data": str (optional),
                "agents": list[str] (optional), "mode": "demo"|"live" }
        """
        body = request.get_json(force=True)
        question = body.get("question", "").strip()
        if not question:
            return jsonify({"error": "question is required"}), 400

        context_data = body.get("context_data", "")
        agents = body.get("agents")
        mode = body.get("mode", "demo")
        subs = body.get("subs")  # subscription IDs for live mode

        # In live mode, gather real Azure data as context
        if mode == "live" and not context_data:
            try:
                sub_ids = subs if subs else [settings.subscription_id]
                scan = _gather_live_scan(sub_ids)
                context_data = json.dumps(scan, indent=2, default=str)
            except Exception as e:
                traceback.print_exc()
                context_data = f"[Error gathering live data: {e}]"

        try:
            result = run_council(question, context_data, agents)
            result["mode"] = mode
            return jsonify(result)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/ask/stream", methods=["POST"])
    def ask_stream():
        """SSE endpoint — streams each agent response as it completes."""
        body = request.get_json(force=True)
        question = body.get("question", "").strip()
        if not question:
            return jsonify({"error": "question is required"}), 400

        context_data = body.get("context_data", "")
        agents = body.get("agents")
        mode = body.get("mode", "demo")
        subs = body.get("subs")  # subscription IDs for live mode

        if mode == "live" and not context_data:
            try:
                sub_ids = subs if subs else [settings.subscription_id]
                scan = _gather_live_scan(sub_ids)
                context_data = json.dumps(scan, indent=2, default=str)
            except Exception as e:
                context_data = f"[Error gathering live data: {e}]"

        def generate():
            try:
                for event in run_council_streaming(question, context_data, agents):
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                yield f"data: {json.dumps({'phase': 'done'})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'phase': 'error', 'error': str(e)})}\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/demo/<scenario_id>/stream", methods=["POST"])
    def run_demo_stream(scenario_id):
        """SSE endpoint for demo scenarios."""
        scenario = DEMO_SCENARIOS.get(scenario_id)
        if not scenario:
            return jsonify({"error": f"Unknown scenario: {scenario_id}"}), 404

        def generate():
            try:
                for event in run_council_streaming(
                    scenario["question"], scenario["data"], scenario["agents"]
                ):
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                yield f"data: {json.dumps({'phase': 'done'})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'phase': 'error', 'error': str(e)})}\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/remediate", methods=["POST"])
    def generate_remediation():
        """Generate Terraform/CLI remediation code based on a crew analysis.

        Body: { "context": str (the crew's analysis to remediate) }
        """
        body = request.get_json(force=True)
        context = body.get("context", "").strip()
        if not context:
            return jsonify({"error": "context is required"}), 400

        remediation_prompt = f"""Based on this operational analysis, generate complete remediation artifacts that an ops engineer can review and deploy through their normal CI/CD pipeline.

ANALYSIS:
{context}

Generate FOUR distinct artifacts, each in its own clearly labeled code block:

1. **main.tf** — Terraform configuration to remediate the issue. Follow these organizational standards:
   - Use variables for subscription_id, resource_group, location
   - Include proper tags (support-owner, environment, managed-by = "ops-council")
   - Use azurerm provider with required_version constraint
   - Include comments explaining the remediation rationale

2. **variables.tf** — All variable declarations with descriptions and sensible defaults

3. **remediate.sh** — Azure CLI script for immediate remediation. Include:
   - Pre-flight checks (az account show, confirm subscription)
   - The actual fix commands
   - Post-fix validation commands
   - Comments explaining each step

4. **RUNBOOK.md** — A brief runbook entry for the ops team:
   - Issue summary (1 sentence)
   - Risk if not addressed
   - Remediation steps (numbered)
   - Validation steps
   - Rollback procedure
   - Estimated time to complete

If the analysis recommends NOT making changes, generate a RUNBOOK.md explaining why the current config is correct and what documentation should be updated to prevent this question from recurring.

These artifacts should be ready for a human to review, not auto-execute. The ops team will put the Terraform through their standard PR → review → apply process."""

        def generate():
            try:
                # Use The Roughneck (foundry-gpt) for remediation — he knows the standards
                roughneck_cfg = settings.agents["standards_architect"]
                result = call_agent(roughneck_cfg, remediation_prompt)
                yield f"data: {json.dumps({'phase': 'remediation', 'result': result}, default=str)}\n\n"
                yield f"data: {json.dumps({'phase': 'done'})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'phase': 'error', 'error': str(e)})}\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/demo/<scenario_id>", methods=["POST"])
    def run_demo(scenario_id):
        """Run a pre-built demo scenario."""
        scenario = DEMO_SCENARIOS.get(scenario_id)
        if not scenario:
            return jsonify({"error": f"Unknown scenario: {scenario_id}"}), 404

        try:
            result = run_council(
                scenario["question"],
                scenario["data"],
                scenario["agents"],
            )
            result["mode"] = "demo"
            return jsonify(result)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ─── Live Azure Data ────────────────────────────────────

    @app.route("/api/scan", methods=["GET"])
    def scan_subscription():
        """Scan the real Azure subscription and return environment data."""
        try:
            data = _gather_live_scan()
            return jsonify(data)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/subscriptions", methods=["GET"])
    def list_subs():
        """Discover all subscriptions accessible to the Managed Identity."""
        try:
            subs = azure_data.list_subscriptions()
            # Mark the default/configured subscription
            default_sub = settings.subscription_id
            for s in subs:
                s["default"] = s["id"] == default_sub
            return jsonify({"subscriptions": subs, "count": len(subs), "default": default_sub})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/scan/overview", methods=["GET"])
    def scan_overview():
        """Scan subscriptions for dashboard KPI cards.

        Query params:
          ?subs=all          — scan all accessible subscriptions
          ?subs=id1,id2,id3  — scan specific subscriptions
          (none)             — scan the default configured subscription
        """
        try:
            sub_ids = _parse_sub_ids()

            # For Resource Graph queries — pass all sub IDs at once (efficient)
            resources = azure_data.get_all_resources(subscription_ids=sub_ids)
            tagging = azure_data.get_tagging_compliance(subscription_ids=sub_ids)
            orphaned = azure_data.get_orphaned_disks(subscription_ids=sub_ids)
            public_ips = azure_data.get_public_endpoints(subscription_ids=sub_ids)

            rg_count = len(set(r.get("resourceGroup", "") for r in resources))
            tagged = sum(1 for t in tagging if t.get("supportOwner"))
            total_rgs = len(tagging)
            findings = len(orphaned) + len(public_ips) + len([t for t in tagging if not t.get("supportOwner")])

            # Azure Advisor — platform-verified recommendations (the evidence)
            advisor_recs = []
            try:
                for sid in sub_ids:
                    advisor_recs.extend(azure_data.get_advisor_recommendations(sid))
            except Exception:
                pass

            # Deep intelligence — cross-resource correlation (the stuff Advisor CAN'T do)
            deep = {}
            try:
                deep = azure_data.get_deep_analysis(subscription_ids=sub_ids)
            except Exception:
                pass

            # Security drift — dangerous open NSG rules
            security_drift = []
            try:
                security_drift = azure_data.detect_security_drift(subscription_ids=sub_ids)
            except Exception:
                pass

            # Insecure storage — public blob access
            insecure_storage = []
            try:
                insecure_storage = azure_data.detect_insecure_storage(subscription_ids=sub_ids)
            except Exception:
                pass

            # Policy compliance (per-sub, then aggregate)
            policy_compliance = {}
            try:
                for sid in sub_ids:
                    pc = azure_data.get_policy_compliance_summary(sid)
                    if not policy_compliance:
                        policy_compliance = pc
                    else:
                        for k in ("total_policies", "non_compliant_policies", "non_compliant_resources", "compliant_resources", "total_resources"):
                            policy_compliance[k] = policy_compliance.get(k, 0) + pc.get(k, 0)
                        if policy_compliance.get("total_resources", 0) > 0:
                            policy_compliance["compliance_pct"] = round(
                                (1 - policy_compliance["non_compliant_resources"] / policy_compliance["total_resources"]) * 100, 1)
                        policy_compliance.setdefault("top_non_compliant_assignments", []).extend(pc.get("top_non_compliant_assignments", []))
            except Exception:
                pass

            # Resource health — per-resource availability
            resource_health = []
            try:
                for sid in sub_ids:
                    resource_health.extend(azure_data.get_resource_health_statuses(sid))
            except Exception:
                pass

            health_summary = {"Available": 0, "Degraded": 0, "Unavailable": 0, "Unknown": 0}
            degraded_resources = []
            for rh in resource_health:
                status = rh.get("status", "Unknown")
                health_summary[status] = health_summary.get(status, 0) + 1
                if status in ("Degraded", "Unavailable"):
                    degraded_resources.append(rh)

            # Azure Service Health — platform incidents affecting us
            service_health = []
            try:
                for sid in sub_ids:
                    service_health.extend(azure_data.get_service_health_events(sid, days=30))
            except Exception:
                pass

            advisor_by_category = {}
            for r in advisor_recs:
                cat = r.get("category", "Unknown")
                advisor_by_category[cat] = advisor_by_category.get(cat, 0) + 1

            # Group resources by subscription for multi-sub visibility
            resources_by_sub = {}
            for r in resources:
                sid = r.get("subscriptionId", "unknown")
                resources_by_sub.setdefault(sid, []).append(r)

            return jsonify({
                "subscription_ids": sub_ids,
                "subscription_count": len(sub_ids),
                "total_resources": len(resources),
                "resource_groups": rg_count,
                "findings": findings,
                "tagging": {
                    "total": total_rgs,
                    "with_support_owner": tagged,
                    "compliance_pct": round(tagged / total_rgs * 100, 1) if total_rgs else 0,
                    "non_compliant": [t["name"] for t in tagging if not t.get("supportOwner")],
                },
                "orphaned_disks": len(orphaned),
                "orphaned_disk_details": orphaned,
                "public_ips": len(public_ips),
                "public_ip_details": public_ips,
                "resources_by_type": _count_by(resources, "type"),
                "resources_by_rg": _count_by(resources, "resourceGroup"),
                "advisor": {
                    "total": len(advisor_recs),
                    "by_category": advisor_by_category,
                    "high_impact": [r for r in advisor_recs if r.get("impact") == "High"][:10],
                    "recommendations": advisor_recs[:20],
                },
                "security_drift": security_drift,
                "insecure_storage": insecure_storage,
                "resource_health": {
                    "summary": health_summary,
                    "total_monitored": len(resource_health),
                    "degraded": degraded_resources,
                },
                "service_health": service_health,
                "deep_analysis": deep,
                "policy_compliance": policy_compliance,
                "resources_by_subscription": {sid: len(rs) for sid, rs in resources_by_sub.items()},
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "healthy",
            "agents": list(settings.agents.keys()),
            "openai_endpoint": settings.openai_endpoint,
            "subscription_id": settings.subscription_id,
        })

    # ─── Chaos Demo ─────────────────────────────────────────

    @app.route("/api/chaos/create", methods=["POST"])
    def chaos_create():
        """Create a security problem — opens SSH to the world on an NSG."""
        try:
            result = azure_data.create_chaos_nsg_rule()
            return jsonify({"status": "chaos_created", "detail": result})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/chaos/cleanup", methods=["POST"])
    def chaos_cleanup():
        """Clean up the chaos rule."""
        try:
            result = azure_data.cleanup_chaos_nsg_rule()
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/scan/security", methods=["GET"])
    def scan_security():
        """Quick security drift scan — checks for open dangerous ports."""
        try:
            sub_ids = _parse_sub_ids()
            drift = azure_data.detect_security_drift(subscription_ids=sub_ids)
            return jsonify({"drift_findings": drift, "count": len(drift), "subscription_ids": sub_ids})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/scan/compliance", methods=["GET"])
    def scan_compliance():
        """Azure Policy compliance scan — summary + non-compliant resources."""
        try:
            sub_ids = _parse_sub_ids()
            summary = {}
            non_compliant = []
            for sid in sub_ids:
                s = azure_data.get_policy_compliance_summary(sid)
                nc = azure_data.get_non_compliant_resources(sid)
                non_compliant.extend(nc)
                if not summary:
                    summary = s
                else:
                    for k in ("total_policies", "non_compliant_policies", "non_compliant_resources", "total_resources"):
                        summary[k] = summary.get(k, 0) + s.get(k, 0)
            if summary.get("total_resources", 0) > 0:
                summary["compliance_pct"] = round(
                    (1 - summary.get("non_compliant_resources", 0) / summary["total_resources"]) * 100, 1)
            return jsonify({
                "summary": summary,
                "non_compliant_resources": non_compliant,
                "count": len(non_compliant),
                "subscription_ids": sub_ids,
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/digest", methods=["GET"])
    def daily_digest():
        """Generate a daily 'top of mind' digest — what the crew found overnight."""
        try:
            sub_ids = _parse_sub_ids()
            # Gather all signals across selected subscriptions
            orphaned = azure_data.get_orphaned_disks(subscription_ids=sub_ids)
            drift = azure_data.detect_security_drift(subscription_ids=sub_ids)
            insecure = azure_data.detect_insecure_storage(subscription_ids=sub_ids)
            health_events = []
            resource_health = []
            for sid in sub_ids:
                health_events.extend(azure_data.get_service_health_events(sid, days=7))
                resource_health.extend(azure_data.get_resource_health_statuses(sid))
            tagging = azure_data.get_tagging_compliance(subscription_ids=sub_ids)

            degraded = [r for r in resource_health if r.get("status") in ("Degraded", "Unavailable")]
            active_incidents = [e for e in health_events if e.get("status") == "Active"]
            untagged = [t for t in tagging if not t.get("supportOwner")]

            digest_context = json.dumps({
                "date": "today",
                "orphaned_disks": len(orphaned),
                "security_drift": drift,
                "insecure_storage": insecure,
                "degraded_resources": degraded,
                "active_service_incidents": active_incidents,
                "untagged_resource_groups": [t["name"] for t in untagged],
                "tagging_compliance_pct": round(len([t for t in tagging if t.get("supportOwner")]) / len(tagging) * 100, 1) if tagging else 0,
            }, default=str)

            def generate():
                yield f"data: {json.dumps({'phase': 'round1', 'agent_key': 'scout', 'result': {'agent': 'Flare Stack', 'role': 'Overnight scan', 'model': 'digest', 'response': '🔥 **Daily Digest — scanning overnight findings...**', 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}})}\n\n"

                for agent_key in ["scout", "cost_sentinel", "standards_architect"]:
                    agent_cfg = settings.agents.get(agent_key)
                    if not agent_cfg:
                        continue
                    result = call_agent(agent_cfg, f"Generate a daily morning briefing. What should the ops team address TODAY based on this overnight scan data? Prioritize by risk and impact.\n\nOvernight scan results:\n{digest_context}")
                    yield f"data: {json.dumps({'phase': 'round1', 'agent_key': agent_key, 'result': result}, default=str)}\n\n"

                # Pipeline summary
                orchestrator_cfg = settings.agents["orchestrator"]
                summary = call_agent(orchestrator_cfg, f"Create a crisp morning briefing from the crew's overnight findings. Format as: TOP PRIORITY (1 item), WATCH LIST (2-3 items), ALL CLEAR (what's fine). Data:\n{digest_context}")
                yield f"data: {json.dumps({'phase': 'synthesis', 'agent_key': 'orchestrator', 'result': summary}, default=str)}\n\n"
                yield f"data: {json.dumps({'phase': 'done'})}\n\n"

            return Response(generate(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ─── ADO Integration (Phase 2) ─────────────────────────

    @app.route("/api/ado/proposals", methods=["GET"])
    def list_proposals():
        """List ADO proposals. Optional ?status=pending|approved|rejected|created"""
        status = request.args.get("status", "").strip() or None
        proposals = ado_integration.get_proposals(status=status)
        return jsonify({"proposals": proposals, "count": len(proposals)})

    @app.route("/api/ado/proposals", methods=["POST"])
    def create_proposal():
        """Create a proposal from an Inspector classification.

        Body: { "classifications": [ { class, policy_name, resource_id,
                support_owner, reasoning, title, description, ... } ] }
        """
        body = request.get_json(force=True)
        classifications = body.get("classifications", [])
        if not classifications:
            return jsonify({"error": "classifications array is required"}), 400

        proposals = ado_integration.generate_proposals_from_inspection(body)
        return jsonify({"proposals": proposals, "count": len(proposals)})

    @app.route("/api/ado/proposals/<proposal_id>", methods=["GET"])
    def get_proposal(proposal_id):
        """Get a single proposal by ID."""
        proposal = ado_integration.get_proposal(proposal_id)
        if not proposal:
            return jsonify({"error": f"Proposal {proposal_id} not found"}), 404
        return jsonify(proposal.to_dict())

    @app.route("/api/ado/proposals/<proposal_id>/approve", methods=["POST"])
    def approve_proposal(proposal_id):
        """Human approves a proposal → creates item in ADO.

        If ADO is configured: creates branch+PR (policy bugs) or work item (PBI/Bug/Task).
        If not configured: returns the payload showing what would be created.
        """
        body = request.get_json(force=True) if request.is_json else {}
        approved_by = body.get("approved_by", "ops-user")
        result = ado_integration.approve_proposal(proposal_id, approved_by=approved_by)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/ado/proposals/<proposal_id>/reject", methods=["POST"])
    def reject_proposal(proposal_id):
        """Human rejects a proposal with optional reason."""
        body = request.get_json(force=True) if request.is_json else {}
        reason = body.get("reason", "")
        result = ado_integration.reject_proposal(proposal_id, reason=reason)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route("/api/ado/inspect-and-propose", methods=["POST"])
    def inspect_and_propose():
        """Full Phase 2 pipeline: scan → classify → generate fix code → propose.

        This is the main Phase 2 workflow endpoint:
        1. Scans Azure Policy compliance for the subscription
        2. Sends findings to The Inspector for classification
        3. For policy bugs: calls The Roughneck to generate Terraform/policy fix code
        4. Creates proposals with fix code attached (for PRs) or work item details (for PBIs)
        5. Returns proposals for human review

        On human approval, the system will:
        - Policy bugs: create branch → push fix files → open PR in ADO
          (Terraform pipeline auto-runs plan, human reviews, merges → apply)
        - Workaround abuse: create PBI in ADO backlog
        - Misconfiguration: create Bug assigned to support owner
        - Exemptions: create Task to verify documentation

        Body (optional): { "subs": ["sub-id-1", ...] }
        """
        body = request.get_json(force=True) if request.is_json else {}
        subs = body.get("subs")

        try:
            sub_ids = subs if subs else [settings.subscription_id]

            # Step 1: Get compliance data
            non_compliant = []
            for sid in sub_ids:
                nc = azure_data.get_non_compliant_resources(sid)
                non_compliant.extend(nc)

            if not non_compliant:
                return jsonify({
                    "message": "No non-compliant resources found. The estate is clean.",
                    "proposals": [],
                    "count": 0,
                })

            # Step 2: Send to The Inspector for classification
            compliance_context = json.dumps(non_compliant[:30], indent=2, default=str)

            inspector_prompt = f"""Analyze these non-compliant Azure resources and classify each violation.

For EACH non-compliant resource, output a JSON block with:
- "class": one of "policy_bug", "misconfiguration", "intentional_exemption", "workaround_abuse"
- "policy_name": the policy that flagged it
- "resource_id": the resource ID
- "support_owner": from tags if available, otherwise "unassigned"
- "reasoning": 1-2 sentences explaining your classification
- "title": a clear work item title
- "description": detailed description for the ADO work item
- "acceptance_criteria": what "done" looks like
- "priority": 1-4 (1=Critical)

Wrap your output in ```json ... ``` with an array of classification objects.

NON-COMPLIANT RESOURCES:
{compliance_context}"""

            inspector_cfg = settings.agents["compliance_inspector"]
            inspector_result = call_agent(inspector_cfg, inspector_prompt)

            # Step 3: Parse Inspector output into classifications
            response_text = inspector_result.get("response", "")
            classifications = _parse_inspector_classifications(response_text)

            # Step 4: For policy bugs, call The Roughneck to generate fix code
            roughneck_cfg = settings.agents["standards_architect"]
            for c in classifications:
                if c.get("class") == "policy_bug":
                    remediation_prompt = (
                        f"Generate Terraform/policy-as-code to fix this policy definition bug.\n\n"
                        f"Policy: {c.get('policy_name', 'unknown')}\n"
                        f"Resource: {c.get('resource_id', 'unknown')}\n"
                        f"Problem: {c.get('reasoning', 'unknown')}\n"
                        f"Fix needed: {c.get('description', 'unknown')}\n\n"
                        f"Generate:\n"
                        f"1. **main.tf** — Terraform to deploy the corrected policy definition\n"
                        f"2. **variables.tf** — Variable declarations with defaults\n"
                        f"3. **RUNBOOK.md** — Validation steps after apply\n\n"
                        f"Follow organizational standards: tags (support-owner, managed-by=ops-council), "
                        f"azurerm provider with version constraint."
                    )
                    fix_result = call_agent(roughneck_cfg, remediation_prompt)
                    fix_text = fix_result.get("response", "")
                    c["file_changes"] = ado_integration.parse_remediation_to_file_changes(
                        fix_text, c.get("policy_name", "")
                    )
                    c["remediation_raw"] = fix_text

            # Step 5: Create proposals
            proposals = []
            for c in classifications:
                proposal = ado_integration.create_proposal(
                    violation_class=c.get("class", "misconfiguration"),
                    policy_name=c.get("policy_name", ""),
                    resource_id=c.get("resource_id", ""),
                    support_owner=c.get("support_owner", "unassigned"),
                    inspector_reasoning=c.get("reasoning", ""),
                    title=c.get("title", "Ops Council Finding"),
                    description=c.get("description", ""),
                    acceptance_criteria=c.get("acceptance_criteria", ""),
                    priority=c.get("priority", 2),
                    pr_file_changes=c.get("file_changes", []),
                )
                proposals.append(proposal.to_dict())

            return jsonify({
                "inspector_analysis": inspector_result,
                "proposals": proposals,
                "count": len(proposals),
                "non_compliant_scanned": len(non_compliant),
                "policy_bugs_with_fix_code": sum(1 for c in classifications if c.get("class") == "policy_bug"),
                "message": f"Inspector classified {len(classifications)} violations. {len(proposals)} proposals pending human review.",
            })

        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    def _parse_inspector_classifications(response_text: str) -> list[dict]:
        """Extract JSON classifications from Inspector's response text."""
        import re
        # Look for ```json ... ``` block
        match = re.search(r'```json\s*\n(.*?)\n\s*```', response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Fallback: try to find a JSON array anywhere in the response
        match = re.search(r'\[.*\]', response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        return []

    # ─── Helpers ────────────────────────────────────────────

    def _parse_sub_ids() -> list[str]:
        """Parse subscription IDs from ?subs= query param."""
        subs_param = request.args.get("subs", "").strip()
        if subs_param == "all":
            all_subs = azure_data.list_subscriptions()
            return [s["id"] for s in all_subs if s.get("state") == "Enabled"]
        elif subs_param:
            return [s.strip() for s in subs_param.split(",") if s.strip()]
        else:
            return [settings.subscription_id]

    def _gather_live_context() -> str:
        """Gather real Azure data to use as agent context."""
        sub_ids = _parse_sub_ids()
        scan = _gather_live_scan(sub_ids)
        return json.dumps(scan, indent=2, default=str)

    def _gather_live_scan(sub_ids: list[str] = None) -> dict:
        """Full scan of Azure subscriptions."""
        if not sub_ids:
            sub_ids = [settings.subscription_id]

        resources = azure_data.get_all_resources(subscription_ids=sub_ids)
        tagging = azure_data.get_tagging_compliance(subscription_ids=sub_ids)
        orphaned = azure_data.get_orphaned_disks(subscription_ids=sub_ids)
        public_ips = azure_data.get_public_endpoints(subscription_ids=sub_ids)

        rg_count = len(set(r.get("resourceGroup", "") for r in resources))
        tagged = sum(1 for t in tagging if t.get("supportOwner"))
        total_rgs = len(tagging)

        health = []
        try:
            health = azure_data.get_resource_health(subscription_ids=sub_ids)
        except Exception:
            pass

        activity_errors = []
        try:
            activity_errors = azure_data.get_recent_activity_errors(hours=24)
        except Exception:
            pass

        return {
            "subscription_ids": sub_ids,
            "subscription_count": len(sub_ids),
            "scan_type": "live",
            "resources": {
                "total": len(resources),
                "resource_groups": rg_count,
                "by_type": _count_by(resources, "type"),
                "by_rg": _count_by(resources, "resourceGroup"),
                "details": resources[:100],  # cap to avoid token overload
            },
            "tagging_compliance": {
                "total_rgs": total_rgs,
                "with_support_owner": tagged,
                "compliance_pct": round(tagged / total_rgs * 100, 1) if total_rgs else 0,
                "non_compliant_rgs": [t["name"] for t in tagging if not t.get("supportOwner")],
            },
            "orphaned_disks": orphaned,
            "public_endpoints": public_ips,
            "resource_health": health[:50],
            "recent_failures": activity_errors[:20],
        }

    def _count_by(items: list[dict], key: str) -> dict:
        counts = {}
        for item in items:
            val = item.get(key, "unknown")
            counts[val] = counts.get(val, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))

    return app
