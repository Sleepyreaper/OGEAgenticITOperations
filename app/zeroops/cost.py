"""Per-incident cost model for ZeroOps: SRE Agent AAUs + squad Foundry tokens.

SRE Agent billing (learn.microsoft.com/azure/sre-agent/pricing-billing):
always-on flow is 4 AAU per agent-hour; active flow is metered per token
with model-specific AAU rates. The USD price per AAU comes from
``SRE_AGENT_AAU_PRICE_USD`` (Azure Retail Prices API list price for the
"SRE Agent Unit" meter was $0.10 in eastus/westus2 at time of writing --
verify for your region and agreement).

Squad cost is the ``usage_summary.estimated_cost_usd`` the analysis
pipeline already computes from real token counts.
"""
import os

ALWAYS_ON_AAU_PER_HOUR = 4.0
HOURS_PER_MONTH = 730.0
DEFAULT_AAU_PRICE_USD = 0.10

# AAUs per 1M tokens: (input, output, cache_read, cache_write)
SRE_MODEL_AAU_RATES = {
    "gpt-5.2": (35.0, 280.0, 3.5, 0.0),
    "gpt-5.3-codex": (35.0, 280.0, 3.5, 0.0),
    "claude-opus-4.6": (100.0, 500.0, 10.0, 125.0),
}

# Typical SRE Agent token profiles (published "active flow by task type").
SRE_TASK_PROFILES = {
    "quick_question": {"input": 20_000, "output": 2_000, "cache_read": 15_000, "cache_write": 5_000},
    "incident_investigation": {"input": 200_000, "output": 15_000, "cache_read": 150_000, "cache_write": 50_000},
    "full_remediation": {"input": 500_000, "output": 40_000, "cache_read": 400_000, "cache_write": 100_000},
}


def aau_price_usd() -> float:
    raw = os.environ.get("SRE_AGENT_AAU_PRICE_USD", "").strip()
    if not raw:
        return DEFAULT_AAU_PRICE_USD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_AAU_PRICE_USD
    return value if value >= 0 else DEFAULT_AAU_PRICE_USD


def sre_model() -> str:
    model = os.environ.get("SRE_AGENT_MODEL", "gpt-5.2").strip().lower()
    return model if model in SRE_MODEL_AAU_RATES else "gpt-5.2"


def sre_active_aau(profile: str, model: str = "") -> float:
    tokens = SRE_TASK_PROFILES.get(profile) or SRE_TASK_PROFILES["incident_investigation"]
    rates = SRE_MODEL_AAU_RATES[model or sre_model()]
    keys = ("input", "output", "cache_read", "cache_write")
    return round(sum(tokens[k] / 1_000_000 * rate for k, rate in zip(keys, rates)), 3)


def incident_cost(*, sre_profile: str, squad_usage: dict = None) -> dict:
    """Blend estimated SRE Agent active-flow cost with the squad's measured token cost."""
    price = aau_price_usd()
    model = sre_model()
    aau = sre_active_aau(sre_profile, model)
    sre_usd = round(aau * price, 4)
    squad_usage = squad_usage or {}
    squad_usd = round(float(squad_usage.get("estimated_cost_usd") or 0.0), 4)
    return {
        "sre_agent": {
            "model": model, "task_profile": sre_profile, "active_aau": aau,
            "aau_price_usd": price, "estimated_usd": sre_usd, "basis": "published token profile (estimate)",
        },
        "squad": {
            "total_tokens": int(squad_usage.get("total_tokens") or 0),
            "model_calls": int(squad_usage.get("model_calls") or 0),
            "estimated_usd": squad_usd, "basis": "measured tokens (usage_summary)",
        },
        "total_estimated_usd": round(sre_usd + squad_usd, 4),
    }


def model_rates_summary() -> dict:
    model = sre_model()
    i, o, cr, cw = SRE_MODEL_AAU_RATES[model]
    return {"model": model, "aau_per_1m": {"input": i, "output": o, "cache_read": cr, "cache_write": cw}}


def monthly_baseline() -> dict:
    price = aau_price_usd()
    aau = ALWAYS_ON_AAU_PER_HOUR * HOURS_PER_MONTH
    return {
        "always_on_aau_per_hour": ALWAYS_ON_AAU_PER_HOUR,
        "always_on_aau_per_month": aau,
        "aau_price_usd": price,
        "always_on_usd_per_month": round(aau * price, 2),
        "sre_model": model_rates_summary(),
        "task_profiles_aau": {name: sre_active_aau(name) for name in SRE_TASK_PROFILES},
        "note": "Always-on is billed from agent creation until deletion. Verify AAU price for your region/agreement.",
    }
