"""OGE Ops Council — Flask application."""

from flask import Flask, render_template, request, jsonify, Response
import json
import traceback
from app.agents.runner import run_council, call_agent
from app.agents.demos import DEMO_SCENARIOS
from app.config import settings


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
                "agents": list[str] (optional) }
        """
        body = request.get_json(force=True)
        question = body.get("question", "").strip()
        if not question:
            return jsonify({"error": "question is required"}), 400

        context_data = body.get("context_data", "")
        agents = body.get("agents")

        try:
            result = run_council(question, context_data, agents)
            return jsonify(result)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

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
            return jsonify(result)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "healthy",
            "agents": list(settings.agents.keys()),
            "openai_endpoint": settings.openai_endpoint,
        })

    return app
