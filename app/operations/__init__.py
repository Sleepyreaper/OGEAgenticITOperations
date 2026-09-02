"""Deterministic Azure operations evidence layer.

This package produces a structured, deterministic layer of Findings and
summaries (SLO/capacity) from real Azure signals -- Azure Monitor alerts,
Activity Log changes, Resource Health, regional capacity/quota, and
configurable workload SLOs. It intentionally does NOT call an LLM or any
other model: every Finding here is either a direct platform-reported fact
or a deterministically computed/correlated one (see docs/EVIDENCE_MODEL.md
for the exact confidence taxonomy). Executive/ops dashboards and AI agents
built on top of this package reason over these Findings rather than
re-deriving them from raw Azure payloads.

No Flask routes live here (yet) -- see app/main.py for wiring once a
consumer is ready.
"""
