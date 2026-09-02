#!/usr/bin/env python3
"""Test the Phase 2 additions to app/operations/config.py -- strict
parsing/validation for the new lookback/threshold/flag/list fields
(budget thresholds, feature enable flags, comma-separated resource
lists), and that OperationsConfig.from_env() round-trips them correctly.

Run: python3 tests/test_operations_config_phase2.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.config import (  # noqa: E402
    DEFAULT_TELEMETRY_MONITORED_RESOURCE_TYPES,
    OperationsConfig,
    OperationsConfigError,
)

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


_PHASE2_ENV_VARS = [
    "ENABLE_DEFENDER_ALERTS", "ENABLE_DEFENDER_ASSESSMENTS", "COST_BUDGET_WARNING_PCT", "COST_BUDGET_CRITICAL_PCT",
    "COST_TREND_LOOKBACK_DAYS", "COST_TREND_GROWTH_PCT_THRESHOLD", "ENABLE_COST_MANAGEMENT_BUDGET",
    "ENABLE_COST_MANAGEMENT_TREND", "BACKUP_LOOKBACK_HOURS", "BACKUP_STALE_RECOVERY_POINT_DAYS", "ENABLE_BACKUP",
    "PATCH_ASSESSMENT_STALE_DAYS", "ENABLE_UPDATE_MANAGER", "KEY_VAULT_EXPIRY_WARNING_DAYS", "KEY_VAULT_MONITOR_URIS",
    "KEY_VAULT_MAX_ITEMS_PER_TYPE", "ENABLE_KEY_VAULT_EXPIRY", "AUTOMATION_LOOKBACK_HOURS", "AUTOMATION_ACCOUNT_IDS",
    "ENABLE_AUTOMATION", "TELEMETRY_MONITORED_RESOURCE_TYPES", "TELEMETRY_CRITICAL_RESOURCE_IDS",
    "TELEMETRY_MAX_RESOURCES", "TELEMETRY_HEARTBEAT_LOOKBACK_HOURS", "ENABLE_TELEMETRY_COVERAGE",
    "RETIREMENT_WARNING_DAYS", "ENABLE_RETIREMENT_ADVISORIES",
]


def _clear_phase2_env():
    for name in _PHASE2_ENV_VARS:
        os.environ.pop(name, None)


_clear_phase2_env()

# ─── Defaults -- every Phase 2 knob has a safe, bounded default ────────
print("\n\U0001f9ea Test 1: OperationsConfig() defaults -- safe, bounded, every enable_* flag defaults True")
default_config = OperationsConfig()
test("every enable_* flag defaults to True (the source runs whenever it has input, not opt-in)", all([
    default_config.enable_defender_alerts, default_config.enable_defender_assessments,
    default_config.enable_cost_management_budget, default_config.enable_cost_management_trend,
    default_config.enable_backup, default_config.enable_update_manager, default_config.enable_key_vault_expiry,
    default_config.enable_automation, default_config.enable_telemetry_coverage, default_config.enable_retirement_advisories,
]))
test("key_vault_monitor_uris/automation_account_ids/telemetry_critical_resource_ids default to empty tuples (bounded, safe)", (
    default_config.key_vault_monitor_uris == () and default_config.automation_account_ids == ()
    and default_config.telemetry_critical_resource_ids == ()
))
test("telemetry_monitored_resource_types defaults to the curated built-in allowlist, not 'everything'", (
    default_config.telemetry_monitored_resource_types == DEFAULT_TELEMETRY_MONITORED_RESOURCE_TYPES
    and len(DEFAULT_TELEMETRY_MONITORED_RESOURCE_TYPES) < 20
))
test("cost_budget_warning_pct < cost_budget_critical_pct by default", default_config.cost_budget_warning_pct < default_config.cost_budget_critical_pct)
test("telemetry_max_resources has a small, bounded default (never unbounded)", 0 < default_config.telemetry_max_resources <= 200)

# ─── Validation -- ordering/positivity constraints ─────────────────────
print("\n\U0001f9ea Test 2: OperationsConfig validation -- budget threshold ordering and positivity")
try:
    OperationsConfig(cost_budget_warning_pct=100, cost_budget_critical_pct=80)
    test("cost_budget_warning_pct >= cost_budget_critical_pct raises OperationsConfigError", False)
except OperationsConfigError:
    test("cost_budget_warning_pct >= cost_budget_critical_pct raises OperationsConfigError", True)

try:
    OperationsConfig(cost_budget_critical_pct=120)  # legitimately allowed -- a budget CAN be over-spent
    test("cost_budget_critical_pct > 100 is allowed (a budget can be over-spent, unlike a capacity percentage)", True)
except OperationsConfigError:
    test("cost_budget_critical_pct > 100 is allowed (a budget can be over-spent, unlike a capacity percentage)", False)

for field_name in ("cost_trend_lookback_days", "backup_lookback_hours", "backup_stale_recovery_point_days",
                    "patch_assessment_stale_days", "key_vault_expiry_warning_days", "key_vault_max_items_per_type",
                    "automation_lookback_hours", "telemetry_max_resources", "telemetry_heartbeat_lookback_hours",
                    "retirement_warning_days"):
    try:
        OperationsConfig(**{field_name: 0})
        test(f"{field_name}=0 raises OperationsConfigError", False)
    except OperationsConfigError:
        test(f"{field_name}=0 raises OperationsConfigError", True)

# ─── from_env() -- strict parsing for booleans and comma-separated lists ──
print("\n\U0001f9ea Test 3: OperationsConfig.from_env() -- boolean flags and comma-separated resource lists")
os.environ["ENABLE_BACKUP"] = "false"
os.environ["ENABLE_DEFENDER_ALERTS"] = "true"
os.environ["KEY_VAULT_MONITOR_URIS"] = "https://kv1.vault.azure.net/, https://kv2.vault.azure.net/"
os.environ["AUTOMATION_ACCOUNT_IDS"] = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Automation/automationAccounts/aa1"
os.environ["TELEMETRY_CRITICAL_RESOURCE_IDS"] = ""
os.environ["COST_BUDGET_WARNING_PCT"] = "80"
os.environ["COST_BUDGET_CRITICAL_PCT"] = "100"

env_config = OperationsConfig.from_env()
test("ENABLE_BACKUP=false parses to False", env_config.enable_backup is False)
test("ENABLE_DEFENDER_ALERTS=true parses to True", env_config.enable_defender_alerts is True)
test("KEY_VAULT_MONITOR_URIS is split into a stripped tuple of 2 URIs", env_config.key_vault_monitor_uris == (
    "https://kv1.vault.azure.net/", "https://kv2.vault.azure.net/",
))
test("AUTOMATION_ACCOUNT_IDS with a single entry parses to a 1-tuple", len(env_config.automation_account_ids) == 1)
test("an unset/blank TELEMETRY_CRITICAL_RESOURCE_IDS parses to an empty tuple", env_config.telemetry_critical_resource_ids == ())

os.environ["ENABLE_BACKUP"] = "not_a_bool"
try:
    OperationsConfig.from_env()
    test("an unrecognized boolean value raises OperationsConfigError (never silently coerced)", False)
except OperationsConfigError:
    test("an unrecognized boolean value raises OperationsConfigError (never silently coerced)", True)

_clear_phase2_env()


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
