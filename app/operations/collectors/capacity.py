"""Regional compute/quota capacity signals via ARM REST.

Covers Microsoft.Compute regional usage/limits and, where reliably
available, Azure OpenAI quota (Microsoft.CognitiveServices regional
usages -- the ARM-level view of TPM/quota consumption for a subscription
in a region; it is not per-deployment, since ARM does not expose a
reliable per-deployment quota-consumption endpoint at the time of
writing -- see docs/EVIDENCE_MODEL.md for this limitation). Both
providers' "usages" APIs return the same
`{value:[{currentValue, limit, unit, name:{value, localizedValue}}]}`
shape, so they share `_usages_to_capacity_summaries` below.

Exhaustion forecasting (`compute_exhaustion_forecast`) is a simple,
deterministic linear-regression extrapolation over caller-supplied
historical usage points -- it is never invented when no history is
available (forecast_state = 'not_available').
"""

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from app.operations.collectors.http import (
    CredentialFactory,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RECORDS,
    HttpGet,
    default_credential_factory,
    default_http_get,
    paginated_get,
)
from app.operations.models import (
    CapacitySummary,
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    format_utc_iso,
    parse_utc_iso,
)

__all__ = [
    "ForecastResult",
    "compute_exhaustion_forecast",
    "collect_compute_capacity",
    "collect_openai_capacity",
    "capacity_summaries_to_findings",
]

COMPUTE_SOURCE = EvidenceSource.ARM_COMPUTE_USAGE.value
OPENAI_SOURCE = EvidenceSource.ARM_OPENAI_QUOTA.value

COMPUTE_API_VERSION = "2024-07-01"
COGNITIVE_SERVICES_API_VERSION = "2024-10-01"

# A Finding is only raised for a forecasted exhaustion within this many
# days -- a mathematically valid but distant (e.g. 400-day) forecast is
# noise, not an actionable capacity Finding.
FORECAST_FINDING_HORIZON_DAYS = 30

# scope_key (f"{resource_scope}:{metric}") -> list[(datetime, float)] of
# historical usage points, oldest first. None (the default almost
# everywhere) means "no history available" -- forecast_state is always
# 'not_available' in that case, never guessed.
HistoryProvider = Callable[[str], list]


class ForecastResult:
    """Plain result holder (not an app.operations.models dataclass since
    it's an internal computation detail, not part of the evidence
    schema -- CapacitySummary.forecast_state/forecast_exhaustion_at carry
    the externally-visible result)."""

    __slots__ = ("state", "exhaustion_at", "slope_per_hour")

    def __init__(self, state: str, exhaustion_at: Optional[str] = None, slope_per_hour: Optional[float] = None):
        self.state = state
        self.exhaustion_at = exhaustion_at
        self.slope_per_hour = slope_per_hour


