#!/usr/bin/env python3
"""Test the bounded-concurrency refactor of app/operations/service.py's
run_collection()/run_full_collection() -- the fix for
/api/operations/brief?refresh=true sequentially collecting up to 22
Azure sources and exceeding a 300s Gunicorn worker timeout (502 gateway
error).

Covers, all through the PUBLIC run_collection/run_full_collection entry
points with injected fakes (no real network calls):

  1. Real wall-clock speedup -- concurrent sources finish in well under
     the sum of their own individual durations (proof collection is
     actually parallelized, not just refactored cosmetically).
  2. Deterministic envelope order -- unchanged regardless of which
     source's collector actually finishes first.
  3. run_full_collection runs ONE flat 14-source pool (not run_collection
     nested inside a second pool) -- Phase 1 sources overlap with Phase 2
     sources' wall-clock time, not just with each other.
  4. Bounded max workers -- peak concurrent in-flight collector calls
     never exceeds OperationsConfig.operations_collection_max_workers,
     and never exceeds the number of sources actually being collected
     even when max_workers is configured higher.
  5. The config hard cap itself (12) is enforced.
  6. Partial failure isolation -- a genuinely UNEXPECTED exception (not
     one of _EXPECTED_SOURCE_FAILURES) escaping one source's collector
     is contained to that source's own 'error' envelope; the other 13
     sources still collect successfully and run_full_collection never
     raises.

All timing assertions use generous (200ms+) sleeps and wide margins to
avoid flakiness on a slow/contended CI/test machine -- see each test's
comment for the exact margin reasoning.

Run: python3 tests/test_operations_service_concurrency.py
"""
import sys
import threading
import time as real_time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations import service  # noqa: E402
from app.operations.config import OperationsConfig, OperationsConfigError  # noqa: E402

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


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


ALERT_PAYLOAD = {"value": []}
# A generic, universally-valid "nothing found" ARM list response --
# every collector that reads a `{"value": [...]}` shaped ARM list API
# (compute/OpenAI usages, Defender assessments, Cost Management
# budgets, ...) handles an empty list cleanly (0 results, status stays
# 'ok'). Deliberately NOT source-specific data, so these tests' fakes
# can share one fallback response across every source without any one
# source's normalization choking on a shape it wasn't meant for.
COMPUTE_PAYLOAD = {"value": []}
COST_TREND_PAYLOAD = {"columns": [{"name": "Cost"}], "rows": [[0.0]]}


# ─── Test 1+2: run_collection -- concurrent wall-clock speedup, fixed order ──
print("\n\U0001f9ea Test 1: run_collection -- sources actually overlap in wall-clock time (not just refactored cosmetically)")

_SLEEP_SECONDS = 0.3  # generous -- real threading overhead is a few ms, nowhere close to masking this


def slow_http_get(url, *, headers, params=None, timeout=30):
    real_time.sleep(_SLEEP_SECONDS)
    if "AlertsManagement" in url:
        return FakeResponse(ALERT_PAYLOAD)
    return FakeResponse(COMPUTE_PAYLOAD)


def slow_query_logs_fn(query, workspace_id, timespan):
    real_time.sleep(_SLEEP_SECONDS)
    return []


wall_start = real_time.monotonic()
envelopes1 = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=slow_http_get, query_logs_fn=slow_query_logs_fn,
)
wall_ms1 = (real_time.monotonic() - wall_start) * 1000

test("returns exactly 4 envelopes", len(envelopes1) == 4)
test(
    "every envelope is stamped with a non-negative duration_ms",
    all(e.duration_ms is not None and e.duration_ms >= 0 for e in envelopes1),
)
total_duration_ms1 = sum(e.duration_ms for e in envelopes1)
test(
    "4 sources overlap concurrently -- wall clock is well under the SUM of each source's own duration_ms "
    "(a fully sequential run's wall clock would be roughly equal to that sum, not meaningfully less than it)",
    wall_ms1 < total_duration_ms1 * 0.7,
)
test(
    "wall clock stays within a generous absolute bound (2 sequential internal calls' worth, not 4+)",
    wall_ms1 < _SLEEP_SECONDS * 1000 * 2.5,
)

print("\n\U0001f9ea Test 2: run_collection -- envelope order is fixed regardless of which source finishes first")


def skewed_http_get(url, *, headers, params=None, timeout=30):
    # azure_monitor_alerts (submitted FIRST) is deliberately the
    # SLOWEST source; capacity (submitted THIRD) is fast. If order were
    # completion-order instead of submission-order, capacity would come
    # before alerts in the result.
    if "AlertsManagement" in url:
        real_time.sleep(0.4)
        return FakeResponse(ALERT_PAYLOAD)
    real_time.sleep(0.02)
    return FakeResponse(COMPUTE_PAYLOAD)


