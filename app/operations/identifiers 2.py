"""Deterministic ID generation for the operations evidence layer.

IDs here are derived (sha256) from the semantic content that identifies a
Finding/ActionItem, never from a random UUID or a wall-clock timestamp.
Re-running a collector against the same underlying Azure state must
produce the same ID for the same logical Finding, so downstream
consumers (dashboards, ADO proposals, dedup logic) can treat the ID as a
stable key across collection runs.
"""

import hashlib
from typing import Optional

# Short, human-scannable prefixes so an ID hints at its category without a
# lookup (e.g. "chg-..." for a change finding). "fnd" is the fallback for
# any category not in this table (kept in sync with
# app.operations.models.FindingCategory, but not import-coupled to it so
# identifiers.py has zero dependencies within this package).
_CATEGORY_PREFIX = {
    "incident": "inc",
    "reliability": "rel",
    "capacity": "cap",
    "change": "chg",
    "security": "sec",
    "compliance": "cmp",
    "cost": "cst",
    "backup": "bkp",
    "patch": "pat",
    "certificate": "crt",
    "automation": "aut",
    "telemetry": "tel",
    "ownership": "own",
}

_ID_DIGEST_CHARS = 16


def compute_finding_id(
    *,
    category: str,
    source: str,
    resource_id: Optional[str],
    discriminator: str = "",
) -> str:
    """Stable Finding ID from (category, source, resource_id, discriminator).

    `discriminator` disambiguates Findings that share a category/source/
    resource_id (e.g. an alert rule ID, an SLO workload name, or a
    correlation timestamp) -- pass the most specific stable value the
    caller has, since it is folded into the hash verbatim.
    """
    prefix = _CATEGORY_PREFIX.get(category, "fnd")
    canonical = "|".join([category or "", source or "", resource_id or "", discriminator or ""])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_ID_DIGEST_CHARS]
    return f"{prefix}-{digest}"


def compute_action_item_id(*, finding_id: str, title: str, discriminator: str = "") -> str:
    """Stable ActionItem ID, scoped under its parent Finding's ID."""
    canonical = "|".join([finding_id or "", title or "", discriminator or ""])
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_ID_DIGEST_CHARS]
    return f"act-{digest}"
