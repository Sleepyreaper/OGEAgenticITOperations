#!/usr/bin/env python3
"""Test regional capacity/quota collection, threshold classification, and
the deterministic (never-fabricated) exhaustion forecast
(app/operations/collectors/capacity.py).

No real network/Azure calls -- credential_factory and http_get are
injected fakes.

Run: python3 tests/test_operations_capacity.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import capacity  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402
from app.operations.models import ConfidenceLevel, FindingCategory  # noqa: E402

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


NOW = datetime.now(timezone.utc)


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


def usages_payload(current, limit, name="standardDSv3Family Family vCPUs", unit="Count"):
    return {"value": [{"name": {"value": name, "localizedValue": name}, "currentValue": current, "limit": limit, "unit": unit}]}


def fake_http_get_factory(payload, status_code=200):
    def _fake_http_get(url, *, headers, params=None, timeout=30):
        return FakeResponse(payload, status_code=status_code)
    return _fake_http_get


# ─── compute_exhaustion_forecast -- deterministic, never fabricated ───
print("\n\U0001f9ea Test 1: compute_exhaustion_forecast -- no fake forecast without enough history")
test("zero history points -> not_available", capacity.compute_exhaustion_forecast([], limit=100, now=NOW).state == "not_available")
test("one history point -> not_available", capacity.compute_exhaustion_forecast([(NOW, 50)], limit=100, now=NOW).state == "not_available")
test("a non-positive limit -> not_available", capacity.compute_exhaustion_forecast([(NOW, 50), (NOW, 60)], limit=0, now=NOW).state == "not_available")

identical_x = [(NOW, 10), (NOW, 20)]  # zero-variance x-axis (same timestamp twice)
test("zero time-variance across points -> not_available (can't fit a trend)", capacity.compute_exhaustion_forecast(identical_x, limit=100, now=NOW).state == "not_available")

print("\n\U0001f9ea Test 2: compute_exhaustion_forecast -- increasing trend produces a real forecast")
increasing = [(NOW - timedelta(hours=h), 50 + (10 - h) * 2) for h in range(10, -1, -2)]
forecast = capacity.compute_exhaustion_forecast(increasing, limit=100, now=NOW)
test("state is 'available'", forecast.state == "available")
test("slope_per_hour is positive", forecast.slope_per_hour > 0)
test("exhaustion_at is a canonical UTC ISO timestamp", forecast.exhaustion_at.endswith("Z"))
test("exhaustion_at is in the future for a trend that hasn't yet crossed the limit", forecast.exhaustion_at > NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

print("\n\U0001f9ea Test 3: compute_exhaustion_forecast -- flat/decreasing trend is 'not_applicable', not a fake forecast")
decreasing = [(NOW - timedelta(hours=h), 100 - (10 - h) * 2) for h in range(10, -1, -2)]
dec_forecast = capacity.compute_exhaustion_forecast(decreasing, limit=100, now=NOW)
test("a decreasing trend is 'not_applicable'", dec_forecast.state == "not_applicable")
test("not_applicable never carries an exhaustion_at", dec_forecast.exhaustion_at is None)

flat = [(NOW - timedelta(hours=h), 50) for h in range(4, -1, -1)]
flat_forecast = capacity.compute_exhaustion_forecast(flat, limit=100, now=NOW)
test("a perfectly flat trend is 'not_applicable' (slope == 0, not > 0)", flat_forecast.state == "not_applicable")


# ─── collect_compute_capacity -- threshold classification ─────────────
print("\n\U0001f9ea Test 4: collect_compute_capacity -- threshold classification (healthy/warning/critical)")
healthy = capacity.collect_compute_capacity(
    "sub1", ["eastus"], warning_pct=75.0, critical_pct=90.0,
    credential_factory=FakeCredential, http_get=fake_http_get_factory(usages_payload(50, 100)), now=NOW,
)
test("50/100 (50%) is healthy", healthy[0].threshold_state == "healthy")

warning = capacity.collect_compute_capacity(
    "sub1", ["eastus"], warning_pct=75.0, critical_pct=90.0,
    credential_factory=FakeCredential, http_get=fake_http_get_factory(usages_payload(80, 100)), now=NOW,
)
test("80/100 (80%) is warning", warning[0].threshold_state == "warning")

critical = capacity.collect_compute_capacity(
    "sub1", ["eastus"], warning_pct=75.0, critical_pct=90.0,
    credential_factory=FakeCredential, http_get=fake_http_get_factory(usages_payload(95, 100)), now=NOW,
)
test("95/100 (95%) is critical", critical[0].threshold_state == "critical")
test("headroom_pct is computed", critical[0].headroom_pct == 5.0)
test("resource_scope is prefixed by location", critical[0].resource_scope == "compute:eastus")

unknown = capacity.collect_compute_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(usages_payload(0, 0)), now=NOW,
)
test("a zero limit is 'unknown', never a fabricated percentage", unknown[0].threshold_state == "unknown")
test("headroom_pct is None when limit is 0", unknown[0].headroom_pct is None)

try:
    capacity.collect_compute_capacity("sub1", [], credential_factory=FakeCredential, http_get=fake_http_get_factory({}))
    test("empty locations raises ValueError", False)
except ValueError:
    test("empty locations raises ValueError", True)

try:
    capacity.collect_compute_capacity("sub1", ["eastus"], credential_factory=FailingCredential, http_get=fake_http_get_factory({}))
    test("an auth failure raises OperationsCollectionError, not an empty list", False)
except OperationsCollectionError:
    test("an auth failure raises OperationsCollectionError, not an empty list", True)


# ─── collect_compute_capacity -- history_provider wiring (forecast) ───
print("\n\U0001f9ea Test 5: collect_compute_capacity -- forecast only appears when a history_provider is injected")
no_history = capacity.collect_compute_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(usages_payload(80, 100)), now=NOW,
)
test("forecast_state is 'not_available' with no history_provider injected", no_history[0].forecast_state == "not_available")


def history_provider(scope_key):
    return [(NOW - timedelta(hours=h), 50 + (10 - h) * 3) for h in range(10, -1, -2)]


with_history = capacity.collect_compute_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(usages_payload(80, 100)),
    history_provider=history_provider, now=NOW,
)
test("forecast_state is 'available' once a history_provider is injected", with_history[0].forecast_state == "available")
test("forecast_exhaustion_at is set alongside 'available'", with_history[0].forecast_exhaustion_at is not None)


# ─── collect_openai_capacity -- same shape, distinct source/scope ─────
print("\n\U0001f9ea Test 6: collect_openai_capacity -- Cognitive Services usages, distinct resource_scope")
openai_summaries = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential,
    http_get=fake_http_get_factory(usages_payload(10, 100, name="OpenAI.Standard.gpt-4")), now=NOW,
)
test("resource_scope is prefixed with 'openai:'", openai_summaries[0].resource_scope == "openai:eastus")
test("metric name is preserved", openai_summaries[0].metric == "OpenAI.Standard.gpt-4")


# ─── capacity_summaries_to_findings ────────────────────────────────────
print("\n\U0001f9ea Test 7: capacity_summaries_to_findings -- Findings only for warning/critical, plus near-term forecasts")
findings = capacity.capacity_summaries_to_findings(healthy + warning + critical)
test("exactly two Findings (one for warning, one for critical)", len(findings) == 2)
test("all capacity Findings use category capacity", all(f.category == FindingCategory.CAPACITY.value for f in findings))
test("confidence is derived (deterministic threshold math, not a raw platform fact)", all(f.confidence == ConfidenceLevel.DERIVED.value for f in findings))

near_term_forecast_summary = with_history[0]
forecast_findings = capacity.capacity_summaries_to_findings([near_term_forecast_summary])
test(
    "a near-term forecast produces its own Finding with confidence=estimated",
    any(f.confidence == ConfidenceLevel.ESTIMATED.value for f in forecast_findings),
)


# ─── Multipage/bounded pagination -- usages endpoints follow nextLink ──
print("\n\U0001f9ea Test 8: collect_compute_capacity/collect_openai_capacity -- follow nextLink across multiple pages, and honor an injected bound")
USAGES_PAGE2_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Compute/locations/eastus/usages?api-version=x&page=2"


def multipage_usages_http_get(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [{"name": {"value": "standardDv3Family"}, "currentValue": 5, "limit": 50, "unit": "Count"}]})
    return FakeResponse({"value": [{"name": {"value": "standardDSv3Family"}, "currentValue": 10, "limit": 100, "unit": "Count"}], "nextLink": USAGES_PAGE2_URL})


multipage_summaries = capacity.collect_compute_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=multipage_usages_http_get, now=NOW,
)
test("collects usage entries from both pages, not just the first", {s.metric for s in multipage_summaries} == {"standardDSv3Family", "standardDv3Family"})


def endless_usages_http_get(url, *, headers, params=None, timeout=30):
    return FakeResponse({"value": [{"name": {"value": "endlessFamily"}, "currentValue": 1, "limit": 10, "unit": "Count"}], "nextLink": "https://management.azure.com/next?page=999"})


bounded_summaries = capacity.collect_compute_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=endless_usages_http_get, now=NOW, max_pages=2, max_records=100,
)
test("an injected max_pages bound stops a runaway nextLink chain at exactly 2 pages", len(bounded_summaries) == 2)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
