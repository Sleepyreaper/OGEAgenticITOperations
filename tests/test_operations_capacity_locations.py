#!/usr/bin/env python3
"""Test the explicit, configured `CAPACITY_LOCATIONS` capacity-region
wiring added on top of Phase 1's capacity source:

  1. `OperationsConfig.capacity_locations` parsing/validation (comma-
     separated ARM region slugs, portal copy/paste normalization, strict
     rejection of anything else) and its empty/unset default.
  2. That `app/operations/routes.py` forwards it -- as
     `run_full_collection`'s `locations`/`openai_locations` kwargs, via
     an explicit `OperationsConfig.from_env()` -- for every route that
     builds a snapshot (`/snapshot`, `/brief`, `/queue`, `GET/POST
     /handoff`, `/evidence/<id>`), with no `?locations=` query-string
     override.
  3. That the Bicep infra and `.env.example` actually expose
     `CAPACITY_LOCATIONS`/`capacityLocations` end-to-end.

Run: python3 tests/test_operations_capacity_locations.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Telemetry must be provably disabled, and the workflow-state DB must
# point at a disposable, repo-local (never /tmp) test file -- both must
# be set before app.main/app.operations.snapshot import and cache
# anything from the environment. Use a distinct AZURE_SUBSCRIPTION_ID/DB
# path from tests/test_operations_routes.py so the two files never share
# process-wide singleton state if a test runner imports both.
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ.pop("CAPACITY_LOCATIONS", None)
os.environ["AZURE_SUBSCRIPTION_ID"] = "sub-test-capacity-locations"
DB_PATH = str(REPO_ROOT / "tests" / "_test_operations_capacity_locations.db")
os.environ["OPERATIONS_STATE_DB"] = DB_PATH


def _cleanup_db():
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)


_cleanup_db()

from app.main import create_app  # noqa: E402
from app.operations import routes as routes_mod  # noqa: E402
from app.operations.config import OperationsConfig, OperationsConfigError  # noqa: E402
from app.operations.models import (  # noqa: E402
    ConfidenceLevel, EvidenceReference, EvidenceSource, Finding, FindingCategory, FindingStatus, Severity,
)
from app.operations.snapshot import OperationsSnapshot  # noqa: E402

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


# ─── Test 1: OperationsConfig.capacity_locations parsing/validation ────
print("\n\U0001f9ea Test 1: OperationsConfig.capacity_locations -- parsing/validation/empty state")

test("default (unset) is an empty tuple -- not_configured, never auto-discovered", OperationsConfig().capacity_locations == ())

os.environ["CAPACITY_LOCATIONS"] = ""
test("blank env var parses to an empty tuple", OperationsConfig.from_env().capacity_locations == ())

os.environ["CAPACITY_LOCATIONS"] = "eastus2,westeurope"
test(
    "plain lowercase slugs parse through unchanged",
    OperationsConfig.from_env().capacity_locations == ("eastus2", "westeurope"),
)

os.environ["CAPACITY_LOCATIONS"] = " East US 2 , West Europe ,UKSouth"
test(
    "portal copy/paste variants (mixed case, spaces as word separators, extra whitespace) normalize to slugs",
    OperationsConfig.from_env().capacity_locations == ("eastus2", "westeurope", "uksouth"),
)

os.environ["CAPACITY_LOCATIONS"] = "south-africa-north"
test(
    "hyphenated variants normalize to slugs",
    OperationsConfig.from_env().capacity_locations == ("southafricanorth",),
)

os.environ["CAPACITY_LOCATIONS"] = "eastus2,,  ,westeurope"
test(
    "blank entries between commas are dropped, not turned into an error",
    OperationsConfig.from_env().capacity_locations == ("eastus2", "westeurope"),
)

for bad_value in ("not_a_region!!", "123eastus", "eastus_2", "eastus2/prod"):
    os.environ["CAPACITY_LOCATIONS"] = bad_value
    try:
        OperationsConfig.from_env()
        test(f"{bad_value!r} raises OperationsConfigError (never silently coerced/dropped)", False)
    except OperationsConfigError:
        test(f"{bad_value!r} raises OperationsConfigError (never silently coerced/dropped)", True)

os.environ.pop("CAPACITY_LOCATIONS", None)


# ─── Test 2: route forwarding -- captures the exact kwargs get_snapshot receives ──
print("\n\U0001f9ea Test 2: app/operations/routes.py forwards CAPACITY_LOCATIONS to every snapshot-building route")

FINDING = Finding(
    category=FindingCategory.CAPACITY.value, severity=Severity.MEDIUM.value, status=FindingStatus.OPEN.value,
    title="Capacity finding", summary="s", business_impact="b",
    first_seen="2025-06-01T00:00:00Z", last_seen="2025-06-01T00:00:00Z",
    source=EvidenceSource.ARM_COMPUTE_USAGE.value, confidence=ConfidenceLevel.CONFIRMED.value,
    evidence=[EvidenceReference(source=EvidenceSource.ARM_COMPUTE_USAGE.value, title="t", observed_at="2025-06-01T00:00:00Z")],
    executive_attention=False, approval_required=False, discriminator="cap-locations",
)

CANNED_SNAPSHOT = OperationsSnapshot(
    id="snap-capacity-locations", generated_at="2025-06-01T00:00:00.000Z",
    subscription_ids=("sub-test-capacity-locations",), status="ok", envelopes=[],
    findings=[{
        "finding": FINDING.to_dict(),
        "workflow": {
            "status": "new", "assigned_owner": "", "disposition_reason": "", "snooze_until": None,
            "first_seen_at": None, "created_at": None, "updated_at": None,
        },
        "priority": {"band": "P4", "factors": {
            "customer_impact": False, "severity_rank": 2, "slo_state": None, "slo_state_rank": 2,
            "age_hours": 1.0, "confidence_rank": 0,
        }},
    }],
    coverage={"total_sources": 0, "sources_by_status": {"ok": [], "error": [], "not_configured": [], "not_supported": []}},
    source_errors=[], summary={"total_findings": 1},
)

captured = {}


def fake_get_snapshot(sub_ids, **kwargs):
    captured["sub_ids"] = sub_ids
    captured["kwargs"] = kwargs
    return CANNED_SNAPSHOT


routes_mod.get_snapshot = fake_get_snapshot

app = create_app()
client = app.test_client()

os.environ.pop("CAPACITY_LOCATIONS", None)
resp = client.get("/api/operations/snapshot")
test("GET /snapshot with no CAPACITY_LOCATIONS -> 200", resp.status_code == 200)
test("an explicit OperationsConfig is forwarded to get_snapshot", isinstance(captured["kwargs"].get("config"), OperationsConfig))
test(
    "full_collect_kwargs is empty -- capacity stays not_configured, exactly like calling run_full_collection with no locations",
    captured["kwargs"].get("full_collect_kwargs") == {},
)

os.environ["CAPACITY_LOCATIONS"] = "East US 2, westeurope"
EXPECTED_KWARGS = {"locations": ["eastus2", "westeurope"], "openai_locations": ["eastus2", "westeurope"]}

for path in ("/api/operations/snapshot", "/api/operations/brief", "/api/operations/queue", "/api/operations/handoff"):
    resp = client.get(path)
    test(f"GET {path} -> 200 with CAPACITY_LOCATIONS set", resp.status_code == 200)
    test(f"GET {path} forwards locations/openai_locations derived from CAPACITY_LOCATIONS", captured["kwargs"].get("full_collect_kwargs") == EXPECTED_KWARGS)
    test(f"GET {path}'s forwarded config.capacity_locations is normalized", captured["kwargs"]["config"].capacity_locations == ("eastus2", "westeurope"))

resp = client.get(f"/api/operations/evidence/{FINDING.id}")
test("GET /evidence/<id> -> 200", resp.status_code == 200)
test("GET /evidence/<id> also forwards locations/openai_locations", captured["kwargs"].get("full_collect_kwargs") == EXPECTED_KWARGS)

resp = client.post("/api/operations/handoff", json={"created_by": "alice"})
test("POST /handoff -> 201", resp.status_code == 201)
test("POST /handoff also forwards locations/openai_locations (not just the GET route)", captured["kwargs"].get("full_collect_kwargs") == EXPECTED_KWARGS)

os.environ["CAPACITY_LOCATIONS"] = "not-a-valid-region!!"
resp = client.get("/api/operations/snapshot")
test("a malformed CAPACITY_LOCATIONS -> 502 (OperationsConfigError propagated, never silently ignored)", resp.status_code == 502)

os.environ.pop("CAPACITY_LOCATIONS", None)


# ─── Test 3: Bicep/env mapping -- CAPACITY_LOCATIONS is actually wired end-to-end ──
print("\n\U0001f9ea Test 3: CAPACITY_LOCATIONS/capacityLocations are wired through .env.example and the Bicep infra")

env_example = (REPO_ROOT / ".env.example").read_text()
test(".env.example documents CAPACITY_LOCATIONS", "CAPACITY_LOCATIONS=" in env_example)

main_bicep = (REPO_ROOT / "infra" / "main.bicep").read_text()
test("infra/main.bicep's operationsSettings description lists capacityLocations", "capacityLocations" in main_bicep)

web_app_bicep = (REPO_ROOT / "infra" / "modules" / "web-app.bicep").read_text()
test("infra/modules/web-app.bicep's operationsSettings description lists capacityLocations", "capacityLocations" in web_app_bicep)
test(
    "infra/modules/web-app.bicep maps capacityLocations -> CAPACITY_LOCATIONS",
    "capacityLocations: 'CAPACITY_LOCATIONS'" in web_app_bicep,
)

docs_azure_sources = (REPO_ROOT / "docs" / "AZURE_DATA_SOURCES.md").read_text()
test("docs/AZURE_DATA_SOURCES.md documents CAPACITY_LOCATIONS for the capacity source", "CAPACITY_LOCATIONS" in docs_azure_sources)

docs_operations_api = (REPO_ROOT / "docs" / "OPERATIONS_API.md").read_text()
test("docs/OPERATIONS_API.md documents CAPACITY_LOCATIONS route forwarding", "CAPACITY_LOCATIONS" in docs_operations_api)


_cleanup_db()

# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
