"""Agent catalog -- the single, code-derived answer to "what does each
agent do, and how does it do it?".

Display name, one-line role and model deployment come from the active
profile (profiles/<id>/profile.json). Everything else is DERIVED from
the code that actually drives the agent, so the catalog cannot drift
from runtime behavior:

  * handles       -- inverse of app.agents.routing.CATEGORY_AGENT_MAP
                     (the deterministic category -> specialist routing).
  * evidence      -- the Azure collectors that produce those categories
                     (app/operations/collectors/*).
  * tools         -- app.agents.tools.TOOLS (read-only, bounded).
  * foundry agent -- app.agents.backend.foundry_agent_name(...), the
                     exact name published to Azure AI Foundry when
                     AGENT_BACKEND=foundry.

Served as JSON at GET /api/agents and rendered on the "Agents" page;
docs/AGENTS.md is the human-readable companion.
"""

from app.agents import routing as routing_module
from app.agents import tools as tools_module
from app.operations.models import FindingCategory

__all__ = ["CATEGORY_LABELS", "CATEGORY_EVIDENCE", "AGENT_FUNCTIONS", "build_agent_catalog"]

ANALYSIS_SCHEMA_NAME = "agent_analysis_result"

CATEGORY_LABELS = {
    FindingCategory.COST.value: "Cost & budgets",
    FindingCategory.CAPACITY.value: "Capacity & quota",
    FindingCategory.INCIDENT.value: "Incidents & alerts",
    FindingCategory.RELIABILITY.value: "Reliability & SLOs",
    FindingCategory.CHANGE.value: "Recent changes",
    FindingCategory.SECURITY.value: "Security posture",
    FindingCategory.TELEMETRY.value: "Monitoring coverage",
    FindingCategory.COMPLIANCE.value: "Policy compliance & retirements",
    FindingCategory.OWNERSHIP.value: "Ownership & tagging",
    FindingCategory.BACKUP.value: "Backup health",
    FindingCategory.PATCH.value: "Patching",
    FindingCategory.CERTIFICATE.value: "Certificate & secret expiry",
    FindingCategory.AUTOMATION.value: "Automation jobs",
}

# Azure data sources (read-only) behind each category -- mirrors
# app/operations/collectors/*.py; see docs/AZURE_DATA_SOURCES.md.
CATEGORY_EVIDENCE = {
    FindingCategory.COST.value: ["Azure Cost Management (budgets, period-over-period trend)", "Azure Advisor cost recommendations"],
    FindingCategory.CAPACITY.value: ["Compute & Azure OpenAI regional quota usage (ARM)"],
    FindingCategory.INCIDENT.value: ["Azure Monitor fired alerts", "Resource Health"],
    FindingCategory.RELIABILITY.value: ["Workload SLOs over Log Analytics / App Insights", "Resource Health"],
    FindingCategory.CHANGE.value: ["Activity Log changes correlated with health degradation"],
    FindingCategory.SECURITY.value: ["Microsoft Defender for Cloud alerts & assessments"],
    FindingCategory.TELEMETRY.value: ["Diagnostic-settings coverage", "Log Analytics heartbeat gaps"],
    FindingCategory.COMPLIANCE.value: ["Azure Policy compliance state", "Defender regulatory assessments", "Service Health retirement advisories"],
    FindingCategory.OWNERSHIP.value: ["Resource Graph tag/owner inventory"],
    FindingCategory.BACKUP.value: ["Azure Backup jobs & protected-item health (Log Analytics)"],
    FindingCategory.PATCH.value: ["Azure Update Manager patch assessments (Resource Graph)"],
    FindingCategory.CERTIFICATE.value: ["Key Vault certificate / secret / key expiry"],
    FindingCategory.AUTOMATION.value: ["Azure Automation failed / suspended jobs"],
}

