"""Shared helper: locate one Finding within an already-built
OperationsSnapshot and shape its bounded evidence-only view.

Extracted from app/operations/routes.py's GET /api/operations/evidence/
<finding_id> so app/agents/tools.py's get_finding_evidence tool can
return the EXACT same bounded shape without duplicating the search/
shaping logic (see docs/AGENT_INTELLIGENCE.md for how the agent tool
registry reuses this).
"""

from typing import Optional

from app.operations.snapshot import OperationsSnapshot

__all__ = ["find_finding_item", "bounded_evidence_view"]


def find_finding_item(snapshot: OperationsSnapshot, finding_id: str) -> Optional[dict]:
    """Return the {"finding", "workflow", "priority"} item for
    `finding_id` in `snapshot.findings`, or None if it isn't present."""
    for item in snapshot.findings:
        if item["finding"]["id"] == finding_id:
            return item
    return None


def bounded_evidence_view(finding: dict) -> dict:
    """The bounded, evidence-only view of one Finding.to_dict() -- id,
    title, category, severity, source, confidence, and up to 10 evidence
    references. Never the full finding/workflow/priority item, and
    never a subscription id (EvidenceReference.resource_id is passed
    through here, same as the rest of this app's engineer-facing routes
    -- see app/operations/routes.py's module docstring for why the
    executive brief is the one surface that strips it)."""
    evidence = finding.get("evidence") or []
    return {
        "id": finding["id"],
        "title": finding["title"],
        "category": finding["category"],
        "severity": finding["severity"],
        "source": finding["source"],
        "confidence": finding["confidence"],
        "evidence_count": len(evidence),
        "evidence": evidence[:10],
    }