def fast_query_logs_fn(query, workspace_id, timespan):
    return []


envelopes2 = service.run_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=skewed_http_get, query_logs_fn=fast_query_logs_fn,
)
test(
    "order is [alerts, change_health, capacity, workload_slo] even though alerts (submitted first) finishes LAST",
    [e.source for e in envelopes2] == _PHASE1_SOURCES,
)


# ─── Test 3: run_full_collection -- ONE flat 14-source pool, not a nested pool ──
print("\n\U0001f9ea Test 3: run_full_collection -- Phase 1 and Phase 2 sources overlap in ONE flat pool (not two sequential pools)")


def slow_http_post(url, *, headers, json=None, timeout=30):
    real_time.sleep(_SLEEP_SECONDS)
    return FakeResponse(COST_TREND_PAYLOAD)


def slow_query_resource_graph(query, subscription_ids):
    real_time.sleep(_SLEEP_SECONDS)
    return []


wall_start3 = real_time.monotonic()
envelopes3 = service.run_full_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=slow_http_get, http_post=slow_http_post,
    query_logs_fn=slow_query_logs_fn, query_resource_graph_fn=slow_query_resource_graph,
)
wall_ms3 = (real_time.monotonic() - wall_start3) * 1000

test("returns exactly 14 envelopes", len(envelopes3) == 14)
test("Phase 1 sources are first, in their fixed order", [e.source for e in envelopes3[:4]] == _PHASE1_SOURCES)
test("Phase 2 sources follow, in their fixed order", [e.source for e in envelopes3[4:]] == _PHASE2_SOURCES)

total_duration_ms3 = sum(e.duration_ms for e in envelopes3)
test(
    "14 sources (Phase 1 AND Phase 2 together) overlap concurrently in ONE pool -- wall clock is well under "
    "the sum of each source's own duration_ms (if run_full_collection nested run_collection's pool separately "
    "from Phase 2's, Phase 2 could never start until every Phase 1 source finished, inflating wall clock)",
    wall_ms3 < total_duration_ms3 * 0.6,
)
test(
    "wall clock stays within a generous absolute bound given the default max_workers=6 bound and a handful "
    "of sources genuinely calling out (not the naive ~11-source sequential sum)",
    wall_ms3 < _SLEEP_SECONDS * 1000 * 6,
)


# ─── Test 4: bounded max workers -- peak concurrency never exceeds the configured/hard cap ──
print("\n\U0001f9ea Test 4: run_full_collection -- peak concurrent collector calls never exceeds operations_collection_max_workers")

_peak_lock = threading.Lock()
_current_inflight = 0
_peak_inflight = 0


def _track_start():
    global _current_inflight, _peak_inflight
    with _peak_lock:
        _current_inflight += 1
        _peak_inflight = max(_peak_inflight, _current_inflight)


def _track_end():
    global _current_inflight
    with _peak_lock:
        _current_inflight -= 1


def bounded_http_get(url, *, headers, params=None, timeout=30):
    _track_start()
    try:
        real_time.sleep(0.2)
    finally:
        _track_end()
    if "AlertsManagement" in url:
        return FakeResponse(ALERT_PAYLOAD)
    return FakeResponse(COMPUTE_PAYLOAD)


def bounded_http_post(url, *, headers, json=None, timeout=30):
    _track_start()
    try:
        real_time.sleep(0.2)
    finally:
        _track_end()
    return FakeResponse(COST_TREND_PAYLOAD)


def bounded_query_logs_fn(query, workspace_id, timespan):
    _track_start()
    try:
        real_time.sleep(0.2)
    finally:
        _track_end()
    return []


def bounded_query_resource_graph(query, subscription_ids):
    _track_start()
    try:
        real_time.sleep(0.2)
    finally:
        _track_end()
    return []


bounded_config = OperationsConfig(operations_collection_max_workers=3)
envelopes4 = service.run_full_collection(
    ["sub1"], config=bounded_config, locations=["eastus"],
    credential_factory=FakeCredential, http_get=bounded_http_get, http_post=bounded_http_post,
    query_logs_fn=bounded_query_logs_fn, query_resource_graph_fn=bounded_query_resource_graph,
)
test("returns exactly 14 envelopes despite the reduced worker bound", len(envelopes4) == 14)
test("peak concurrent in-flight collector calls never exceeded operations_collection_max_workers=3", _peak_inflight <= 3)
test("real concurrency actually happened (peak > 1, not accidentally serialized)", _peak_inflight > 1)

