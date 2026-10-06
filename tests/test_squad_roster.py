#!/usr/bin/env python3
"""Validate the repository-local Build Squad roster and routing metadata."""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SQUAD_ROOT = REPO_ROOT / ".squad"

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def roster_entries():
    entries = []
    for line in (SQUAD_ROOT / "team.md").read_text().splitlines():
        if not line.startswith("|") or "`" not in line:
            continue
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) < 4 or columns[0] == "Name":
            continue
        match = re.search(r"`([^`]+)`", columns[2])
        if match:
            entries.append(
                {
                    "name": columns[0],
                    "role": columns[1],
                    "charter": match.group(1),
                    "status": columns[3],
                }
            )
    return entries


EXPECTED_NEW_AGENTS = {
    "zeroops-sre-engineer": "ZeroOps SRE Engineer",
    "security-identity-engineer": "Security & Identity Engineer",
    "state-workflow-engineer": "State & Workflow Engineer",
    "platform-release-engineer": "Platform Release Engineer",
    "operations-ux-engineer": "Operations UX Engineer",
}
BUILT_INS = {"Scribe", "Ralph", "Rai", "Fact Checker"}


print("\n🧪 Test 1: every roster member has readable metadata")
entries = roster_entries()
test("team roster is non-empty", bool(entries))
for entry in entries:
    charter = REPO_ROOT / entry["charter"]
    history = charter.with_name("history.md")
    test(f"{entry['name']}: charter exists", charter.is_file())
    test(f"{entry['name']}: history exists", history.is_file())
    if charter.is_file() and entry["name"] not in BUILT_INS:
        text = charter.read_text()
        test(f"{entry['name']}: charter defines ownership", "## What I Own" in text)
        test(f"{entry['name']}: charter defines boundaries", "## Boundaries" in text)
        test(f"{entry['name']}: charter defines model guidance", "## Model" in text)
    test(f"{entry['name']}: active", entry["status"] == "active")

print("\n🧪 Test 2: new specialists are cast and routed")
registry = json.loads((SQUAD_ROOT / "casting" / "registry.json").read_text())
agents = registry.get("agents", {})
routing = (SQUAD_ROOT / "routing.md").read_text()
for key, name in EXPECTED_NEW_AGENTS.items():
    test(f"{name}: casting entry exists", key in agents)
    test(f"{name}: casting entry active", (agents.get(key) or {}).get("status") == "active")
    test(f"{name}: routing entry exists", name in routing)

print("\n🧪 Test 3: independent evaluation policy is wired")
tester = (SQUAD_ROOT / "agents" / "tester" / "charter.md").read_text()
test(
    "Tester owns adversarial evaluation",
    "Agent Evaluation & Adversarial QA Engineer" in tester,
)
test(
    "routing requires explicit per-task model selection",
    "MUST choose the model per task" in routing,
)
test(
    "routing requires different author/reviewer families",
    "different model family" in routing,
)
test(
    "repo-local loading boundary is documented",
    "loads it only when started from OGEAgenticITOperations"
    in (SQUAD_ROOT / "team.md").read_text(),
)

print(f"\n{'✅' if FAIL == 0 else '❌'} {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