# Profile-independent functional description of each agent key. The
# profile chooses the display name; this says what the agent is FOR.
AGENT_FUNCTIONS = {
    routing_module.COORDINATOR_KEY: {
        "icon": "⚡",
        "function": "Operations Coordinator",
        "what": "Turns the specialists' separate analyses into one prioritized, evidence-cited recommendation.",
        "how": [
            "Deterministic routing (code, not the model) picks which specialists see each request.",
            "Single-domain, low-severity requests go straight to one specialist; cross-domain, high/critical or customer-impacting evidence triggers a debate.",
            "In a debate, specialists run in parallel, then each rebuts the others' conclusions.",
            "The coordinator reconciles agreements and disagreements into a final answer that must cite finding IDs.",
        ],
        "outputs": ["Unified recommendation", "Points of agreement / dissent", "Cited finding IDs", "Token & cost usage summary"],
    },
    "cost_sentinel": {
        "icon": "💰",
        "function": "Cost & Capacity Analyst",
        "what": "Explains where spend and quota pressure come from and what to rightsize, defer or reserve.",
        "how": [
            "Reads budget state, cost trend, orphaned-resource and Advisor cost signals, plus regional compute / Azure OpenAI quota usage.",
            "Can call get_capacity_watch for quota line items in warning/critical state.",
            "Shows the math (current vs. threshold, period-over-period delta) behind every recommendation.",
        ],
        "outputs": ["Rightsizing / cleanup candidates", "Budget & quota risk", "Estimated impact"],
    },
    "diagnostics_sre": {
        "icon": "🔍",
        "function": "Incident & Change Investigator",
        "what": "Finds the most likely root cause of an incident or reliability regression and the safest next step.",
        "how": [
            "Correlates fired alerts, Resource Health and SLO breaches with Activity Log changes in the same window.",
            "Can call get_recent_changes and get_finding_evidence to build a timeline: symptom -> change -> impact.",
            "Proposes remediation as a reviewable action; it never executes changes itself.",
        ],
        "outputs": ["Incident timeline", "Probable root cause + confidence", "Remediation proposal"],
    },
    "scout": {
        "icon": "🛡️",
        "function": "Security & Monitoring Analyst",
        "what": "Surfaces security risks and blind spots where the estate isn't being monitored.",
        "how": [
            "Reads Defender for Cloud alerts and unhealthy assessments.",
            "Checks diagnostic-settings coverage and Log Analytics heartbeat gaps.",
            "Can call get_source_coverage so it reports what it could NOT see, not just what it found.",
        ],
        "outputs": ["Prioritized security findings", "Monitoring coverage gaps", "Evidence-source gaps"],
    },
    "compliance_inspector": {
        "icon": "📋",
        "function": "Policy & Governance Advisor",
        "what": "Classifies policy violations and ownership gaps and routes each to the right fix.",
        "how": [
            "Reads Azure Policy compliance, Defender regulatory assessments, Service Health retirement notices and tag/owner inventory.",
            "Classifies each violation as policy bug, misconfiguration, documented exemption, or workaround.",
            "Recommends the fix path (policy PR, remediation work item, or exemption review) for human approval.",
        ],
        "outputs": ["Violation classification", "Fix path per violation", "Retirement deadlines"],
    },
    "standards_architect": {
        "icon": "🔧",
        "function": "Resilience & Hygiene Engineer",
        "what": "Keeps operational hygiene green: backups, patching, certificates and automation.",
        "how": [
            "Reads Azure Backup job health, Update Manager patch assessments, Key Vault expiry and Automation job failures.",
            "Checks each against the organization's standards and explains what a change would break.",
            "Prioritizes by deadline (expiry, retirement) and blast radius.",
            "On request, drafts remediation artifacts (Terraform, Azure CLI script, runbook) for human review through the normal PR process.",
        ],
        "outputs": ["Hygiene gaps by deadline", "Standards deviations", "Remediation artifacts (Terraform / CLI / runbook)"],
    },
}

GUARDRAILS = [
    "Read-only: agents query Azure evidence but never change resources.",
    "Evidence-grounded: every claim must cite a finding ID; citations are validated.",
    "Schema-validated structured output; invalid output is reported, not trusted.",
    "Remediation is a proposal that requires human approval (Azure DevOps work item / PR).",
]


def _categories_by_agent() -> dict:
    by_agent: dict = {}
    for category, agent_key in routing_module.CATEGORY_AGENT_MAP.items():
        by_agent.setdefault(agent_key, []).append(category)
    return by_agent


def build_agent_catalog(agents: dict, *, backend: str = "direct", foundry_agent_prefix: str = "") -> list:
    """Return one dict per configured agent, coordinator first.

    `agents` is Settings.agents ({key: AgentConfig}). `backend` and
    `foundry_agent_prefix` describe where the agent actually runs."""
    from app.agents.backend import foundry_agent_name

    by_agent = _categories_by_agent()
    tool_names = list(tools_module.TOOLS)
    order = [routing_module.COORDINATOR_KEY] + [k for k in AGENT_FUNCTIONS if k != routing_module.COORDINATOR_KEY]
    order += [k for k in agents if k not in order]

    catalog = []
    for key in order:
        cfg = agents.get(key)
        if cfg is None:
            continue
        fn = AGENT_FUNCTIONS.get(key, {})
        categories = by_agent.get(key, [])
        evidence: list = []
        for category in categories:
            for source in CATEGORY_EVIDENCE.get(category, []):
                if source not in evidence:
                    evidence.append(source)
        is_coordinator = key == routing_module.COORDINATOR_KEY
        catalog.append({
            "key": key,
            "name": cfg.name,
            "function": fn.get("function", cfg.name),
            "icon": fn.get("icon", "🤖"),
            "role": cfg.role,
            "what": fn.get("what", cfg.role),
            "how": list(fn.get("how", [])),
            "outputs": list(fn.get("outputs", [])),
            "handles": [{"category": c, "label": CATEGORY_LABELS.get(c, c)} for c in categories],
            "evidence_sources": evidence if not is_coordinator else ["All specialists' cited analyses"],
            "tools": tool_names,
            "model_deployment": cfg.deployment,
            "prompt_version": getattr(cfg, "prompt_version", ""),
            "runtime": {
                "backend": backend,
                "foundry_agent": foundry_agent_name(foundry_agent_prefix, key, ANALYSIS_SCHEMA_NAME)
                if backend == "foundry" and foundry_agent_prefix else "",
            },
            "guardrails": list(GUARDRAILS),
        })
    return catalog
