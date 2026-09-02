"""Environment-driven configuration for the operations evidence layer.

Mirrors app/config.py's strict-parsing convention: a malformed value
raises immediately (OperationsConfigError, a ValueError subclass) rather
than silently falling back to a guessed default. An *unset* value falls
back to the documented default below -- see .env.example for the
corresponding environment variable names.
"""

import os
from dataclasses import dataclass
from typing import Tuple

__all__ = ["OperationsConfig", "OperationsConfigError"]


class OperationsConfigError(ValueError):
    """A malformed operations-layer environment variable."""


def _parse_positive_int(value: str, env_name: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise OperationsConfigError(f"{env_name}: expected a positive integer, got {value!r}.") from exc
    if parsed <= 0:
        raise OperationsConfigError(f"{env_name}: expected a positive integer, got {parsed}.")
    return parsed


def _parse_pct(value: str, env_name: str) -> float:
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise OperationsConfigError(f"{env_name}: expected a number, got {value!r}.") from exc
    if not (0 < parsed <= 100):
        raise OperationsConfigError(f"{env_name}: expected a percentage in (0, 100], got {parsed}.")
    return parsed


def _parse_positive_float(value: str, env_name: str) -> float:
    """Like _parse_pct, but for thresholds that are legitimately allowed
    to exceed 100 (e.g. a budget critical threshold of 120% -- 20%
    over-budget -- or a cost-trend growth threshold)."""
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise OperationsConfigError(f"{env_name}: expected a number, got {value!r}.") from exc
    if parsed <= 0:
        raise OperationsConfigError(f"{env_name}: expected a positive number, got {parsed}.")
    return parsed


def _parse_bool(value: str, env_name: str) -> bool:
    key = value.strip().lower()
    if key in ("true", "1", "yes", "on"):
        return True
    if key in ("false", "0", "no", "off"):
        return False
    raise OperationsConfigError(f"{env_name}: expected a boolean (true/false), got {value!r}.")


def _parse_csv_list(value: str) -> Tuple[str, ...]:
    """Comma-separated list env var -> a tuple of stripped, non-empty
    entries. An unset/blank env var yields an empty tuple -- that is a
    valid, common state (see each collector's not_configured semantics
    when its required list is empty), never an error."""
    return tuple(item.strip() for item in value.split(",") if item.strip())


# A curated, deliberately small default allowlist of resource types that
# (a) commonly carry customer/business-critical data or availability risk
# and (b) reliably support Microsoft.Insights/diagnosticSettings -- never
# "every resource type in the subscription" (see
# collectors.telemetry.collect_diagnostic_settings_coverage). Matches the
# list already used by app.azure_data.get_deep_analysis's monitorable
# resource-type check, for consistency across the codebase.
DEFAULT_TELEMETRY_MONITORED_RESOURCE_TYPES: Tuple[str, ...] = (
    "Microsoft.Compute/virtualMachines",
    "Microsoft.Web/sites",
    "Microsoft.Sql/servers",
    "Microsoft.KeyVault/vaults",
    "Microsoft.Storage/storageAccounts",
    "Microsoft.Network/applicationGateways",
    "Microsoft.ContainerService/managedClusters",
    "Microsoft.DBforPostgreSQL/flexibleServers",
)


@dataclass
class OperationsConfig:
    """Bounded knobs for the collectors in app/operations/collectors/.

    Construct directly for tests (all fields have safe defaults); use
    `OperationsConfig.from_env()` in real collection runs.
    """
    alert_lookback_hours: int = 24
    change_lookback_hours: int = 24
    change_correlation_window_minutes: int = 60
    capacity_warning_pct: float = 75.0
    capacity_critical_pct: float = 90.0
    # Exactly one of these configures workload SLOs; both empty (the
    # default) means "no SLOs configured" -- the SLO collector reports an
    # explicit not_configured state, never a fabricated uptime number.
    slo_definitions_path: str = ""
    slo_definitions_json: str = ""

    # ── Phase 2: operational risk/hygiene collectors ──────────────────
    # Each `enable_*` flag lets an operator deliberately turn a source off
    # (its envelope then reports not_configured with a "disabled via
    # config" message) even when the inputs it would otherwise need are
    # present -- distinct from not_configured due to a missing input.

    # Microsoft Defender for Cloud. No extra inputs needed beyond the
    # subscription id already passed to every collector.
    enable_defender_alerts: bool = True
    enable_defender_assessments: bool = True

    # Azure Cost Management. Budget thresholds compare each budget's
    # currentSpend/amount ratio (Microsoft.Consumption/budgets) against
    # these two percentages; WARNING must be < CRITICAL, like capacity.
    # The trend collector is a deterministic period-over-period actual-
    # cost comparison (Microsoft.CostManagement/query), NOT Cost
    # Management's native anomaly-detection feature -- see
    # docs/AZURE_DATA_SOURCES.md for why.
    cost_budget_warning_pct: float = 80.0
    cost_budget_critical_pct: float = 100.0
    cost_trend_lookback_days: int = 30
    cost_trend_growth_pct_threshold: float = 20.0
    enable_cost_management_budget: bool = True
    enable_cost_management_trend: bool = True

    # Azure Backup. Both read from the Log Analytics workspace's
    # AddonAzureBackupJobs/CoreAzureBackup tables (Azure Backup
    # diagnostic data), so -- like workload SLOs -- they assume Recovery
    # Services vaults are sending diagnostics there; see
    # docs/AZURE_DATA_SOURCES.md.
    backup_lookback_hours: int = 24
    backup_stale_recovery_point_days: int = 3
    enable_backup: bool = True

    # Azure Update Manager (Resource Graph `patchassessmentresources`).
    # ARG itself only retains 7 days of assessment history, so the
    # default staleness threshold matches that retention window.
    patch_assessment_stale_days: int = 7
    enable_update_manager: bool = True

    # Key Vault certificate/secret/key expiry (data-plane List APIs --
    # metadata only, values are never read). `key_vault_monitor_uris` is
    # the bounded, explicit vault allowlist this collector checks; an
    # empty list (the default) means not_configured, exactly like
    # capacity's `locations` input.
    key_vault_expiry_warning_days: int = 30
    key_vault_monitor_uris: Tuple[str, ...] = ()
    key_vault_max_items_per_type: int = 200
    enable_key_vault_expiry: bool = True

    # Azure Automation job failures. `automation_account_ids` is a
    # bounded, explicit list of Automation Account ARM resource IDs; an
    # empty list means not_configured.
    automation_lookback_hours: int = 24
    automation_account_ids: Tuple[str, ...] = ()
    enable_automation: bool = True

    # Telemetry coverage gaps (diagnostic settings + Log Analytics
    # heartbeat). `telemetry_monitored_resource_types` bounds resource-
    # type discovery (via Resource Graph) to a curated allowlist rather
    # than every resource type in the subscription;
    # `telemetry_critical_resource_ids` lets an operator pin specific
    # resource ids regardless of type. `telemetry_max_resources` bounds
    # the total number of per-resource diagnostic-settings calls made in
    # one run (each is a separate ARM REST call -- see
    # docs/AZURE_DATA_SOURCES.md for why Resource Graph can't do this in
    # one query).
    telemetry_monitored_resource_types: Tuple[str, ...] = DEFAULT_TELEMETRY_MONITORED_RESOURCE_TYPES
    telemetry_critical_resource_ids: Tuple[str, ...] = ()
    telemetry_max_resources: int = 50
    telemetry_heartbeat_lookback_hours: int = 24
    enable_telemetry_coverage: bool = True

    # Retirement/deprecation advisories (Service Health HealthAdvisory
    # events via Resource Graph's ServiceHealthResources table).
    # `retirement_warning_days` is the threshold below which an
    # advisory's mitigation deadline is treated as high severity.
    retirement_warning_days: int = 180
    enable_retirement_advisories: bool = True

    # ── Phase 3: product-facing snapshot/state layer ──────────────────
    # Bounds how long a built OperationsSnapshot (app/operations/snapshot.py)
    # is served from the in-process cache before the next request rebuilds
    # it from Azure -- keyed by the normalized subscription set, so a
    # different ?subs= selection never reuses another selection's cache
    # entry. A caller can always force a rebuild (?refresh=true).
    snapshot_cache_ttl_seconds: int = 60
    # SQLite file path for the finding workflow-state store
    # (app/operations/state.py) -- status/owner/snooze/audit history.
    # The default is a local, working-directory-relative path suitable
    # for local dev; on Azure App Service (Linux), set this to something
    # under /home (the only persisted, writable path across restarts/
    # scale-outs on that platform), e.g. /home/data/operations.db.
    operations_state_db_path: str = "operations_state.db"

    def __post_init__(self):
        if self.alert_lookback_hours <= 0:
            raise OperationsConfigError(f"alert_lookback_hours must be positive, got {self.alert_lookback_hours}.")
        if self.change_lookback_hours <= 0:
            raise OperationsConfigError(f"change_lookback_hours must be positive, got {self.change_lookback_hours}.")
        if self.change_correlation_window_minutes <= 0:
            raise OperationsConfigError(
                f"change_correlation_window_minutes must be positive, got {self.change_correlation_window_minutes}."
            )
        if not (0 < self.capacity_warning_pct <= 100):
            raise OperationsConfigError(f"capacity_warning_pct must be in (0, 100], got {self.capacity_warning_pct}.")
        if not (0 < self.capacity_critical_pct <= 100):
            raise OperationsConfigError(f"capacity_critical_pct must be in (0, 100], got {self.capacity_critical_pct}.")
        if self.capacity_warning_pct >= self.capacity_critical_pct:
            raise OperationsConfigError(
                f"capacity_warning_pct ({self.capacity_warning_pct}) must be less than "
                f"capacity_critical_pct ({self.capacity_critical_pct})."
            )
        if self.slo_definitions_path and self.slo_definitions_json:
            raise OperationsConfigError(
                "set at most one of SLO_DEFINITIONS_PATH / SLO_DEFINITIONS_JSON, not both "
                "(SLO_DEFINITIONS_JSON would silently win, which hides a likely misconfiguration)."
            )

        if self.cost_budget_warning_pct <= 0:
            raise OperationsConfigError(f"cost_budget_warning_pct must be positive, got {self.cost_budget_warning_pct}.")
        if self.cost_budget_critical_pct <= 0:
            raise OperationsConfigError(f"cost_budget_critical_pct must be positive, got {self.cost_budget_critical_pct}.")
        if self.cost_budget_warning_pct >= self.cost_budget_critical_pct:
            raise OperationsConfigError(
                f"cost_budget_warning_pct ({self.cost_budget_warning_pct}) must be less than "
                f"cost_budget_critical_pct ({self.cost_budget_critical_pct})."
            )
        if self.cost_trend_lookback_days <= 0:
            raise OperationsConfigError(f"cost_trend_lookback_days must be positive, got {self.cost_trend_lookback_days}.")
        if self.cost_trend_growth_pct_threshold <= 0:
            raise OperationsConfigError(
                f"cost_trend_growth_pct_threshold must be positive, got {self.cost_trend_growth_pct_threshold}."
            )
        if self.backup_lookback_hours <= 0:
            raise OperationsConfigError(f"backup_lookback_hours must be positive, got {self.backup_lookback_hours}.")
        if self.backup_stale_recovery_point_days <= 0:
            raise OperationsConfigError(
                f"backup_stale_recovery_point_days must be positive, got {self.backup_stale_recovery_point_days}."
            )
        if self.patch_assessment_stale_days <= 0:
            raise OperationsConfigError(
                f"patch_assessment_stale_days must be positive, got {self.patch_assessment_stale_days}."
            )
        if self.key_vault_expiry_warning_days <= 0:
            raise OperationsConfigError(
                f"key_vault_expiry_warning_days must be positive, got {self.key_vault_expiry_warning_days}."
            )
        if self.key_vault_max_items_per_type <= 0:
            raise OperationsConfigError(
                f"key_vault_max_items_per_type must be positive, got {self.key_vault_max_items_per_type}."
            )
        if self.automation_lookback_hours <= 0:
            raise OperationsConfigError(f"automation_lookback_hours must be positive, got {self.automation_lookback_hours}.")
        if self.telemetry_max_resources <= 0:
            raise OperationsConfigError(f"telemetry_max_resources must be positive, got {self.telemetry_max_resources}.")
        if self.telemetry_heartbeat_lookback_hours <= 0:
            raise OperationsConfigError(
                f"telemetry_heartbeat_lookback_hours must be positive, got {self.telemetry_heartbeat_lookback_hours}."
            )
        if self.retirement_warning_days <= 0:
            raise OperationsConfigError(f"retirement_warning_days must be positive, got {self.retirement_warning_days}.")

        if self.snapshot_cache_ttl_seconds <= 0:
            raise OperationsConfigError(
                f"snapshot_cache_ttl_seconds must be positive, got {self.snapshot_cache_ttl_seconds}."
            )
        if not self.operations_state_db_path.strip():
            raise OperationsConfigError("operations_state_db_path must not be blank.")

    @classmethod
    def from_env(cls) -> "OperationsConfig":
        def env_int(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            return _parse_positive_int(raw, name) if raw else default

        def env_pct(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            return _parse_pct(raw, name) if raw else default

        def env_positive_float(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            return _parse_positive_float(raw, name) if raw else default

        def env_bool(name: str, default: bool) -> bool:
            raw = os.environ.get(name, "").strip()
            return _parse_bool(raw, name) if raw else default

        def env_list(name: str, default: Tuple[str, ...] = ()) -> Tuple[str, ...]:
            raw = os.environ.get(name, "")
            return _parse_csv_list(raw) if raw.strip() else default

        return cls(
            alert_lookback_hours=env_int("ALERT_LOOKBACK_HOURS", 24),
            change_lookback_hours=env_int("CHANGE_LOOKBACK_HOURS", 24),
            change_correlation_window_minutes=env_int("CHANGE_CORRELATION_WINDOW_MINUTES", 60),
            capacity_warning_pct=env_pct("CAPACITY_WARNING_PCT", 75.0),
            capacity_critical_pct=env_pct("CAPACITY_CRITICAL_PCT", 90.0),
            slo_definitions_path=os.environ.get("SLO_DEFINITIONS_PATH", "").strip(),
            slo_definitions_json=os.environ.get("SLO_DEFINITIONS_JSON", "").strip(),
            enable_defender_alerts=env_bool("ENABLE_DEFENDER_ALERTS", True),
            enable_defender_assessments=env_bool("ENABLE_DEFENDER_ASSESSMENTS", True),
            cost_budget_warning_pct=env_positive_float("COST_BUDGET_WARNING_PCT", 80.0),
            cost_budget_critical_pct=env_positive_float("COST_BUDGET_CRITICAL_PCT", 100.0),
            cost_trend_lookback_days=env_int("COST_TREND_LOOKBACK_DAYS", 30),
            cost_trend_growth_pct_threshold=env_positive_float("COST_TREND_GROWTH_PCT_THRESHOLD", 20.0),
            enable_cost_management_budget=env_bool("ENABLE_COST_MANAGEMENT_BUDGET", True),
            enable_cost_management_trend=env_bool("ENABLE_COST_MANAGEMENT_TREND", True),
            backup_lookback_hours=env_int("BACKUP_LOOKBACK_HOURS", 24),
            backup_stale_recovery_point_days=env_int("BACKUP_STALE_RECOVERY_POINT_DAYS", 3),
            enable_backup=env_bool("ENABLE_BACKUP", True),
            patch_assessment_stale_days=env_int("PATCH_ASSESSMENT_STALE_DAYS", 7),
            enable_update_manager=env_bool("ENABLE_UPDATE_MANAGER", True),
            key_vault_expiry_warning_days=env_int("KEY_VAULT_EXPIRY_WARNING_DAYS", 30),
            key_vault_monitor_uris=env_list("KEY_VAULT_MONITOR_URIS"),
            key_vault_max_items_per_type=env_int("KEY_VAULT_MAX_ITEMS_PER_TYPE", 200),
            enable_key_vault_expiry=env_bool("ENABLE_KEY_VAULT_EXPIRY", True),
            automation_lookback_hours=env_int("AUTOMATION_LOOKBACK_HOURS", 24),
            automation_account_ids=env_list("AUTOMATION_ACCOUNT_IDS"),
            enable_automation=env_bool("ENABLE_AUTOMATION", True),
            telemetry_monitored_resource_types=env_list(
                "TELEMETRY_MONITORED_RESOURCE_TYPES", DEFAULT_TELEMETRY_MONITORED_RESOURCE_TYPES
            ),
            telemetry_critical_resource_ids=env_list("TELEMETRY_CRITICAL_RESOURCE_IDS"),
            telemetry_max_resources=env_int("TELEMETRY_MAX_RESOURCES", 50),
            telemetry_heartbeat_lookback_hours=env_int("TELEMETRY_HEARTBEAT_LOOKBACK_HOURS", 24),
            enable_telemetry_coverage=env_bool("ENABLE_TELEMETRY_COVERAGE", True),
            retirement_warning_days=env_int("RETIREMENT_WARNING_DAYS", 180),
            enable_retirement_advisories=env_bool("ENABLE_RETIREMENT_ADVISORIES", True),
            snapshot_cache_ttl_seconds=env_int("OPERATIONS_SNAPSHOT_CACHE_TTL_SECONDS", 60),
            operations_state_db_path=os.environ.get("OPERATIONS_STATE_DB", "").strip() or "operations_state.db",
        )
