#!/usr/bin/env python3
"""Test app/operations/collectors/http.py's arm_post -- bounded retry/
backoff on a 429 (throttled) or transient 5xx ARM response (the real
live cause of Cost Management trend collection failing with HTTP 429),
Retry-After honoring, the injectable sleep_fn (tests never actually
sleep), the small hard max_retries ceiling, that any OTHER 4xx is NEVER
retried, and that the Authorization header sent on every attempt is a
real, structurally-valid `Bearer <token>` value -- never a literal
redacted placeholder -- checked structurally (regex shape) so this
test itself never needs to assert against/print the token's actual
value.

Run: python3 tests/test_operations_http_arm_post_retry.py
"""
import re
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
    def __init__(self, payload, status_code=200, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


_AUTH_HEADER_SHAPE = re.compile(r"^Bearer \S+$")


def _recording_sleep_fn(calls):
    def _sleep(seconds):
        calls.append(seconds)
    return _sleep


# ─── First success returns normally (no retry needed) ──────────────────
print("\n\U0001f9ea Test 1: arm_post -- an immediate 2xx returns normally, sends a real Bearer header, no sleep")
_headers_seen = []


def http_post_immediate_success(url, *, headers, json=None, timeout=30):
    _headers_seen.append(headers)
    return FakeResponse({"ok": True})


sleep_calls = []
result = http.arm_post(
    "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
    credential_factory=FakeCredential, http_post=http_post_immediate_success, sleep_fn=_recording_sleep_fn(sleep_calls),
)
test("returns the parsed JSON body on the first successful attempt", result == {"ok": True})
test("sleep_fn is never called when the first attempt already succeeds", sleep_calls == [])
test(
    "the Authorization header is a structurally-valid 'Bearer <token>' value, never the literal placeholder",
    bool(_AUTH_HEADER_SHAPE.match(_headers_seen[0].get("Authorization", ""))),
)
test("Content-Type is application/json", _headers_seen[0].get("Content-Type") == "application/json")


# ─── A 429 then a 200 succeeds after exactly one retry ─────────────────
print("\n\U0001f9ea Test 2: arm_post -- a single 429 followed by a 200 succeeds after exactly one retry")
_calls = []


def http_post_429_then_ok(url, *, headers, json=None, timeout=30):
    _calls.append(1)
    if len(_calls) == 1:
        return FakeResponse({}, status_code=429, text="Too Many Requests", headers={"Retry-After": "2"})
    return FakeResponse({"ok": True})


sleep_calls = []
result = http.arm_post(
    "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
    credential_factory=FakeCredential, http_post=http_post_429_then_ok, sleep_fn=_recording_sleep_fn(sleep_calls),
)
test("returns the second attempt's successful JSON body", result == {"ok": True})
test("exactly 2 requests were made (1 failure + 1 success)", len(_calls) == 2)
test("sleep_fn was called exactly once (one retry)", len(sleep_calls) == 1)
test("the injected Retry-After header (2s) was honored verbatim", sleep_calls[0] == 2.0)


# ─── A transient 503 is retried exactly like a 429 ─────────────────────
print("\n\U0001f9ea Test 3: arm_post -- a transient 503 is retried (not just 429)")
_calls = []


def http_post_503_then_ok(url, *, headers, json=None, timeout=30):
    _calls.append(1)
    if len(_calls) == 1:
        return FakeResponse({}, status_code=503, text="Service Unavailable")
    return FakeResponse({"ok": True})


sleep_calls = []
result = http.arm_post(
    "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
    credential_factory=FakeCredential, http_post=http_post_503_then_ok, sleep_fn=_recording_sleep_fn(sleep_calls),
)
test("returns normally once the transient 503 clears", result == {"ok": True})
test("sleep_fn was called once, with no Retry-After header falling back to the base backoff", sleep_calls == [1.0])


# ─── Exhausting every retry still raises an explicit error ─────────────
print("\n\U0001f9ea Test 4: arm_post -- persistent 429s exhaust max_retries and still raise explicitly (never a silent partial return)")


def http_post_always_429(url, *, headers, json=None, timeout=30):
    return FakeResponse({}, status_code=429, text="Too Many Requests")


sleep_calls = []
try:
    http.arm_post(
        "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
        credential_factory=FakeCredential, http_post=http_post_always_429, max_retries=2,
        sleep_fn=_recording_sleep_fn(sleep_calls),
    )
    test("exhausting every retry raises OperationsCollectionError instead of returning a partial/empty success", False)
except OperationsCollectionError as exc:
    test("exhausting every retry raises OperationsCollectionError instead of returning a partial/empty success", True)
    test("the error identifies the failing source", exc.source == "test_source")
test("sleep_fn was called exactly max_retries (2) times before giving up", len(sleep_calls) == 2)


# ─── Any OTHER 4xx is never retried ─────────────────────────────────────
print("\n\U0001f9ea Test 5: arm_post -- a plain 400/401/404 is never retried (a real request/auth problem, not transient)")
for status in (400, 401, 403, 404):
    _calls = []

    def http_post_client_error(url, *, headers, json=None, timeout=30, _status=status):
        _calls.append(1)
        return FakeResponse({}, status_code=_status, text="client error")

    sleep_calls = []
    try:
        http.arm_post(
            "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
            credential_factory=FakeCredential, http_post=http_post_client_error, sleep_fn=_recording_sleep_fn(sleep_calls),
        )
        test(f"a {status} raises immediately instead of retrying", False)
    except OperationsCollectionError:
        test(f"a {status} raises immediately instead of retrying", True)
    test(f"a {status} never triggers a retry -- exactly 1 request was made", len(_calls) == 1)
    test(f"a {status} never calls sleep_fn", sleep_calls == [])


# ─── max_retries is clamped to a small hard ceiling ─────────────────────
print("\n\U0001f9ea Test 6: arm_post -- a caller-requested max_retries beyond the hard ceiling is still clamped, never unbounded")
_calls = []


def http_post_always_503(url, *, headers, json=None, timeout=30):
    _calls.append(1)
    return FakeResponse({}, status_code=503, text="Service Unavailable")


sleep_calls = []
try:
    http.arm_post(
        "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
        credential_factory=FakeCredential, http_post=http_post_always_503, max_retries=10_000_000,
        sleep_fn=_recording_sleep_fn(sleep_calls),
    )
    test("a huge max_retries request is still bounded, not unbounded", False)
except OperationsCollectionError:
    test("a huge max_retries request is still bounded, not unbounded", True)
test(
    "retries never exceed the module's hard ceiling even when a caller asks for far more",
    len(sleep_calls) <= http._HARD_MAX_ARM_POST_RETRIES,
)


# ─── Auth/token-acquisition failure still raises immediately ───────────
print("\n\U0001f9ea Test 7: arm_post -- an auth/token-acquisition failure raises OperationsCollectionError, never retried")
try:
    http.arm_post(
        "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
        credential_factory=FailingCredential, http_post=http_post_immediate_success,
    )
    test("an auth failure raises OperationsCollectionError instead of returning a partial/empty success", False)
except OperationsCollectionError:
    test("an auth failure raises OperationsCollectionError instead of returning a partial/empty success", True)


# ─── A malformed/missing Retry-After header falls back to backoff ─────
print("\n\U0001f9ea Test 8: arm_post -- a malformed Retry-After header falls back to the exponential base backoff, never raises")
_calls = []


def http_post_malformed_retry_after(url, *, headers, json=None, timeout=30):
    _calls.append(1)
    if len(_calls) == 1:
        return FakeResponse({}, status_code=429, text="Too Many Requests", headers={"Retry-After": "not-a-number"})
    return FakeResponse({"ok": True})


sleep_calls = []
result = http.arm_post(
    "/subscriptions/s/providers/Microsoft.CostManagement/query", source="test_source",
    credential_factory=FakeCredential, http_post=http_post_malformed_retry_after, sleep_fn=_recording_sleep_fn(sleep_calls),
)
test("still succeeds once the malformed-Retry-After attempt clears", result == {"ok": True})
test("a malformed Retry-After never raises -- falls back to the base backoff seconds", sleep_calls == [1.0])


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
