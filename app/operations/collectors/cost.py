"""Azure Cost Management: budget threshold state and a deterministic
period-over-period cost trend, normalized to BudgetSummary/Finding.

Two independent, separately-named signals (see docs/AZURE_DATA_SOURCES.md
for the full rationale):

  - collect_budget_summaries() / budget_summaries_to_findings()
    Reads Microsoft.Consumption/budgets (ARM REST) -- a stable, GA API.
    Every configured budget gets a BudgetSummary (healthy ones included,
    matching capacity.py's CapacitySummary convention); only
    warning/critical budgets become Findings.

  - collect_cost_trend()
    Azure Cost Management's native anomaly-detection feature has no
    stable, generally-available REST surface at the time of writing (see
    docs/AZURE_DATA_SOURCES.md) -- this function deliberately does NOT
    call it or fabricate an "anomaly". Instead it computes a strictly
    deterministic period-over-period actual-cost comparison via the
    Microsoft.CostManagement/query REST API (also GA/stable) and raises a
    Finding only when the percentage change vs. the prior period of equal
    length exceeds a configured threshold. This is a cost TREND signal,
    not ML-based anomaly detection -- named and documented as such so it
    is never mistaken for one.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.operations.collectors.http import (
    CredentialFactory,
    DEFAULT_ARM_POST_MAX_RETRIES,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RECORDS,
    HttpGet,
    HttpPost,
    SleepFn,
    arm_post,
    default_credential_factory,
    default_http_get,
    default_http_post,
    default_sleep_fn,
    paginated_get,
)
from app.operations.errors import OperationsCollectionError
from app.operations.models import (
    BudgetSummary,
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    format_utc_iso,
)

__all__ = [
    "budget_to_summary",
    "collect_budget_summaries",
    "budget_summaries_to_findings",
    "collect_cost_trend",
]

BUDGET_SOURCE = EvidenceSource.COST_MANAGEMENT_BUDGET.value
TREND_SOURCE = EvidenceSource.COST_MANAGEMENT_USAGE.value

BUDGETS_API_VERSION = "2023-05-01"
QUERY_API_VERSION = "2023-11-01"


def budget_to_summary(raw_budget: dict, *, warning_pct: float, critical_pct: float, now: datetime) -> BudgetSummary:
    """Normalize one Microsoft.Consumption/budgets payload into a
    BudgetSummary. A budget with amount <= 0 (malformed/not yet
    provisioned) gets threshold_state='unknown' rather than a division
    by zero or a fabricated percentage."""
    props = raw_budget.get("properties") or {}
    budget_name = raw_budget.get("name") or raw_budget.get("id") or ""
    if not budget_name:
        raise OperationsCollectionError(BUDGET_SOURCE, "budget payload is missing a name/id")

    amount = float(props.get("amount") or 0)
    current_spend_obj = props.get("currentSpend") or {}
    current_spend = float(current_spend_obj.get("amount") or 0)
    currency = current_spend_obj.get("unit") or "USD"
    time_grain = props.get("timeGrain") or ""
    category = props.get("category") or "Cost"

    usage_pct = round(current_spend / amount * 100, 2) if amount > 0 else None
    if usage_pct is None:
        threshold_state = "unknown"
    elif usage_pct >= critical_pct:
        threshold_state = "critical"
    elif usage_pct >= warning_pct:
        threshold_state = "warning"
    else:
        threshold_state = "healthy"

    evaluated_at = format_utc_iso(now)
    return BudgetSummary(
        budget_name=budget_name,
        amount=amount,
        current_spend=current_spend,
        currency=currency,
        time_grain=time_grain,
        category=category,
        usage_pct=usage_pct,
        threshold_state=threshold_state,
        evaluated_at=evaluated_at,
        evidence=[EvidenceReference(
            source=BUDGET_SOURCE,
            title=f"Budget: {budget_name}",
            observed_at=evaluated_at,
            reference=raw_budget.get("id") or budget_name,
            raw_excerpt=f"amount={amount} {currency}, currentSpend={current_spend} {currency}, timeGrain={time_grain}",
        )],
    )


def collect_budget_summaries(
    subscription_id: str,
    *,
    warning_pct: float = 80.0,
    critical_pct: float = 100.0,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[datetime] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> list:
    """Every Microsoft.Consumption budget configured on `subscription_id`,
    normalized to a BudgetSummary. An empty result is a legitimate 'ok,
    zero budgets configured in Azure' state, not an error -- see
    app.operations.service for how that maps to the envelope status.

    `max_pages`/`max_records` bound how many `nextLink` pages/budgets
    this call will ever follow/accumulate (see
    app.operations.collectors.http.paginated_get)."""
    if not subscription_id:
        raise ValueError("subscription_id is required")
    now = now or datetime.now(timezone.utc)

    paged = paginated_get(
        f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/budgets",
        source=BUDGET_SOURCE,
        params={"api-version": BUDGETS_API_VERSION},
        credential_factory=credential_factory,
        http_get=http_get,
        max_pages=max_pages,
        max_records=max_records,
    )
    return [
        budget_to_summary(raw, warning_pct=warning_pct, critical_pct=critical_pct, now=now)
        for raw in paged.items
    ]


def budget_summaries_to_findings(summaries: list) -> list:
    """One Finding per warning/critical budget -- healthy/unknown budgets
    stay informational-only in the summaries list (see
    capacity_summaries_to_findings for the identical convention)."""
    findings = []
    for summary in summaries:
        if summary.threshold_state not in ("warning", "critical"):
            continue
        severity = Severity.HIGH if summary.threshold_state == "critical" else Severity.MEDIUM
        findings.append(Finding(
            category=FindingCategory.COST.value,
            severity=severity.value,
            status=FindingStatus.OPEN.value,
            title=f"Budget '{summary.budget_name}': {summary.threshold_state}",
            summary=(
                f"Spend is {summary.current_spend} {summary.currency} of a {summary.amount} {summary.currency} "
                f"{summary.time_grain or ''} budget ({summary.usage_pct}% used)."
            ),
            business_impact=f"Budget '{summary.budget_name}' is at or approaching its configured threshold.",
            first_seen=summary.evaluated_at,
            last_seen=summary.evaluated_at,
            source=BUDGET_SOURCE,
            confidence=ConfidenceLevel.DERIVED.value,
            evidence=summary.evidence,
            recommended_action="Review recent spend drivers and confirm whether this budget or the underlying usage needs adjustment.",
            approval_required=False,
            executive_attention=summary.threshold_state == "critical",
            metadata={"budget_name": summary.budget_name, "usage_pct": summary.usage_pct, "category": summary.category},
            discriminator=summary.budget_name,
        ))
    return findings


def _query_total_cost(
    subscription_id: str,
    *,
    start: datetime,
    end: datetime,
    credential_factory: CredentialFactory,
    http_post: HttpPost,
    max_retries: int,
    sleep_fn: SleepFn,
) -> Optional[float]:
    """POST one Microsoft.CostManagement/query (ActualCost, no
    granularity -- a single summed total for [start, end)) and return the
    total cost, or None if the period returned no rows (no cost data,
    never treated as zero-cost)."""
    body = arm_post(
        f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query?api-version={QUERY_API_VERSION}",
        source=TREND_SOURCE,
        json_body={
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {"from": format_utc_iso(start), "to": format_utc_iso(end)},
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            },
        },
        credential_factory=credential_factory,
        http_post=http_post,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
    )
    columns = [c.get("name") for c in (body.get("columns") or [])]
    rows = body.get("rows") or []
    if not rows or "Cost" not in columns:
        return None
    cost_index = columns.index("Cost")
    return float(rows[0][cost_index])


def collect_cost_trend(
    subscription_id: str,
    *,
    lookback_days: int = 30,
    growth_pct_threshold: float = 20.0,
    credential_factory: CredentialFactory = default_credential_factory,
    http_post: HttpPost = default_http_post,
    now: Optional[datetime] = None,
    max_retries: int = DEFAULT_ARM_POST_MAX_RETRIES,
    sleep_fn: SleepFn = default_sleep_fn,
) -> list:
    """Deterministic period-over-period actual-cost trend: compares total
    cost for the last `lookback_days` against the equal-length period
    immediately before it, via Microsoft.CostManagement/query.

    Raises ValueError for a non-positive lookback_days/growth_pct_threshold
    (no silent clamping). Returns [] (never a fabricated Finding) when the
    prior period has no cost data to compute a meaningful percentage
    against -- there is no baseline to call "material growth" against.

    `max_retries`/`sleep_fn` are forwarded to each underlying `arm_post`
    call (see app.operations.collectors.http.arm_post) -- Cost
    Management's Query API is the one that has been observed throttling
    (HTTP 429) under real load; a transient 429/5xx here is retried
    with backoff rather than immediately failing the whole trend
    collection.
    """
    if not subscription_id:
        raise ValueError("subscription_id is required")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")
    if growth_pct_threshold <= 0:
        raise ValueError("growth_pct_threshold must be positive")

    now = now or datetime.now(timezone.utc)
    current_end = now
    current_start = now - timedelta(days=lookback_days)
    prior_end = current_start
    prior_start = current_start - timedelta(days=lookback_days)

    current_cost = _query_total_cost(
        subscription_id, start=current_start, end=current_end,
        credential_factory=credential_factory, http_post=http_post,
        max_retries=max_retries, sleep_fn=sleep_fn,
    )
    prior_cost = _query_total_cost(
        subscription_id, start=prior_start, end=prior_end,
        credential_factory=credential_factory, http_post=http_post,
        max_retries=max_retries, sleep_fn=sleep_fn,
    )

    if current_cost is None or prior_cost is None or prior_cost <= 0:
        return []

    growth_pct = round((current_cost - prior_cost) / prior_cost * 100, 2)
    if growth_pct < growth_pct_threshold:
        return []

    evaluated_at = format_utc_iso(now)
    evidence = [EvidenceReference(
        source=TREND_SOURCE,
        title="Cost Management period-over-period trend",
        observed_at=evaluated_at,
        reference=f"Microsoft.CostManagement/query ({lookback_days}d window)",
        raw_excerpt=f"currentPeriodCost={current_cost}, priorPeriodCost={prior_cost}, growthPct={growth_pct}",
    )]
    return [Finding(
        category=FindingCategory.COST.value,
        severity=Severity.MEDIUM.value,
        status=FindingStatus.OPEN.value,
        title=f"Cost trend: {growth_pct}% higher than the prior {lookback_days}-day period",
        summary=(
            f"Actual cost over the last {lookback_days} days ({current_cost:.2f}) is {growth_pct}% higher than "
            f"the prior {lookback_days}-day period ({prior_cost:.2f})."
        ),
        business_impact="Spend is growing materially faster than the prior comparable period -- may indicate an unplanned deployment, scale-out, or leaked resource.",
        first_seen=evaluated_at,
        last_seen=evaluated_at,
        source=TREND_SOURCE,
        confidence=ConfidenceLevel.DERIVED.value,
        evidence=evidence,
        recommended_action="Review Cost Management's cost analysis for the drivers of this period's spend increase.",
        approval_required=False,
        executive_attention=False,
        metadata={"current_period_cost": current_cost, "prior_period_cost": prior_cost, "growth_pct": growth_pct, "lookback_days": lookback_days},
        discriminator=f"{evaluated_at[:10]}|{lookback_days}",
    )]
