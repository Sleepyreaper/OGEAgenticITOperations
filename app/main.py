"""OGE Ops Council — Flask application."""

from flask import Flask, render_template, request, jsonify, Response
import json
import traceback
from app.agents.runner import run_council, run_council_streaming, call_agent
from app.agents.demos import DEMO_SCENARIOS
from app.config import settings
from app import azure_data


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

        # In live mode, gather real Azure data as context
        if mode == "live" and not context_data:
            try:
                context_data = _gather_live_context()
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

        if mode == "live" and not context_data:
            try:
                context_data = _gather_live_context()
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

        remediation_prompt = f"""Based on this operational analysis, generate the remediation code.

{context}

Generate TWO things:
1. **Terraform code** to fix this issue following OGE standards (modular, tagged, least-privilege)
2. **Azure CLI commands** as a quick alternative for immediate action

Format each in a proper code block. Include comments explaining what each section does.
Keep it production-ready — no placeholders except for subscription/resource group IDs which should use variables.
If the analysis recommends NOT making changes (e.g., The Roughneck defended the current config), say so and explain why no remediation is needed.
Be concise — working code, not an essay."""

        def generate():
            try:
                # Use The Roughneck (gpt-4.1) for remediation — he knows the standards
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

    @app.route("/api/scan/overview", methods=["GET"])
    def scan_overview():
        """Scan the current subscription for dashboard KPI cards."""
        try:
            sub_id = settings.subscription_id
            resources = azure_data.get_all_resources(subscription_id=sub_id)
            tagging = azure_data.get_tagging_compliance(subscription_id=sub_id)
            orphaned = azure_data.get_orphaned_disks(subscription_id=sub_id)
            public_ips = azure_data.get_public_endpoints(subscription_id=sub_id)

            rg_count = len(set(r.get("resourceGroup", "") for r in resources))
            tagged = sum(1 for t in tagging if t.get("supportOwner"))
            total_rgs = len(tagging)
            findings = len(orphaned) + len(public_ips) + len([t for t in tagging if not t.get("supportOwner")])

            # Azure Advisor — platform-verified recommendations (the evidence)
            advisor_recs = []
            try:
                advisor_recs = azure_data.get_advisor_recommendations(sub_id)
            except Exception:
                pass

            advisor_by_category = {}
            for r in advisor_recs:
                cat = r.get("category", "Unknown")
                advisor_by_category[cat] = advisor_by_category.get(cat, 0) + 1

            return jsonify({
                "subscription_id": sub_id,
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

    # ─── Helpers ────────────────────────────────────────────

    def _gather_live_context() -> str:
        """Gather real Azure data to use as agent context."""
        scan = _gather_live_scan()
        return json.dumps(scan, indent=2, default=str)

    def _gather_live_scan() -> dict:
        """Full scan of the Azure subscription."""
        resources = azure_data.get_all_resources()
        tagging = azure_data.get_tagging_compliance()
        orphaned = azure_data.get_orphaned_disks()
        public_ips = azure_data.get_public_endpoints()

        rg_count = len(set(r.get("resourceGroup", "") for r in resources))
        tagged = sum(1 for t in tagging if t.get("supportOwner"))
        total_rgs = len(tagging)

        health = []
        try:
            health = azure_data.get_resource_health()
        except Exception:
            pass

        activity_errors = []
        try:
            activity_errors = azure_data.get_recent_activity_errors(hours=24)
        except Exception:
            pass

        return {
            "subscription_id": settings.subscription_id,
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
