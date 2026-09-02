"""Selective agent routing + debate policy.

Deterministic (never model-decided): which specialist(s) an evidence
bundle maps to, and whether the request needs multi-agent debate versus
one specialist (plus the coordinator only when synthesis is needed).
See docs/AGENT_INTELLIGENCE.md for the full policy write-up and
rationale table.

Debate (multiple specialists, a rebuttal round, and a coordinator
synthesis -- see app/agents/analysis.py) is triggered by any of:
  * cross_domain       -- the evidence spans 2+ specialist domains.
  * high_or_critical    -- any matched finding is high/critical severity.
  * customer_impact     -- any matched finding is customer-impacting
                           (app.operations.priority.is_customer_impacting,
                           already computed per-finding in the bundle).
  * ambiguous           -- the evidence spans 3+ specialist domains (no
                           single clear owner).
  * explicit_request    -- the caller passed >1 agent key or `debate=true`.

Otherwise it's routine: exactly one mapped specialist, plus the
coordinator ONLY if there's more than one finding to synthesize within
that single domain.
"""

from dataclasses import dataclass, field

from app.agents.evidence import EvidenceBundle
from app.operations.models import FindingCategory

__all__ = ["COORDINATOR_KEY", "CATEGORY_AGENT_MAP", "RoutingDecision", "route"]

COORDINATOR_KEY = "orchestrator"

# Every FindingCategory maps to exactly one specialist -- see
# profiles/power/profile.json for each agent's role/model. Rationale:
#   cost_sentinel        -- "Finds waste, tracks burn rate, recommends
#                            rightsizing" -> cost AND capacity (quota/
#                            headroom is a capacity-planning concern the
#                            same specialist already owns).
#   diagnostics_sre       -- "Root-cause analysis ... timeline, symptoms,
#                            root cause, remediation" -> incidents,
#                            reliability/SLO breaches, and changes (a
#                            change is the leading root-cause hypothesis
#                            for a health event).
#   scout                 -- "Continuously scans for anomalies, health
#                            degradation, and security drift" -> security
#                            alerts and telemetry coverage gaps (both are
#                            monitoring-hygiene signals scout already owns).
#   compliance_inspector  -- "Classifies Azure Policy violations ...
#                            recommends the right fix" -> compliance and
#                            resource-ownership/tagging gaps (a
#                            governance concern, not a reliability one).
#   standards_architect   -- "Validates changes against ... standards,
#                            flags what a change would break" -> backup,
#                            patch, certificate, and automation hygiene
#                            (all "is this configured to standard?"
#                            questions).
CATEGORY_AGENT_MAP = {
    FindingCategory.COST.value: "cost_sentinel",
    FindingCategory.CAPACITY.value: "cost_sentinel",
    FindingCategory.INCIDENT.value: "diagnostics_sre",
    FindingCategory.RELIABILITY.value: "diagnostics_sre",
    FindingCategory.CHANGE.value: "diagnostics_sre",
    FindingCategory.SECURITY.value: "scout",
    FindingCategory.TELEMETRY.value: "scout",
    FindingCategory.COMPLIANCE.value: "compliance_inspector",
    FindingCategory.OWNERSHIP.value: "compliance_inspector",
    FindingCategory.BACKUP.value: "standards_architect",
    FindingCategory.PATCH.value: "standards_architect",
    FindingCategory.CERTIFICATE.value: "standards_architect",
    FindingCategory.AUTOMATION.value: "standards_architect",
}
# Every FindingCategory value must have a mapped specialist -- guarded at
# import time so adding a new category without updating this map fails
# loudly at startup, not silently at request time.
_missing = set(member.value for member in FindingCategory) - set(CATEGORY_AGENT_MAP)
if _missing:
    raise RuntimeError(f"app.agents.routing.CATEGORY_AGENT_MAP is missing categor{'y' if len(_missing)==1 else 'ies'}: {sorted(_missing)}")

_HIGH_SEVERITY = {"critical", "high"}
# 3+ distinct specialist domains in one request -- no single clear owner.
_AMBIGUOUS_DOMAIN_COUNT = 3


@dataclass(frozen=True)
class RoutingDecision:
    specialist_agents: tuple
    coordinator_included: bool
    debate: bool
    factors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "specialist_agents": list(self.specialist_agents),
            "coordinator_included": self.coordinator_included,
            "debate": self.debate,
            "factors": self.factors,
        }


