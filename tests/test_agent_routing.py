#!/usr/bin/env python3
"""Test selective routing/debate policy (app/agents/routing.py).

Run: python3 tests/test_agent_routing.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.agents.evidence import EvidenceBundle, EvidenceBundleItem  # noqa: E402
from app.agents.routing import CATEGORY_AGENT_MAP, COORDINATOR_KEY, route  # noqa: E402
from app.operations.models import FindingCategory  # noqa: E402

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2705 {name}")
    else:
        FAIL += 1
        print(f"  \u274c {name}")


def make_item(category, *, severity="low", customer_impact=False, item_id="x"):
    return EvidenceBundleItem(
        id=item_id, category=category, severity=severity, title="t", summary="s", business_impact="b",
        owner="", recommended_action="", confidence="derived", approval_required=False, priority_band="P4",
        customer_impact=customer_impact, workflow_status="new", evidence=(),
    )


def make_bundle(items):
    return EvidenceBundle(
        items=tuple(items), total_available=len(items), truncated=False,
        generated_at="x", snapshot_id="s", subscription_count=1, coverage={},
    )


print("\n\U0001f9ea Test 1: CATEGORY_AGENT_MAP covers every FindingCategory")
test("every category has a mapped specialist", set(CATEGORY_AGENT_MAP) == {m.value for m in FindingCategory})


print("\n\U0001f9ea Test 2: routine single-domain, single finding -> ONE specialist, no coordinator, no debate")
bundle1 = make_bundle([make_item(FindingCategory.COST.value, item_id="a")])
decision1 = route(bundle1)
test("exactly one specialist selected", decision1.specialist_agents == (CATEGORY_AGENT_MAP[FindingCategory.COST.value],))
test("no coordinator (nothing to synthesize)", decision1.coordinator_included is False)
test("no debate", decision1.debate is False)


print("\n\U0001f9ea Test 3: routine single-domain, MULTIPLE findings -> coordinator joins to synthesize, still no debate")
bundle2 = make_bundle([make_item(FindingCategory.COST.value, item_id="a"), make_item(FindingCategory.COST.value, item_id="b")])
decision2 = route(bundle2)
test("still one specialist", len(decision2.specialist_agents) == 1)
test("coordinator now included (synthesis needed)", decision2.coordinator_included is True)
test("still no debate (single domain)", decision2.debate is False)


print("\n\U0001f9ea Test 4: cross-domain evidence ALWAYS triggers debate + coordinator")
bundle3 = make_bundle([make_item(FindingCategory.COST.value, item_id="a"), make_item(FindingCategory.SECURITY.value, item_id="b")])
decision3 = route(bundle3)
test("both specialists selected", set(decision3.specialist_agents) == {CATEGORY_AGENT_MAP[FindingCategory.COST.value], CATEGORY_AGENT_MAP[FindingCategory.SECURITY.value]})
test("debate is True", decision3.debate is True)
test("coordinator included", decision3.coordinator_included is True)
test("factors explain why (cross_domain)", decision3.factors["cross_domain"] is True)


print("\n\U0001f9ea Test 5: high/critical severity ALWAYS triggers debate, even single-domain")
bundle4 = make_bundle([make_item(FindingCategory.COST.value, severity="critical", item_id="a")])
decision4 = route(bundle4)
test("critical severity triggers debate", decision4.debate is True)
test("factors explain why (high_or_critical)", decision4.factors["high_or_critical"] is True)


print("\n\U0001f9ea Test 6: customer-impacting finding ALWAYS triggers debate, even single-domain/low severity")
bundle5 = make_bundle([make_item(FindingCategory.COST.value, customer_impact=True, item_id="a")])
decision5 = route(bundle5)
test("customer impact triggers debate", decision5.debate is True)
test("factors explain why (customer_impact)", decision5.factors["customer_impact"] is True)


print("\n\U0001f9ea Test 7: ambiguous (3+ specialist domains) triggers debate")
bundle6 = make_bundle([
    make_item(FindingCategory.COST.value, item_id="a"),
    make_item(FindingCategory.SECURITY.value, item_id="b"),
    make_item(FindingCategory.COMPLIANCE.value, item_id="c"),
])
decision6 = route(bundle6)
test("3+ domains flagged ambiguous", decision6.factors["ambiguous"] is True)
test("debate is True", decision6.debate is True)


print("\n\U0001f9ea Test 8: explicit request always honored (agents + debate)")
decision7 = route(bundle1, requested_agents=["scout"])
test("single explicitly-requested agent -> no debate, no coordinator", decision7.specialist_agents == ("scout",) and decision7.debate is False and decision7.coordinator_included is False)

decision8 = route(bundle1, requested_agents=["scout", "cost_sentinel"])
test("2+ explicitly-requested agents -> debate + coordinator", decision8.debate is True and decision8.coordinator_included is True)

decision9 = route(bundle1, force_debate=True)
test("force_debate=True overrides routine routing", decision9.debate is True and decision9.coordinator_included is True)
test("force_debate factors record the explicit reason", "debate=true" in decision9.factors["reason"])


print("\n\U0001f9ea Test 9: route() requires a non-empty bundle")
empty_bundle = make_bundle([])
try:
    route(empty_bundle)
    test("route() raises on an empty bundle rather than guessing", False)
except ValueError:
    test("route() raises on an empty bundle rather than guessing", True)


print("\n\U0001f9ea Test 10: coordinator key is never itself a mapped specialist")
test("orchestrator never appears as a CATEGORY_AGENT_MAP value", COORDINATOR_KEY not in CATEGORY_AGENT_MAP.values())


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