def compute_exhaustion_forecast(history: list, *, limit: float, now: datetime) -> ForecastResult:
    """Deterministic least-squares linear extrapolation of `history`
    ((datetime, value) pairs) to the point it crosses `limit`.

      - Fewer than 2 points, a zero-variance x-axis, or limit <= 0:
        state='not_available' (insufficient signal to fit a trend).
      - A flat/decreasing trend (slope <= 0): state='not_applicable'
        (usage isn't heading toward the limit).
      - Otherwise: state='available' with the extrapolated exhaustion
        timestamp (clamped to `now` if the trend already implies it's
        been crossed).
    """
    if limit <= 0 or len(history) < 2:
        return ForecastResult(state="not_available")

    points = sorted(history, key=lambda p: p[0])
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in points]
    ys = [float(v) for _, v in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return ForecastResult(state="not_available")

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom  # units per hour
    intercept = mean_y - slope * mean_x
    if slope <= 0:
        return ForecastResult(state="not_applicable", slope_per_hour=round(slope, 6))

    hours_to_limit = (limit - intercept) / slope
    exhaustion_time = t0 + timedelta(hours=hours_to_limit)
    if exhaustion_time <= now:
        exhaustion_time = now  # trend says the limit is already crossed
    return ForecastResult(state="available", exhaustion_at=format_utc_iso(exhaustion_time), slope_per_hour=round(slope, 6))


def _filter_openai_items_by_name(items: list, name_filters: Optional[Sequence[str]]) -> list:
    """Case-insensitive substring filter over each Cognitive Services
    usages item's `name.value`/`name.localizedValue`, applied BEFORE
    `_usages_to_capacity_summaries` normalizes anything -- so a filtered-
    out quota never even produces a CapacitySummary/threshold
    classification, let alone a Finding.

    Exists because a shared Azure OpenAI/Cognitive Services account can
    carry many unrelated model quotas (e.g. Claude/image models
    provisioned for other teams) that are always fully allocated and
    would otherwise dominate a capacity executive summary with noise
    that has nothing to do with this deployment's own models. An empty/
    None `name_filters` means "no filtering" -- every item is kept, the
    correct default for a dedicated/generic account where every quota
    is relevant. This is ONLY ever applied to Cognitive Services/Azure
    OpenAI usages -- see collect_openai_capacity -- Compute usages are
    never filtered by this or any name-based rule.
    """
    if not name_filters:
        return items
    lowered_filters = [f.strip().lower() for f in name_filters if f and f.strip()]
    if not lowered_filters:
        return items
    kept = []
    for item in items:
        name_obj = item.get("name") or {}
        name = str(name_obj.get("value") or name_obj.get("localizedValue") or "")
        name_lower = name.lower()
        if any(term in name_lower for term in lowered_filters):
            kept.append(item)
    return kept


def _usages_to_capacity_summaries(
    items: list,
    *,
    resource_scope: str,
    source: str,
    warning_pct: float,
    critical_pct: float,
    history_provider: Optional[HistoryProvider],
    now: datetime,
) -> list:
    evaluated_at = format_utc_iso(now)
    summaries = []
    for item in items:
        name_obj = item.get("name") or {}
        metric = name_obj.get("value") or name_obj.get("localizedValue") or "unknown"
        current = float(item.get("currentValue", 0) or 0)
        limit = float(item.get("limit", 0) or 0)

        headroom_pct = round((limit - current) / limit * 100, 2) if limit > 0 else None
        usage_pct = (current / limit * 100) if limit > 0 else None
        if usage_pct is None:
            threshold_state = "unknown"
        elif usage_pct >= critical_pct:
            threshold_state = "critical"
        elif usage_pct >= warning_pct:
            threshold_state = "warning"
        else:
            threshold_state = "healthy"

        forecast_state = "not_available"
        forecast_exhaustion_at = None
        if history_provider is not None:
            history = history_provider(f"{resource_scope}:{metric}") or []
            forecast = compute_exhaustion_forecast(history, limit=limit, now=now)
            forecast_state = forecast.state
            forecast_exhaustion_at = forecast.exhaustion_at

        summaries.append(CapacitySummary(
            resource_scope=resource_scope,
            metric=metric,
            current=current,
            limit=limit,
            headroom_pct=headroom_pct,
            threshold_state=threshold_state,
            forecast_state=forecast_state,
            forecast_exhaustion_at=forecast_exhaustion_at,
            evaluated_at=evaluated_at,
            evidence=[EvidenceReference(
                source=source,
                title=f"{resource_scope} usage: {metric}",
                observed_at=evaluated_at,
                reference=metric,
                raw_excerpt=f"current={current}, limit={limit}, unit={item.get('unit', '')}",
            )],
        ))
    return summaries


def collect_compute_capacity(
    subscription_id: str,
    locations: list,
    *,
    warning_pct: float = 75.0,
    critical_pct: float = 90.0,
    history_provider: Optional[HistoryProvider] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[datetime] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> list:
    """Microsoft.Compute regional usage/limits for each of `locations`
    (the Azure regions actually in use -- callers should discover these
    via Resource Graph, not hardcode a list).

    `max_pages`/`max_records` bound how many `nextLink` pages/usage
    entries this call will ever follow/accumulate PER LOCATION (see
    app.operations.collectors.http.paginated_get)."""
    if not subscription_id:
        raise ValueError("subscription_id is required")
    if not locations:
        raise ValueError("locations must be a non-empty list of Azure regions")
    now = now or datetime.now(timezone.utc)

    summaries = []
    for location in locations:
        paged = paginated_get(
            f"/subscriptions/{subscription_id}/providers/Microsoft.Compute/locations/{location}/usages",
            source=COMPUTE_SOURCE,
            params={"api-version": COMPUTE_API_VERSION},
            credential_factory=credential_factory,
            http_get=http_get,
            max_pages=max_pages,
            max_records=max_records,
        )
        summaries.extend(_usages_to_capacity_summaries(
            paged.items,
            resource_scope=f"compute:{location}",
            source=COMPUTE_SOURCE,
            warning_pct=warning_pct,
            critical_pct=critical_pct,
            history_provider=history_provider,
            now=now,
        ))
    return summaries


def collect_openai_capacity(
    subscription_id: str,
    locations: list,
    *,
    warning_pct: float = 75.0,
    critical_pct: float = 90.0,
    name_filters: Optional[Sequence[str]] = None,
    history_provider: Optional[HistoryProvider] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[datetime] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> list:
    """Azure OpenAI / Cognitive Services regional quota usage for each of
    `locations`. This is a subscription+region aggregate (ARM's
    Microsoft.CognitiveServices "usages" endpoint), not a per-account or
    per-deployment breakdown -- see the module docstring.

    `name_filters` (optional, case-insensitive substring match against
    each quota's `name.value`/`localizedValue`) narrows results to
    quotas relevant to this deployment BEFORE any threshold/forecast
    normalization happens -- see `_filter_openai_items_by_name`. Empty/
    None (the default) means no filtering, matching this function's
    prior behavior exactly. NEVER applied to `collect_compute_capacity`
    -- Compute usages are always returned in full regardless of this
    parameter.

    `max_pages`/`max_records` bound how many `nextLink` pages/usage
    entries this call will ever follow/accumulate PER LOCATION (see
    app.operations.collectors.http.paginated_get)."""
    if not subscription_id:
        raise ValueError("subscription_id is required")
    if not locations:
        raise ValueError("locations must be a non-empty list of Azure regions")
    now = now or datetime.now(timezone.utc)

    summaries = []
    for location in locations:
        paged = paginated_get(
            f"/subscriptions/{subscription_id}/providers/Microsoft.CognitiveServices/locations/{location}/usages",
            source=OPENAI_SOURCE,
            params={"api-version": COGNITIVE_SERVICES_API_VERSION},
            credential_factory=credential_factory,
            http_get=http_get,
            max_pages=max_pages,
            max_records=max_records,
        )
        filtered_items = _filter_openai_items_by_name(paged.items, name_filters)
        summaries.extend(_usages_to_capacity_summaries(
            filtered_items,
            resource_scope=f"openai:{location}",
            source=OPENAI_SOURCE,
            warning_pct=warning_pct,
            critical_pct=critical_pct,
            history_provider=history_provider,
            now=now,
        ))
    return summaries


def capacity_summaries_to_findings(summaries: list) -> list:
    """One Finding per warning/critical CapacitySummary (healthy/unknown
    states stay informational-only in the summaries list, to keep
    Findings actionable), plus a separate Finding when a deterministic
    exhaustion forecast lands within FORECAST_FINDING_HORIZON_DAYS."""
    findings = []
    for summary in summaries:
        if summary.threshold_state in ("critical", "warning"):
            severity = Severity.HIGH if summary.threshold_state == "critical" else Severity.MEDIUM
            findings.append(Finding(
                category=FindingCategory.CAPACITY.value,
                severity=severity.value,
                status=FindingStatus.OPEN.value,
                title=f"{summary.resource_scope}/{summary.metric}: capacity {summary.threshold_state}",
                summary=(
                    f"{summary.metric} is at {summary.current}/{summary.limit} "
                    f"({100 - (summary.headroom_pct or 0):.1f}% used) in {summary.resource_scope}."
                ),
                business_impact="Approaching or at the regional quota limit -- new deployments/scale-outs may start failing.",
                first_seen=summary.evaluated_at,
                last_seen=summary.evaluated_at,
                source=EvidenceSource.ARM_COMPUTE_USAGE.value if summary.resource_scope.startswith("compute:") else EvidenceSource.ARM_OPENAI_QUOTA.value,
                confidence=ConfidenceLevel.DERIVED.value,
                evidence=summary.evidence,
                recommended_action="Request a quota increase or rebalance workload to a region/SKU with headroom.",
                approval_required=False,
                executive_attention=summary.threshold_state == "critical",
                metadata={"resource_scope": summary.resource_scope, "metric": summary.metric, "headroom_pct": summary.headroom_pct},
                discriminator=f"{summary.resource_scope}|{summary.metric}",
            ))

        if summary.forecast_state == "available" and summary.forecast_exhaustion_at:
            days_out = (parse_utc_iso(summary.forecast_exhaustion_at) - parse_utc_iso(summary.evaluated_at)).total_seconds() / 86400.0
            if 0 <= days_out <= FORECAST_FINDING_HORIZON_DAYS:
                findings.append(Finding(
                    category=FindingCategory.CAPACITY.value,
                    severity=Severity.MEDIUM.value,
                    status=FindingStatus.OPEN.value,
                    title=f"{summary.resource_scope}/{summary.metric}: forecast to exhaust within {FORECAST_FINDING_HORIZON_DAYS}d",
                    summary=f"Linear trend projects {summary.metric} reaching its limit by {summary.forecast_exhaustion_at}.",
                    business_impact="Sustained growth at the current rate will exhaust this quota before it can typically be increased.",
                    first_seen=summary.evaluated_at,
                    last_seen=summary.evaluated_at,
                    source=EvidenceSource.ARM_COMPUTE_USAGE.value if summary.resource_scope.startswith("compute:") else EvidenceSource.ARM_OPENAI_QUOTA.value,
                    confidence=ConfidenceLevel.ESTIMATED.value,
                    evidence=summary.evidence,
                    recommended_action="Request a quota increase proactively ahead of the forecasted exhaustion date.",
                    approval_required=False,
                    executive_attention=False,
                    metadata={"resource_scope": summary.resource_scope, "metric": summary.metric, "forecast_exhaustion_at": summary.forecast_exhaustion_at},
                    discriminator=f"forecast|{summary.resource_scope}|{summary.metric}",
                ))
    return findings