def _mapped_agents(bundle: EvidenceBundle) -> tuple:
    """Ordered, de-duplicated specialist keys for every category present
    in `bundle.items` (order = first appearance, which is priority order
    since bundle items are already priority-sorted)."""
    seen = []
    for item in bundle.items:
        agent_key = CATEGORY_AGENT_MAP[item.category]
        if agent_key not in seen:
            seen.append(agent_key)
    return tuple(seen)


def route(
    bundle: EvidenceBundle,
    *,
    requested_agents: list = None,
    force_debate: bool = False,
) -> RoutingDecision:
    """Decide which specialist(s) to consult and whether to run the
    debate (rebuttal + coordinator-synthesis) flow -- see module
    docstring. `bundle` must be non-empty (callers with zero matched
    evidence should short-circuit before calling this -- see
    app/agents/analysis.py's insufficient-evidence path)."""
    if not bundle.items:
        raise ValueError("route() requires a non-empty evidence bundle")

    mapped_agents = _mapped_agents(bundle)
    severities = {item.severity for item in bundle.items}
    customer_impact_any = any(item.customer_impact for item in bundle.items)
    cross_domain = len(mapped_agents) > 1
    high_or_critical = bool(severities & _HIGH_SEVERITY)
    ambiguous = len(mapped_agents) >= _AMBIGUOUS_DOMAIN_COUNT

    if requested_agents:
        specialists = tuple(dict.fromkeys(requested_agents))
        debate = force_debate or len(specialists) > 1
        factors = {
            "explicit_request": True, "cross_domain": cross_domain, "high_or_critical": high_or_critical,
            "customer_impact": customer_impact_any, "ambiguous": ambiguous,
            "matched_categories": sorted({item.category for item in bundle.items}),
            "reason": "caller explicitly requested these agent(s)",
        }
        return RoutingDecision(
            specialist_agents=specialists, coordinator_included=debate, debate=debate, factors=factors,
        )

    if force_debate:
        specialists = mapped_agents
        factors = {
            "explicit_request": False, "cross_domain": cross_domain, "high_or_critical": high_or_critical,
            "customer_impact": customer_impact_any, "ambiguous": ambiguous,
            "matched_categories": sorted({item.category for item in bundle.items}),
            "reason": "caller explicitly requested debate=true",
        }
        return RoutingDecision(
            specialist_agents=specialists, coordinator_included=True, debate=True, factors=factors,
        )

    debate = cross_domain or high_or_critical or customer_impact_any or ambiguous
    matched_categories = sorted({item.category for item in bundle.items})

    if debate:
        reason_parts = []
        if cross_domain:
            reason_parts.append(f"evidence spans {len(mapped_agents)} specialist domains")
        if high_or_critical:
            reason_parts.append("high/critical severity present")
        if customer_impact_any:
            reason_parts.append("customer-impacting finding present")
        if ambiguous:
            reason_parts.append("no single clear domain owner")
        factors = {
            "explicit_request": False, "cross_domain": cross_domain, "high_or_critical": high_or_critical,
            "customer_impact": customer_impact_any, "ambiguous": ambiguous,
            "matched_categories": matched_categories, "reason": "; ".join(reason_parts),
        }
        return RoutingDecision(
            specialist_agents=mapped_agents, coordinator_included=True, debate=True, factors=factors,
        )

    # Routine: exactly one mapped specialist (cross_domain is False, so
    # len(mapped_agents) == 1 by construction). Coordinator only joins
    # if there's more than one finding to synthesize within this single
    # domain -- a single finding's own structured answer IS the final
    # answer.
    synthesis_needed = len(bundle.items) > 1
    factors = {
        "explicit_request": False, "cross_domain": False, "high_or_critical": False,
        "customer_impact": False, "ambiguous": False, "matched_categories": matched_categories,
        "synthesis_needed": synthesis_needed,
        "reason": (
            f"single domain ({matched_categories[0]}), low severity, no customer impact"
            + (" -- coordinator added to synthesize multiple findings" if synthesis_needed else "")
        ),
    }
    return RoutingDecision(
        specialist_agents=mapped_agents, coordinator_included=synthesis_needed, debate=False, factors=factors,
    )
