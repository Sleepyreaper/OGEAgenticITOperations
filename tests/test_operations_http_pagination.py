#!/usr/bin/env python3
"""Test the shared, bounded `nextLink`-following pagination helper
(app/operations/collectors/http.py's `paginated_get`) -- multipage
accumulation, absolute-nextLink following, the configurable max_pages/
max_records bounds AND the hard ceilings that clamp them regardless of
what a caller asks for, explicit (never silent) truncation surfacing,
and that a genuine failure (auth/non-2xx/non-JSON) mid-pagination still
raises OperationsCollectionError exactly like a single-page arm_get/
scoped_get call would.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_http_pagination.py
"""
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import http  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402

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


class FakeToken:
    token = "fake-token"  # noqa: S105 -- test fixture, not a real credential


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


# ─── Multipage accumulation -- follows an absolute nextLink across pages ──
print("\n\U0001f9ea Test 1: paginated_get -- follows nextLink across 3 pages, accumulating every page's items")
PAGE1_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Test/things?api-version=1&page=1"
PAGE2_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Test/things?api-version=1&page=2"
PAGE3_URL = "https://management.azure.com/subscriptions/s/providers/Microsoft.Test/things?api-version=1&page=3"

_calls = []


def multipage_http_get(url, *, headers, params=None, timeout=30):
    _calls.append((url, params))
    if "page=2" in url:
        return FakeResponse({"value": [{"id": "item3"}, {"id": "item4"}], "nextLink": PAGE3_URL})
    if "page=3" in url:
        return FakeResponse({"value": [{"id": "item5"}]})
    return FakeResponse({"value": [{"id": "item1"}, {"id": "item2"}], "nextLink": PAGE2_URL})


result = http.paginated_get(
    "/subscriptions/s/providers/Microsoft.Test/things", source="test_source",
    params={"api-version": "1"}, credential_factory=FakeCredential, http_get=multipage_http_get,
)
test("accumulates all 5 items across 3 pages", [i["id"] for i in result.items] == ["item1", "item2", "item3", "item4", "item5"])
test("pages_fetched is exactly 3", result.pages_fetched == 3)
test("not truncated -- the last page had no nextLink", result.truncated is False)
test("only the FIRST request carries the original query params", _calls[0][1] == {"api-version": "1"})
test("every subsequent request's params is None (nextLink already carries its own query string)", _calls[1][1] is None and _calls[2][1] is None)
test("the second request's url is the exact absolute nextLink from page 1", _calls[1][0] == PAGE2_URL)
test("the third request's url is the exact absolute nextLink from page 2", _calls[2][0] == PAGE3_URL)


# ─── max_records bound -- stops early, reports truncated=True ─────────
print("\n\U0001f9ea Test 2: paginated_get -- a max_records bound reached mid-page truncates, and is surfaced (not silent)")


def three_item_pages_http_get(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [{"id": "b1"}, {"id": "b2"}, {"id": "b3"}], "nextLink": PAGE3_URL})
    return FakeResponse({"value": [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}], "nextLink": PAGE2_URL})


_calls.clear()
caplog_records = []
handler = logging.Handler()
handler.emit = lambda record: caplog_records.append(record)
http._logger.addHandler(handler)
http._logger.setLevel(logging.WARNING)
try:
    bounded = http.paginated_get(
        "/subscriptions/s/providers/Microsoft.Test/things", source="test_source",
        credential_factory=FakeCredential, http_get=three_item_pages_http_get, max_pages=10, max_records=4,
    )
finally:
    http._logger.removeHandler(handler)
test("stops as soon as max_records (4) is reached, never over-collecting", len(bounded.items) == 4)
test("the 4 collected items are the first 4 in page order", [i["id"] for i in bounded.items] == ["a1", "a2", "a3", "b1"])
test("truncated is True (more data existed beyond the bound)", bounded.truncated is True)
test("a warning is logged when truncation happens -- surfaced, never silent", any("test_source" in r.getMessage() for r in caplog_records))


# ─── max_pages bound -- stops even though more nextLink pages remain ──
print("\n\U0001f9ea Test 3: paginated_get -- a max_pages bound reached while more nextLink pages remain also truncates")


def endless_http_get(url, *, headers, params=None, timeout=30):
    # Always returns exactly one item and always points at a further page --
    # simulating a pathological/very-large list that never naturally ends.
    return FakeResponse({"value": [{"id": "x"}], "nextLink": "https://management.azure.com/next?page=999"})


endless_result = http.paginated_get(
    "/subscriptions/s/providers/Microsoft.Test/endless", source="test_source",
    credential_factory=FakeCredential, http_get=endless_http_get, max_pages=3, max_records=1000,
)
test("stops after exactly max_pages (3) page fetches", endless_result.pages_fetched == 3)
test("truncated is True (the last fetched page still had a nextLink)", endless_result.truncated is True)
test("collected only the 3 items actually fetched (never blocks waiting for more)", len(endless_result.items) == 3)


# ─── Hard ceiling -- a caller-requested huge bound is still clamped ────
print("\n\U0001f9ea Test 4: paginated_get -- caller-requested max_pages/max_records beyond the hard ceiling are clamped, never unbounded")
huge_bound_result = http.paginated_get(
    "/subscriptions/s/providers/Microsoft.Test/endless", source="test_source",
    credential_factory=FakeCredential, http_get=endless_http_get,
    max_pages=10_000_000, max_records=10_000_000,
)
test("page fetches never exceed the module's hard ceiling even when a caller asks for far more", huge_bound_result.pages_fetched <= http._HARD_MAX_PAGES)
test("a runaway nextLink loop is still bounded, not an infinite/unbounded crawl", huge_bound_result.truncated is True)


# ─── Explicit failures -- a genuine error mid-pagination is never swallowed ──
print("\n\U0001f9ea Test 5: paginated_get -- a non-2xx response on page 2 still raises OperationsCollectionError")


def fails_on_page2_http_get(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({}, status_code=503, text="Service Unavailable")
    return FakeResponse({"value": [{"id": "ok1"}], "nextLink": PAGE2_URL})


try:
    http.paginated_get(
        "/subscriptions/s/providers/Microsoft.Test/things", source="test_source",
        credential_factory=FakeCredential, http_get=fails_on_page2_http_get,
    )
    test("a mid-pagination 503 raises OperationsCollectionError instead of returning a partial success", False)
except OperationsCollectionError as exc:
    test("a mid-pagination 503 raises OperationsCollectionError instead of returning a partial success", True)
    test("the error identifies the failing source", exc.source == "test_source")

try:
    http.paginated_get(
        "/subscriptions/s/providers/Microsoft.Test/things", source="test_source",
        credential_factory=FailingCredential, http_get=multipage_http_get,
    )
    test("an auth failure raises OperationsCollectionError instead of returning []", False)
except OperationsCollectionError:
    test("an auth failure raises OperationsCollectionError instead of returning []", True)


# ─── Single page (no nextLink) -- never truncated, matches a plain GET ──
print("\n\U0001f9ea Test 6: paginated_get -- a single page with no nextLink is never reported truncated")
single_page_result = http.paginated_get(
    "/subscriptions/s/providers/Microsoft.Test/things", source="test_source",
    credential_factory=FakeCredential, http_get=lambda url, **kw: FakeResponse({"value": [{"id": "only1"}]}),
)
test("returns the single page's item", [i["id"] for i in single_page_result.items] == ["only1"])
test("pages_fetched is 1", single_page_result.pages_fetched == 1)
test("truncated is False -- there was genuinely nothing more", single_page_result.truncated is False)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
