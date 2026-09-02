#!/usr/bin/env python3
"""Test Azure Cost Management budget/trend collectors
(app/operations/collectors/cost.py) -- threshold state classification,
severity, that the trend collector never fabricates a Finding without a
real baseline, that the trend collector issues exactly ONE
Microsoft.CostManagement/query POST per invocation (Daily granularity,
covering both the current and prior windows) and splits/sums the
returned daily rows locally, and explicit failure surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_cost.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import cost  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402
from app.operations.models import format_utc_iso  # noqa: E402

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


NOW = datetime(2026, 2, 1, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeCredential:
    def get_token(self, scope):
        class Token:
            token = "fake-token"  # noqa: S105
        return Token()


def fake_credential_factory():
    return FakeCredential()


BUDGETS_PAYLOAD = {"value": [
    {"id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/b1", "name": "b1",
     "properties": {"category": "Cost", "amount": 100, "timeGrain": "Monthly", "currentSpend": {"amount": 85, "unit": "USD"}}},
    {"id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/b2", "name": "b2",
     "properties": {"category": "Cost", "amount": 100, "timeGrain": "Monthly", "currentSpend": {"amount": 105, "unit": "USD"}}},
    {"id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/b3", "name": "b3",
     "properties": {"category": "Cost", "amount": 100, "timeGrain": "Monthly", "currentSpend": {"amount": 20, "unit": "USD"}}},
]}


def http_get_budgets(url, *, headers, params=None, timeout=30):
    return FakeResponse(BUDGETS_PAYLOAD)


# ─── Budget threshold state -- every budget gets a summary ─────────────
print("\n\U0001f9ea Test 1: collect_budget_summaries -- healthy/warning/critical classification")
summaries = cost.collect_budget_summaries("sub1", warning_pct=80.0, critical_pct=100.0, credential_factory=fake_credential_factory, http_get=http_get_budgets, now=NOW)
test("every configured budget gets a summary (3), not just breaching ones", len(summaries) == 3)
by_name = {s.budget_name: s for s in summaries}
test("85% usage vs 80/100 thresholds -> warning", by_name["b1"].threshold_state == "warning")
test("105% usage (over-budget) -> critical", by_name["b2"].threshold_state == "critical")
test("20% usage -> healthy", by_name["b3"].threshold_state == "healthy")
test("usage_pct is computed from currentSpend/amount", by_name["b1"].usage_pct == 85.0)

print("\n\U0001f9ea Test 2: budget_summaries_to_findings -- only warning/critical become Findings")
findings = cost.budget_summaries_to_findings(summaries)
test("exactly 2 Findings (b1 warning, b2 critical) -- b3 healthy stays informational-only", len(findings) == 2)
severities = {f.metadata["budget_name"]: f.severity for f in findings}
test("warning budget -> medium severity", severities["b1"] == "medium")
test("critical budget -> high severity", severities["b2"] == "high")
test("all cost Findings use category cost", all(f.category == "cost" for f in findings))
test("confidence is derived (threshold math, not a raw platform fact)", all(f.confidence == "derived" for f in findings))
critical_finding = next(f for f in findings if f.metadata["budget_name"] == "b2")
test("a critical budget demands executive attention", critical_finding.executive_attention is True)

# ─── Cost trend -- ONE query covering both periods, split/summed locally ──
# NOW = 2026-02-01T00:00:00Z, lookback_days=30 ->
#   current_start = 2026-01-02T00:00:00Z, current_end = NOW
#   prior_start   = 2025-12-03T00:00:00Z
from datetime import timedelta as _timedelta  # noqa: E402

CURRENT_END = NOW
CURRENT_START = NOW - _timedelta(days=30)
PRIOR_START = CURRENT_START - _timedelta(days=30)
EXPECTED_QUERY_URL = f"https://management.azure.com/subscriptions/sub1/providers/Microsoft.CostManagement/query?api-version={cost.QUERY_API_VERSION}"
EXPECTED_QUERY_BODY = {
    "type": "ActualCost",
    "timeframe": "Custom",
    "timePeriod": {"from": format_utc_iso(PRIOR_START), "to": format_utc_iso(CURRENT_END)},
    "dataset": {
        "granularity": "Daily",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
    },
}

print("\n\U0001f9ea Test 3: collect_cost_trend -- exactly one POST, exact request shape, material growth raises a Finding")

_calls = []
_urls = []


def http_post_growth(url, *, headers, json=None, timeout=30):
    _calls.append(json)
    _urls.append(url)
    # One Daily-granularity response covering both periods: 100.0 prior, 150.0 current.
    return FakeResponse({
        "columns": [{"name": "UsageDate"}, {"name": "Cost"}],
        "rows": [[20251215, 100.0], [20260115, 150.0]],
    })


trend_findings = cost.collect_cost_trend("sub1", lookback_days=30, growth_pct_threshold=20.0, credential_factory=fake_credential_factory, http_post=http_post_growth, now=NOW)
test("exactly one underlying POST is made per collect_cost_trend invocation (never two)", len(_calls) == 1)
test("the request hits the exact Microsoft.CostManagement/query URL with the documented api-version (2023-11-01)", _urls[0] == EXPECTED_QUERY_URL and cost.QUERY_API_VERSION == "2023-11-01")
test("the request body is exactly one Daily-granularity ActualCost query covering prior_start..current_end", _calls[0] == EXPECTED_QUERY_BODY)
test("a 50% period-over-period increase (>= 20% threshold) raises exactly one Finding", len(trend_findings) == 1)
test("the trend Finding is category cost, medium severity", trend_findings[0].category == "cost" and trend_findings[0].severity == "medium")
test("growth_pct is exposed in metadata, not hidden in a score", trend_findings[0].metadata["growth_pct"] == 50.0)
test("current/prior period costs in metadata reflect the correct locally-split sums", trend_findings[0].metadata["current_period_cost"] == 150.0 and trend_findings[0].metadata["prior_period_cost"] == 100.0)


print("\n\U0001f9ea Test 3a: collect_cost_trend -- boundary split (prior_start/current_start/current_end inclusive) and int/string/ISO date parsing, all in one response")


def http_post_boundaries(url, *, headers, json=None, timeout=30):
    return FakeResponse({
        "columns": [{"name": "UsageDate"}, {"name": "Cost"}],
        "rows": [
            [20251203, 5.0],                    # == prior_start (int YYYYMMDD)      -> prior
            ["20260101", 7.0],                  # one day before current_start (str) -> prior
            ["2026-01-02", 9.0],                # == current_start (ISO date)        -> current
            ["2026-02-01T00:00:00Z", 11.0],     # == current_end (ISO datetime+Z)    -> current
        ],
    })


boundary_findings = cost.collect_cost_trend("sub1", lookback_days=30, growth_pct_threshold=1.0, credential_factory=fake_credential_factory, http_post=http_post_boundaries, now=NOW)
test(
    "boundary dates split into exactly the documented prior/current buckets (12.0 prior, 20.0 current)",
    len(boundary_findings) == 1
    and boundary_findings[0].metadata["prior_period_cost"] == 12.0
    and boundary_findings[0].metadata["current_period_cost"] == 20.0,
)


print("\n\U0001f9ea Test 3b: collect_cost_trend -- documented fallback column names (date/PreTaxCost)")


def http_post_fallback_columns(url, *, headers, json=None, timeout=30):
    return FakeResponse({
        "columns": [{"name": "date"}, {"name": "PreTaxCost"}],
        "rows": [[20251215, 100.0], [20260115, 150.0]],
    })


fallback_findings = cost.collect_cost_trend("sub1", lookback_days=30, growth_pct_threshold=20.0, credential_factory=fake_credential_factory, http_post=http_post_fallback_columns, now=NOW)
test("the 'date'/'PreTaxCost' documented fallback column names are recognized identically to UsageDate/Cost", len(fallback_findings) == 1 and fallback_findings[0].metadata["growth_pct"] == 50.0)


print("\n\U0001f9ea Test 3c: collect_cost_trend -- below-threshold growth and a zero/absent prior baseline never raise a Finding")


def http_post_below_threshold(url, *, headers, json=None, timeout=30):
    return FakeResponse({
        "columns": [{"name": "UsageDate"}, {"name": "Cost"}],
        "rows": [[20251215, 100.0], [20260115, 105.0]],
    })


below = cost.collect_cost_trend("sub1", growth_pct_threshold=20.0, credential_factory=fake_credential_factory, http_post=http_post_below_threshold, now=NOW)
test("growth below the configured threshold raises no Finding", below == [])


def http_post_no_baseline(url, *, headers, json=None, timeout=30):
    return FakeResponse({"columns": [{"name": "UsageDate"}, {"name": "Cost"}], "rows": [[20260115, 50.0]]})


no_baseline = cost.collect_cost_trend("sub1", credential_factory=fake_credential_factory, http_post=http_post_no_baseline, now=NOW)
test("zero/absent prior-period cost -> no Finding (never a fabricated percentage from a zero baseline)", no_baseline == [])


def http_post_empty(url, *, headers, json=None, timeout=30):
    return FakeResponse({"columns": [{"name": "UsageDate"}, {"name": "Cost"}], "rows": []})


empty_series = cost.collect_cost_trend("sub1", credential_factory=fake_credential_factory, http_post=http_post_empty, now=NOW)
test("an entirely empty rows response (no cost data at all) returns [] rather than raising", empty_series == [])


print("\n\U0001f9ea Test 3d: collect_cost_trend -- a malformed response (no recognized date/cost column) raises OperationsCollectionError rather than fabricate")


def http_post_malformed(url, *, headers, json=None, timeout=30):
    return FakeResponse({"columns": [{"name": "Foo"}, {"name": "Bar"}], "rows": [[1, 2]]})


try:
    cost.collect_cost_trend("sub1", credential_factory=fake_credential_factory, http_post=http_post_malformed, now=NOW)
    test("a response with rows but no recognized date/cost column raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("a response with rows but no recognized date/cost column raises OperationsCollectionError", True)


def http_post_unparseable_date(url, *, headers, json=None, timeout=30):
    return FakeResponse({"columns": [{"name": "UsageDate"}, {"name": "Cost"}], "rows": [["not-a-date", 5.0]]})


try:
    cost.collect_cost_trend("sub1", credential_factory=fake_credential_factory, http_post=http_post_unparseable_date, now=NOW)
    test("an unparseable UsageDate value raises OperationsCollectionError instead of fabricating a date", False)
except OperationsCollectionError:
    test("an unparseable UsageDate value raises OperationsCollectionError instead of fabricating a date", True)


# ─── 429 throttling -- collect_cost_trend retries at the arm_post layer, never fails outright, and still makes only ONE logical arm_post call ──
print("\n\U0001f9ea Test 3e: collect_cost_trend -- a transient 429 is retried at the arm_post layer (with an injected sleep_fn), staying within a single arm_post call per invocation")
_throttled_calls = []
_sleep_calls = []
_arm_post_invocations = []
_original_arm_post = cost.arm_post


def _fake_sleep(seconds):
    _sleep_calls.append(seconds)


def _counting_arm_post(*args, **kwargs):
    """Wraps the real arm_post to count how many times cost.py's
    collector code itself calls arm_post (as opposed to how many times
    arm_post's own internal retry loop calls http_post) -- this is what
    proves collect_cost_trend makes exactly one logical query per
    invocation even though a 429 causes arm_post to retry internally."""
    _arm_post_invocations.append(1)
    return _original_arm_post(*args, **kwargs)


def http_post_throttled_then_growth(url, *, headers, json=None, timeout=30):
    _throttled_calls.append(json)
    # The single underlying query is throttled once; the retry (same
    # request, retried by arm_post itself) succeeds.
    if len(_throttled_calls) == 1:
        return FakeResponse({}, status_code=429, text="Too Many Requests")
    return FakeResponse({
        "columns": [{"name": "UsageDate"}, {"name": "Cost"}],
        "rows": [[20251215, 100.0], [20260115, 150.0]],
    })


cost.arm_post = _counting_arm_post
try:
    throttled_findings = cost.collect_cost_trend(
        "sub1", lookback_days=30, growth_pct_threshold=20.0, credential_factory=fake_credential_factory,
        http_post=http_post_throttled_then_growth, now=NOW, sleep_fn=_fake_sleep,
    )
finally:
    cost.arm_post = _original_arm_post

test("collect_cost_trend calls arm_post exactly once per invocation, even though that call internally retried", len(_arm_post_invocations) == 1)
test("the single arm_post call retried via 2 underlying HTTP POSTs (1 throttled + 1 successful retry)", len(_throttled_calls) == 2)
test("a single 429 is retried and the trend still resolves to the correct Finding", len(throttled_findings) == 1)
test("the resolved growth_pct reflects the retried (not the throttled) response", throttled_findings[0].metadata["growth_pct"] == 50.0)
test("collect_cost_trend never actually slept -- the injected sleep_fn recorded the backoff instead", len(_sleep_calls) == 1)

# ─── Explicit failure surfacing ─────────────────────────────────────────
print("\n\U0001f9ea Test 4: explicit failures -- never an empty success-shaped list")


def http_get_500(url, *, headers, params=None, timeout=30):
    return FakeResponse({}, status_code=500, text="boom")


try:
    cost.collect_budget_summaries("sub1", credential_factory=fake_credential_factory, http_get=http_get_500)
    test("a 500 response raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("a 500 response raises OperationsCollectionError instead of returning []", True)

try:
    cost.collect_cost_trend("sub1", lookback_days=0)
    test("a non-positive lookback_days raises ValueError", False)
except ValueError:
    test("a non-positive lookback_days raises ValueError", True)


# ─── Multipage/bounded pagination -- collect_budget_summaries follows nextLink ──
print("\n\U0001f9ea Test 5: collect_budget_summaries -- follows nextLink across multiple pages, and honors an injected bound")
BUDGETS_PAGE2_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Consumption/budgets?api-version=x&page=2"


def multipage_http_get_budgets(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [
            {"id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/b4", "name": "b4",
             "properties": {"category": "Cost", "amount": 100, "timeGrain": "Monthly", "currentSpend": {"amount": 10, "unit": "USD"}}},
        ]})
    return FakeResponse({"value": BUDGETS_PAYLOAD["value"], "nextLink": BUDGETS_PAGE2_URL})


multipage_summaries = cost.collect_budget_summaries("sub1", credential_factory=fake_credential_factory, http_get=multipage_http_get_budgets, now=NOW)
test("collects all 4 budgets across both pages (3 from page 1, 1 from page 2)", len(multipage_summaries) == 4)
test("the page-2 budget is present by name", any(s.budget_name == "b4" for s in multipage_summaries))


def endless_budgets_http_get(url, *, headers, params=None, timeout=30):
    return FakeResponse({"value": [
        {"id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/endless", "name": "endless",
         "properties": {"category": "Cost", "amount": 100, "timeGrain": "Monthly", "currentSpend": {"amount": 5, "unit": "USD"}}},
    ], "nextLink": "https://management.azure.com/next?page=999"})


bounded_summaries = cost.collect_budget_summaries("sub1", credential_factory=fake_credential_factory, http_get=endless_budgets_http_get, now=NOW, max_pages=2, max_records=100)
test("an injected max_pages bound stops a runaway nextLink chain at exactly 2 pages", len(bounded_summaries) == 2)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
