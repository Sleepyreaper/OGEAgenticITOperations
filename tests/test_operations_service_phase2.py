#!/usr/bin/env python3
"""Test the Phase 2 orchestration additions to app/operations/service.py
-- run_full_collection() (all 14 sources, fixed order, strictly additive
over run_collection()), summarize_coverage() (the consolidated source
coverage/gap inventory), not_configured/not_supported semantics for each
Phase 2 source, and that one source's failure never affects another's.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_service_phase2.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations import service  # noqa: E402
from app.operations.config import OperationsConfig  # noqa: E402

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


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)

_PHASE1_SOURCES = ["azure_monitor_alerts", "activity_log_change_health", "capacity", "workload_slo"]
_PHASE2_SOURCES = [
    "defender_alerts", "defender_assessments", "cost_management_budget", "cost_management_trend",
    "azure_backup", "update_manager", "key_vault_expiry", "automation_failures",
    "telemetry_coverage", "retirement_advisories",
]


class FakeToken:
    token = "fake-token"  # noqa: S105


class FakeCredential:
    def get_token(self, scope):
        return FakeToken()


class FailingCredential:
    def get_token(self, scope):
        raise RuntimeError("no managed identity available")


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def make_http_get(defender_alerts_status=200):
    def _fake(url, *, headers, params=None, timeout=30):
        if "Microsoft.Security/alerts" in url:
            return FakeResponse({"value": []}, status_code=defender_alerts_status, text="boom")
        return FakeResponse({"value": []})
    return _fake


def fake_http_post(url, *, headers, json=None, timeout=30):
    return FakeResponse({"columns": [{"name": "Cost"}], "rows": [[0.0]]})


def fake_query_logs(*args, **kwargs):
    return []


def fake_query_resource_graph(query, subscription_ids):
    return []


# ─── run_full_collection -- exactly 14 envelopes, fixed order ─────────
print("\n\U0001f9ea Test 1: run_full_collection -- 14 envelopes total, Phase 1 first (unchanged order), then Phase 2")
envelopes = service.run_full_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(), http_post=fake_http_post,
    query_logs_fn=fake_query_logs, query_resource_graph_fn=fake_query_resource_graph,
)
test("returns exactly 14 envelopes", len(envelopes) == 14)
test("the first 4 sources are Phase 1's, in Phase 1's exact order", [e.source for e in envelopes[:4]] == _PHASE1_SOURCES)
test("the next 10 sources are Phase 2's, in the documented order", [e.source for e in envelopes[4:]] == _PHASE2_SOURCES)

phase1_only = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(), query_logs_fn=fake_query_logs,
)
test("run_collection() is unaffected by Phase 2 -- still exactly 4 envelopes", len(phase1_only) == 4)


def _without_timing(envelope_dict: dict) -> dict:
    """Every envelope stamps its own real wall-clock collection time
    (`collected_at`, via utc_now_iso()) AND its own real per-source
    collection latency (`duration_ms`, see
    app.operations.service._execute_source_task) -- comparing either of
    those between two SEPARATE run_collection()/run_full_collection()
    invocations is inherently flaky (millisecond-boundary-dependent),
    not a meaningful assertion. Strip both so the comparison below is
    about the actual envelope CONTENT
    (source/status/findings/summaries/error) matching, which is the
    real guarantee this test is checking."""
    return {k: v for k, v in envelope_dict.items() if k not in ("collected_at", "duration_ms")}


test(
    "run_full_collection's first 4 envelopes match a standalone run_collection() call byte-for-byte",
    [_without_timing(e.to_dict()) for e in envelopes[:4]] == [_without_timing(e.to_dict()) for e in phase1_only],
)

# ─── not_configured semantics -- disabled flags and empty required lists ──
print("\n\U0001f9ea Test 2: not_configured semantics -- disabled feature flags and empty required input lists")
by_source = {e.source: e for e in envelopes}
test("key_vault_expiry is not_configured (no KEY_VAULT_MONITOR_URIS)", by_source["key_vault_expiry"].status == "not_configured")
test("automation_failures is not_configured (no AUTOMATION_ACCOUNT_IDS)", by_source["automation_failures"].status == "not_configured")
test("telemetry_coverage is not_configured (no matched/pinned resources)", by_source["telemetry_coverage"].status == "not_configured")
test("every not_configured envelope still carries an explanatory error message", all(
    bool(e.error) for e in envelopes if e.status == "not_configured"
))

disabled_config = OperationsConfig(
    enable_defender_alerts=False, enable_cost_management_budget=False, enable_backup=False,
)
disabled_envelopes = service.run_full_collection(
    ["sub1"], config=disabled_config, locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(), http_post=fake_http_post,
    query_logs_fn=fake_query_logs, query_resource_graph_fn=fake_query_resource_graph,
)
disabled_by_source = {e.source: e for e in disabled_envelopes}
test("a disabled ENABLE_DEFENDER_ALERTS flag -> not_configured (not error/ok)", disabled_by_source["defender_alerts"].status == "not_configured")
test("a disabled ENABLE_COST_MANAGEMENT_BUDGET flag -> not_configured", disabled_by_source["cost_management_budget"].status == "not_configured")
test("a disabled ENABLE_BACKUP flag -> not_configured", disabled_by_source["azure_backup"].status == "not_configured")
test("disabling one source does not affect an unrelated, still-enabled source", disabled_by_source["defender_assessments"].status == "ok")

# ─── not_supported semantics -- cost trend, unsupported billing scope ──
print("\n\U0001f9ea Test 3: not_supported semantics -- cost_management_trend on an unsupported billing scope")


def http_post_not_supported(url, *, headers, json=None, timeout=30):
    return FakeResponse({}, status_code=400, text="The requested operation is not supported for this billing account.")


not_supported_config = OperationsConfig()
env = service.collect_cost_trend_envelope(["sub1"], not_supported_config, credential_factory=FakeCredential, http_post=http_post_not_supported)
test("a Cost Management 'not supported' error maps to envelope status not_supported", env.status == "not_supported")
test("a not_supported envelope still carries an explanatory error message", bool(env.error))

try:
    service.CollectionEnvelope(source="x", status="not_supported", collected_at="2026-01-01T00:00:00.000Z")
    test("CollectionEnvelope requires an error message when status == not_supported", False)
except ValueError:
    test("CollectionEnvelope requires an error message when status == not_supported", True)

# ─── Partial failures -- one Phase 2 source errors, others are unaffected ──
print("\n\U0001f9ea Test 4: partial failures -- defender_alerts fails (HTTP 500); every other source still completes")
envelopes_partial = service.run_full_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=make_http_get(defender_alerts_status=500), http_post=fake_http_post,
    query_logs_fn=fake_query_logs, query_resource_graph_fn=fake_query_resource_graph,
)
partial_by_source = {e.source: e for e in envelopes_partial}
test("defender_alerts reports status=error", partial_by_source["defender_alerts"].status == "error")
test("defender_alerts has no findings (never a partial/guessed result)", partial_by_source["defender_alerts"].findings == [])
test("defender_assessments (a different API call) is unaffected, still ok", partial_by_source["defender_assessments"].status == "ok")
test("azure_monitor_alerts (Phase 1, unrelated API) is unaffected, still ok", partial_by_source["azure_monitor_alerts"].status == "ok")
test("update_manager (Resource Graph, unrelated) is unaffected, still ok", partial_by_source["update_manager"].status == "ok")

# ─── summarize_coverage -- the consolidated source coverage/gap inventory ──
print("\n\U0001f9ea Test 5: summarize_coverage -- consolidated source coverage/gap inventory")
coverage = service.summarize_coverage(envelopes)
test("total_sources matches the envelope count (14)", coverage["total_sources"] == 14)
test("ok_count + error_count + not_configured_count + not_supported_count == total_sources", (
    coverage["ok_count"] + coverage["error_count"] + coverage["not_configured_count"] + coverage["not_supported_count"]
    == coverage["total_sources"]
))
test("not_configured_count reflects the 3 known-empty-input sources (workload_slo, key_vault_expiry, automation_failures, telemetry_coverage == 4)", coverage["not_configured_count"] == 4)
test("sources_by_status lists the actual source names, not just counts", "key_vault_expiry" in coverage["sources_by_status"]["not_configured"])

partial_coverage = service.summarize_coverage(envelopes_partial)
test("summarize_coverage reflects a partial-failure run's error correctly", partial_coverage["error_count"] == 1)
test("summarize_coverage works over any envelope list, e.g. Phase 1 alone", service.summarize_coverage(phase1_only)["total_sources"] == 4)


# ─── Malformed upstream data (ValueError/TypeError) is contained to its
# own source's error envelope, not just OperationsCollectionError --
# see app.operations.service._collect_envelope's _EXPECTED_SOURCE_
# FAILURES. A malformed budget record (a non-numeric "amount", which
# cost.budget_to_summary's float() coercion rejects with a ValueError)
# used to escape collect_cost_budget_envelope entirely and would have
# crashed the whole collection run; it must now surface as ONLY
# cost_management_budget's own 'error' envelope.
print("\n\U0001f9ea Test 6: malformed cost budget record (non-numeric amount) is contained to cost_management_budget alone")


def http_get_malformed_budget(url, *, headers, params=None, timeout=30):
    if "Microsoft.Consumption/budgets" in url:
        return FakeResponse({"value": [{
            "id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/bad", "name": "bad",
            "properties": {"category": "Cost", "amount": "not-a-number", "timeGrain": "Monthly", "currentSpend": {"amount": 10, "unit": "USD"}},
        }]})
    return FakeResponse({"value": []})


envelopes_bad_budget = service.run_full_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=http_get_malformed_budget, http_post=fake_http_post,
    query_logs_fn=fake_query_logs, query_resource_graph_fn=fake_query_resource_graph,
)
bad_budget_by_source = {e.source: e for e in envelopes_bad_budget}
test("a malformed (non-numeric) budget amount surfaces as cost_management_budget's own error envelope", bad_budget_by_source["cost_management_budget"].status == "error")
test("cost_management_budget has no findings/summaries (never a partial/guessed result)", (
    bad_budget_by_source["cost_management_budget"].findings == [] and bad_budget_by_source["cost_management_budget"].summaries == []
))
test("defender_alerts (unrelated API) is unaffected, still ok", bad_budget_by_source["defender_alerts"].status == "ok")
test("azure_backup (unrelated source) is unaffected, still ok", bad_budget_by_source["azure_backup"].status == "ok")
test("azure_monitor_alerts (Phase 1, unrelated) is unaffected, still ok", bad_budget_by_source["azure_monitor_alerts"].status == "ok")


# ─── Optional Log Analytics table not present -- classified as
# not_configured, never a false 'ok'/generic 'error' ───────────────────
print("\n\U0001f9ea Test 7: a missing optional Log Analytics table (Backup/Heartbeat) classifies as not_configured, not error")


def missing_table_query_logs(query, workspace_id, timespan):
    # Mirrors the real azure-monitor-query HttpResponseError text for a
    # KQL query referencing a table that doesn't exist in the workspace
    # (a SemanticError nested under BadArgumentError) -- see
    # docs/AZURE_DATA_SOURCES.md.
    raise RuntimeError(
        "(BadArgumentError) The request had some invalid properties\nInner error: "
        "{\"code\": \"SemanticError\", \"message\": \"'where' operator: Failed to resolve table or "
        f"column or scalar expression named '{query.split()[0]}'\"}}"
    )


backup_not_configured_env = service.collect_backup_envelope(OperationsConfig(), query_logs_fn=missing_table_query_logs)
test("azure_backup classifies a missing-table KQL error as not_configured (not error)", backup_not_configured_env.status == "not_configured")
test("the not_configured envelope still carries the underlying error message", "AddonAzureBackupJobs" in backup_not_configured_env.error)

telemetry_not_configured_env = service.collect_telemetry_coverage_envelope(
    ["sub1"], OperationsConfig(), credential_factory=FakeCredential, http_get=make_http_get(),
    query_logs_fn=missing_table_query_logs, query_resource_graph_fn=fake_query_resource_graph,
    extra_resource_ids=["/subscriptions/s/rg/vm1"],
)
test("telemetry_coverage classifies a missing Heartbeat table as not_configured (not error)", telemetry_not_configured_env.status == "not_configured")
test("the not_configured envelope still carries the underlying error message", "Heartbeat" in telemetry_not_configured_env.error)


def genuine_outage_query_logs(query, workspace_id, timespan):
    raise RuntimeError("(GatewayTimeout) upstream Log Analytics query gateway timed out")


backup_genuine_error_env = service.collect_backup_envelope(OperationsConfig(), query_logs_fn=genuine_outage_query_logs)
test("a genuine (non-missing-table) Log Analytics failure still classifies as error, never misreported as not_configured", backup_genuine_error_env.status == "error")


print("\n\U0001f9ea Test 8: collect_defender_assessments_envelope -- a later-page failure surfaces as coverage_warning, status stays 'ok'")
ASSESSMENTS_PAGE2_URL = "https://management.azure.com/subscriptions/sub1/providers/Microsoft.Security/assessments?api-version=x&page=2"
ASSESSMENT_PAGE1 = {"value": [
    {"id": "/subscriptions/sub1/.../assessments/a1", "name": "a1", "properties": {
        "displayName": "Page 1 recommendation", "status": {"code": "Unhealthy"},
        "metadata": {"severity": "High"},
    }},
], "nextLink": ASSESSMENTS_PAGE2_URL}


def http_get_assessments_partial(url, *, headers, params=None, timeout=30):
    if "Microsoft.Security/assessments" in url and "page=2" in url:
        return FakeResponse({}, status_code=503, text="Service Unavailable")
    if "Microsoft.Security/assessments" in url:
        return FakeResponse(ASSESSMENT_PAGE1)
    return FakeResponse({"value": []})


partial_env = service.collect_defender_assessments_envelope(
    ["sub1"], OperationsConfig(), credential_factory=FakeCredential, http_get=http_get_assessments_partial,
)
test("status stays 'ok' despite the later-page failure (never escalated to 'error')", partial_env.status == "ok")
test("page 1's assessment Finding is still present, not discarded", len(partial_env.findings) == 1)
test("coverage_warning is set with the page-2 failure's detail", bool(partial_env.coverage_warning))
test("CollectionEnvelope.to_dict() exposes coverage_warning", partial_env.to_dict()["coverage_warning"] == partial_env.coverage_warning)

full_success_env = service.collect_defender_assessments_envelope(
    ["sub1"], OperationsConfig(), credential_factory=FakeCredential, http_get=make_http_get(),
)
test("coverage_warning is None when every page fetches successfully", full_success_env.coverage_warning is None)
test("to_dict() reports coverage_warning: null when unset", full_success_env.to_dict()["coverage_warning"] is None)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