# A generous max_workers (the hard cap, 12) with only 4 Phase-1 sources
# must still never exceed 4 in-flight calls at once -- _resolve_max_workers
# bounds by task_count too, not just by config, so no idle/unused worker
# threads are spun up for a small task list.
_peak_inflight_2 = 0
_current_inflight_2 = 0
_lock2 = threading.Lock()


def _track_start2():
    global _current_inflight_2, _peak_inflight_2
    with _lock2:
        _current_inflight_2 += 1
        _peak_inflight_2 = max(_peak_inflight_2, _current_inflight_2)


def _track_end2():
    global _current_inflight_2
    with _lock2:
        _current_inflight_2 -= 1


def bounded_http_get_2(url, *, headers, params=None, timeout=30):
    _track_start2()
    try:
        real_time.sleep(0.2)
    finally:
        _track_end2()
    if "AlertsManagement" in url:
        return FakeResponse(ALERT_PAYLOAD)
    return FakeResponse(COMPUTE_PAYLOAD)


def bounded_query_logs_fn_2(query, workspace_id, timespan):
    _track_start2()
    try:
        real_time.sleep(0.2)
    finally:
        _track_end2()
    return []


hard_cap_config = OperationsConfig(operations_collection_max_workers=12)
service.run_collection(
    ["sub1"], config=hard_cap_config, locations=["eastus"],
    credential_factory=FakeCredential, http_get=bounded_http_get_2, query_logs_fn=bounded_query_logs_fn_2,
)
test(
    "with only 4 Phase-1 sources, peak in-flight calls never exceeds 4 even though max_workers=12 is configured",
    _peak_inflight_2 <= 4,
)


# ─── Test 5: config hard cap is enforced ───────────────────────────────
print("\n\U0001f9ea Test 5: OperationsConfig -- operations_collection_max_workers is bounded to the documented hard cap")
try:
    OperationsConfig(operations_collection_max_workers=13)
    test("a value above the hard cap raises OperationsConfigError", False)
except OperationsConfigError:
    test("a value above the hard cap raises OperationsConfigError", True)
test(
    "the hard cap value itself (12) is accepted",
    OperationsConfig(operations_collection_max_workers=12).operations_collection_max_workers == 12,
)
test("the documented default is 6", OperationsConfig().operations_collection_max_workers == 6)


# ─── Test 6: partial failure isolation -- an UNEXPECTED exception in one source is contained ──
print("\n\U0001f9ea Test 6: run_full_collection -- a genuinely unexpected exception in ONE source's collector never crashes the other 13")


def http_get_raises_for_defender(url, *, headers, params=None, timeout=30):
    if "Microsoft.Security/alerts" in url:
        raise RuntimeError("boom -- simulated unexpected bug, not a classified Azure/data failure")
    if "AlertsManagement" in url:
        return FakeResponse(ALERT_PAYLOAD)
    return FakeResponse(COMPUTE_PAYLOAD)


def fast_http_post(url, *, headers, json=None, timeout=30):
    return FakeResponse(COST_TREND_PAYLOAD)


def fast_query_resource_graph(query, subscription_ids):
    return []


envelopes6 = service.run_full_collection(
    ["sub1"], config=OperationsConfig(), locations=["eastus"],
    credential_factory=FakeCredential, http_get=http_get_raises_for_defender, http_post=fast_http_post,
    query_logs_fn=fast_query_logs_fn, query_resource_graph_fn=fast_query_resource_graph,
)
test("run_full_collection still returns exactly 14 envelopes -- it never raised/crashed", len(envelopes6) == 14)
by_source6 = {e.source: e for e in envelopes6}
test("defender_alerts (the source whose collector raised) reports its OWN 'error' envelope", by_source6["defender_alerts"].status == "error")
test(
    "defender_alerts' error message identifies this as an unexpected/contained failure, not a classified one",
    "unexpected collector failure" in by_source6["defender_alerts"].error and "boom" in by_source6["defender_alerts"].error,
)
test("the unrelated azure_monitor_alerts source is unaffected, still ok", by_source6["azure_monitor_alerts"].status == "ok")
test("the unrelated capacity source is unaffected, still ok", by_source6["capacity"].status == "ok")
test(
    "every OTHER source besides defender_alerts collected without being dragged down by its failure",
    all(e.status != "error" for e in envelopes6 if e.source != "defender_alerts"),
)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
