#!/usr/bin/env python3
"""Test the optional `OPENAI_CAPACITY_NAME_FILTERS` capacity-noise
filter added on top of the capacity source:

  1. `app.operations.collectors.capacity.collect_openai_capacity`'s
     `name_filters` -- case-insensitive substring filtering applied
     BEFORE threshold normalization, empty/None means no filtering, and
     it is NEVER applied to `collect_compute_capacity`.
  2. `OperationsConfig.openai_capacity_name_filters` parsing (a plain
     comma-separated list, like the other Phase 2 list fields) and its
     empty/unset default.
  3. That `app.operations.service.collect_capacity_envelope` forwards
     `config.openai_capacity_name_filters` into `collect_openai_capacity`
     as `name_filters` -- and that `app/operations/routes.py` builds
     that same `OperationsConfig` from the environment for every
     snapshot-building route with no extra wiring required.
  4. That the Bicep infra and `.env.example` actually expose
     `OPENAI_CAPACITY_NAME_FILTERS`/`openAiCapacityNameFilters`
     end-to-end.

Run: python3 tests/test_operations_capacity_name_filters.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ.pop("OPENAI_CAPACITY_NAME_FILTERS", None)

from app.operations.collectors import capacity  # noqa: E402
from app.operations.config import OperationsConfig  # noqa: E402
from app.operations import service  # noqa: E402

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


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


MIXED_QUOTA_PAYLOAD = {"value": [
    {"name": {"value": "gpt-5.6-Capacity", "localizedValue": "gpt-5.6-Capacity"}, "currentValue": 10, "limit": 100, "unit": "Count"},
    {"name": {"value": "OpenAI.Standard.claude-opus", "localizedValue": "OpenAI.Standard.claude-opus"}, "currentValue": 100, "limit": 100, "unit": "Count"},
    {"name": {"value": "OpenAI.Standard.dall-e-3", "localizedValue": "OpenAI.Standard.dall-e-3"}, "currentValue": 50, "limit": 50, "unit": "Count"},
]}

COMPUTE_PAYLOAD = {"value": [
    {"name": {"value": "standardDSv3Family", "localizedValue": "standardDSv3Family"}, "currentValue": 95, "limit": 100, "unit": "Count"},
]}


def fake_http_get_factory(payload):
    def _fake_http_get(url, *, headers, params=None, timeout=30):
        return FakeResponse(payload)
    return _fake_http_get


# ─── Test 1: collect_openai_capacity -- name_filters is case-insensitive, pre-normalization ──
print("\n\U0001f9ea Test 1: collect_openai_capacity -- name_filters narrows results BEFORE threshold normalization")
unfiltered = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD), now=NOW,
)
test("with no name_filters, every quota is kept (unchanged default behavior)", len(unfiltered) == 3)

filtered = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD), now=NOW,
    name_filters=("gpt-5.6",),
)
test("with name_filters=('gpt-5.6',), only the matching quota is kept", len(filtered) == 1)
test("the kept quota is the gpt-5.6 one, not the unrelated Claude/image quotas", filtered[0].metric == "gpt-5.6-Capacity")

filtered_case_insensitive = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD), now=NOW,
    name_filters=("GPT-5.6",),
)
test("matching is case-insensitive", len(filtered_case_insensitive) == 1)

filtered_multi = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD), now=NOW,
    name_filters=("gpt-5.6", "dall-e"),
)
test("multiple filter terms are OR'd together", len(filtered_multi) == 2)

filtered_none_match = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD), now=NOW,
    name_filters=("nonexistent-model",),
)
test("a filter matching nothing produces zero summaries (not an error)", filtered_none_match == [])

filtered_blank = capacity.collect_openai_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD), now=NOW,
    name_filters=(),
)
test("an empty name_filters tuple means no filtering, same as None", len(filtered_blank) == 3)


# ─── Test 2: name_filters is NEVER applied to collect_compute_capacity ──
print("\n\U0001f9ea Test 2: collect_compute_capacity has no name_filters parameter -- Compute usages are never filtered by this setting")
import inspect  # noqa: E402
compute_params = set(inspect.signature(capacity.collect_compute_capacity).parameters)
test("collect_compute_capacity's signature has no name_filters parameter", "name_filters" not in compute_params)
compute_summaries = capacity.collect_compute_capacity(
    "sub1", ["eastus"], credential_factory=FakeCredential, http_get=fake_http_get_factory(COMPUTE_PAYLOAD), now=NOW,
)
test("Compute usages are collected in full regardless of any OpenAI name filter", len(compute_summaries) == 1)


# ─── Test 3: OperationsConfig.openai_capacity_name_filters -- parsing/default ──
print("\n\U0001f9ea Test 3: OperationsConfig.openai_capacity_name_filters -- parsing/empty default")
test("default (unset) is an empty tuple -- no filtering, all quotas kept", OperationsConfig().openai_capacity_name_filters == ())

os.environ["OPENAI_CAPACITY_NAME_FILTERS"] = ""
test("a blank env var parses to an empty tuple", OperationsConfig.from_env().openai_capacity_name_filters == ())

os.environ["OPENAI_CAPACITY_NAME_FILTERS"] = "gpt-5.6"
test("a single filter term parses to a 1-tuple", OperationsConfig.from_env().openai_capacity_name_filters == ("gpt-5.6",))

os.environ["OPENAI_CAPACITY_NAME_FILTERS"] = "gpt-5.6, gpt-4o ,  claude"
test(
    "comma-separated entries are stripped, matching _parse_csv_list's convention",
    OperationsConfig.from_env().openai_capacity_name_filters == ("gpt-5.6", "gpt-4o", "claude"),
)
os.environ.pop("OPENAI_CAPACITY_NAME_FILTERS", None)


# ─── Test 4: collect_capacity_envelope forwards config.openai_capacity_name_filters ──
print("\n\U0001f9ea Test 4: collect_capacity_envelope -- forwards config.openai_capacity_name_filters into collect_openai_capacity")
captured_kwargs = {}
_real_collect_openai_capacity = capacity.collect_openai_capacity


def spy_collect_openai_capacity(*args, **kwargs):
    captured_kwargs["name_filters"] = kwargs.get("name_filters")
    return _real_collect_openai_capacity(*args, **kwargs)


service.capacity_collector.collect_openai_capacity = spy_collect_openai_capacity
try:
    cfg = OperationsConfig(openai_capacity_name_filters=("gpt-5.6",))
    envelope = service.collect_capacity_envelope(
        ["sub1"], ["eastus"], cfg,
        credential_factory=FakeCredential, http_get=fake_http_get_factory(MIXED_QUOTA_PAYLOAD),
    )
finally:
    service.capacity_collector.collect_openai_capacity = _real_collect_openai_capacity

test("collect_capacity_envelope forwards config.openai_capacity_name_filters as name_filters", captured_kwargs.get("name_filters") == ("gpt-5.6",))
test("the envelope still collects successfully (ok status)", envelope.status == "ok")


# ─── Test 5: Bicep/env mapping -- OPENAI_CAPACITY_NAME_FILTERS is wired end-to-end ──
print("\n\U0001f9ea Test 5: OPENAI_CAPACITY_NAME_FILTERS/openAiCapacityNameFilters are wired through .env.example, Bicep, and docs")

env_example = (REPO_ROOT / ".env.example").read_text()
test(".env.example documents OPENAI_CAPACITY_NAME_FILTERS", "OPENAI_CAPACITY_NAME_FILTERS=" in env_example)

main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
test("infra/main.bicep's operationsSettings description lists openAiCapacityNameFilters", "openAiCapacityNameFilters" in main_bicep)

web_app_bicep = (REPO_ROOT / "infra" / "modules" / "web-app.bicep").read_text()
test("infra/modules/web-app.bicep's operationsSettings description lists openAiCapacityNameFilters", "openAiCapacityNameFilters" in web_app_bicep)
test(
    "infra/modules/web-app.bicep maps openAiCapacityNameFilters -> OPENAI_CAPACITY_NAME_FILTERS",
    "openAiCapacityNameFilters: 'OPENAI_CAPACITY_NAME_FILTERS'" in web_app_bicep,
)

docs_azure_sources = (REPO_ROOT / "docs" / "AZURE_DATA_SOURCES.md").read_text()
test("docs/AZURE_DATA_SOURCES.md documents OPENAI_CAPACITY_NAME_FILTERS for the capacity source", "OPENAI_CAPACITY_NAME_FILTERS" in docs_azure_sources)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
