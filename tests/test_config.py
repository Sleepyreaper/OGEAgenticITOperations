#!/usr/bin/env python3
"""Test the profile/config loading layer (app/profiles.py, app/config.py) and
the setup wizard's core logic (scripts/configure.py).

Run: python3 tests/test_config.py
"""
import copy
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.profiles import (  # noqa: E402
    AGENT_KEYS,
    PROFILES_ROOT,
    ProfileError,
    is_valid_profile_id,
    list_profiles,
    load_profile_document,
    resolve_profile_dir,
)
from app.config import Settings  # noqa: E402

import scripts.configure as wizard  # noqa: E402

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


@contextmanager
def temp_env(**overrides):
    """Temporarily set environment variables, restoring the prior state after."""
    sentinel = object()
    previous = {key: os.environ.get(key, sentinel) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, prior in previous.items():
            if prior is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


@contextmanager
def temp_profile(profile_id: str, document: dict, prompt_text: str = "You are a test agent."):
    """Create a real (but temporary) profile directory under profiles/, for
    exercising validation without touching the checked-in profiles."""
    profile_dir = PROFILES_ROOT / profile_id
    if profile_dir.exists():
        raise RuntimeError(f"temp_profile: {profile_dir} already exists — refusing to clobber it.")
    try:
        (profile_dir / "prompts").mkdir(parents=True)
        for key in AGENT_KEYS:
            (profile_dir / "prompts" / f"{key}.txt").write_text(prompt_text, encoding="utf-8")
        (profile_dir / "profile.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
        yield profile_dir
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def minimal_agent_doc(prompt_file: str) -> dict:
    return {
        "name": "Test Agent",
        "role": "A test role.",
        "deployment": "test-deployment",
        "prompt_file": prompt_file,
    }


def minimal_profile_document(profile_id: str) -> dict:
    return {
        "id": profile_id,
        "brand": {
            "app_name": "Test App",
            "app_title": "Test App \u2014 Title",
            "tagline_line1": "TEST",
            "tagline_line2": "Test Subtitle",
            "executive_subtitle": "Test Executive Subtitle",
            "logo_path": "/static/test-logo.svg",
            "logo_alt": "Test",
        },
        "agents": {key: minimal_agent_doc(f"prompts/{key}.txt") for key in AGENT_KEYS},
    }


# ─── app.profiles: list_profiles / is_valid_profile_id ─────────────────
print("\n\U0001f9ea Test 1: list_profiles() finds the checked-in profiles")
profiles = list_profiles()
test("includes 'oge'", "oge" in profiles)
test("includes 'generic'", "generic" in profiles)
test("includes 'power'", "power" in profiles)
test("sorted", profiles == sorted(profiles))

print("\n\U0001f9ea Test 2: is_valid_profile_id rejects unsafe/invalid ids")
test("plain id ok", is_valid_profile_id("contoso"))
test("kebab-case ok", is_valid_profile_id("contoso-retail"))
test("path traversal rejected", not is_valid_profile_id("../../etc"))
test("absolute path rejected", not is_valid_profile_id("/etc/passwd"))
test("uppercase rejected", not is_valid_profile_id("Contoso"))
test("empty rejected", not is_valid_profile_id(""))
test("leading dash rejected", not is_valid_profile_id("-contoso"))

# ─── app.profiles: resolve_profile_dir ─────────────────────────────────
print("\n\U0001f9ea Test 3: resolve_profile_dir enforces path safety + existence")
try:
    resolve_profile_dir("../../etc")
    test("path traversal id raises ProfileError", False)
except ProfileError:
    test("path traversal id raises ProfileError", True)

try:
    resolve_profile_dir("this-profile-does-not-exist")
    test("missing profile raises ProfileError", False)
except ProfileError as exc:
    test("missing profile raises ProfileError", True)
    test("error lists available profiles", "oge" in str(exc) and "generic" in str(exc))

oge_dir = resolve_profile_dir("oge")
test("resolves oge to profiles/oge", oge_dir == (PROFILES_ROOT / "oge").resolve())

# ─── app.profiles: load_profile_document — real profiles ───────────────
print("\n\U0001f9ea Test 4: load_profile_document accepts the checked-in profiles")
for profile_id in ("oge", "generic"):
    profile_dir = resolve_profile_dir(profile_id)
    document = load_profile_document(profile_dir, profile_id)
    test(f"{profile_id}: has all six agent keys", set(document["agents"]) == set(AGENT_KEYS))
    test(f"{profile_id}: brand has app_name", bool(document["brand"].get("app_name")))

# ─── app.profiles: load_profile_document — malformed profiles ─────────
print("\n\U0001f9ea Test 5: load_profile_document rejects malformed profile.json")
bad_document = {
    "id": "test-malformed",
    "brand": {"app_name": "X"},  # missing required brand keys
    "agents": {"orchestrator": {"name": "O"}},  # missing required agent keys + missing agents
}
with temp_profile("test-malformed", bad_document) as profile_dir:
    try:
        load_profile_document(profile_dir, "test-malformed")
        test("malformed profile raises ProfileError", False)
    except ProfileError as exc:
        message = str(exc)
        test("malformed profile raises ProfileError", True)
        test("reports missing brand keys", "brand.app_title" in message)
        test("reports missing agent keys", "cost_sentinel" in message and "scout" in message)
        test("reports missing agent fields", "agents.orchestrator.deployment" in message)

print("\n\U0001f9ea Test 6: load_profile_document rejects unsupported agent keys")
extra_agent_document = minimal_profile_document("test-extra-agent")
extra_agent_document["agents"]["not_a_real_agent"] = minimal_agent_doc("prompts/orchestrator.txt")
with temp_profile("test-extra-agent", extra_agent_document) as profile_dir:
    try:
        load_profile_document(profile_dir, "test-extra-agent")
        test("unsupported agent key raises ProfileError", False)
    except ProfileError as exc:
        test("unsupported agent key raises ProfileError", "not_a_real_agent" in str(exc))

print("\n\U0001f9ea Test 7: load_profile_document rejects a missing prompt file")
missing_prompt_document = minimal_profile_document("test-missing-prompt")
missing_prompt_document["agents"]["scout"]["prompt_file"] = "prompts/does_not_exist.txt"
with temp_profile("test-missing-prompt", missing_prompt_document) as profile_dir:
    try:
        load_profile_document(profile_dir, "test-missing-prompt")
        # profile.json validation itself doesn't check prompt file existence —
        # that happens when Settings actually loads the prompt. Confirm that path.
        settings = Settings(profile_id="test-missing-prompt")
        test("missing prompt file raises ProfileError", False)
    except ProfileError as exc:
        test("missing prompt file raises ProfileError", "does_not_exist.txt" in str(exc))


# ─── app.config.Settings — default ("power") profile behavior ──────────
print("\n\U0001f9ea Test 8: Settings() with the default profile is the 'power' reference deployment")
default_settings = Settings()
test("defaults to the 'power' profile", default_settings.profile_id == "power")
test("has all six agents", set(default_settings.agents) == set(AGENT_KEYS))
test("orchestrator is named Grid Coordinator", default_settings.agents["orchestrator"].name == "Grid Coordinator")
test(
    "cost_sentinel is named Cost & Capacity Analyst",
    default_settings.agents["cost_sentinel"].name == "Cost & Capacity Analyst",
)
test(
    "no agent sends a custom temperature by default",
    all(not cfg.supports_temperature for cfg in default_settings.agents.values()),
)
test(
    "unset secondary endpoint falls back to '' (not an error)",
    default_settings.agents["orchestrator"].endpoint == "",
)

print("\n\U0001f9ea Test 8b: default ('power') profile maps agents to the recommended GPT-5.6 tiers")
expected_power_mapping = {
    "orchestrator": ("Grid Coordinator", "gpt-5.6-sol"),
    "cost_sentinel": ("Cost & Capacity Analyst", "gpt-5.6-terra"),
    "standards_architect": ("Reliability Engineer", "gpt-5.6-terra"),
    "diagnostics_sre": ("Incident Investigator", "gpt-5.6-sol"),
    "scout": ("Operations Monitor", "gpt-5.6-luna"),
    "compliance_inspector": ("Compliance Advisor", "gpt-5.6-terra"),
}
for key, (expected_name, expected_deployment) in expected_power_mapping.items():
    cfg = default_settings.agents[key]
    test(f"{key} name is {expected_name!r}", cfg.name == expected_name)
    test(f"{key} deployment is {expected_deployment!r}", cfg.deployment == expected_deployment)
    test(f"{key} has a positive max_completion_tokens", cfg.max_completion_tokens > 0)
    test(f"{key} has a positive max_context_chars", cfg.max_context_chars > 0)
    test(f"{key} has a non-empty response_instruction", bool(cfg.response_instruction.strip()))
    test(f"{key} has positive pricing configured", cfg.input_cost_per_million > 0 and cfg.output_cost_per_million > 0)

print("\n\U0001f9ea Test 8c: legacy 'oge' profile is unaffected by the default changing")
oge_settings = Settings(profile_id="oge")
test("oge profile still loads", oge_settings.profile_id == "oge")
test("orchestrator is still named Pipeline", oge_settings.agents["orchestrator"].name == "Pipeline")
test("cost_sentinel is still named Barrel Counter", oge_settings.agents["cost_sentinel"].name == "Barrel Counter")
test("standards_architect is still named The Roughneck", oge_settings.agents["standards_architect"].name == "The Roughneck")
test("diagnostics_sre is still named Turnaround", oge_settings.agents["diagnostics_sre"].name == "Turnaround")
test("scout is still named Flare Stack", oge_settings.agents["scout"].name == "Flare Stack")
test("compliance_inspector is still named The Inspector", oge_settings.agents["compliance_inspector"].name == "The Inspector")
test(
    "oge gained explicit conservative controls without changing personality",
    oge_settings.agents["cost_sentinel"].max_completion_tokens > 0
    and "dollar figure" in oge_settings.agents["cost_sentinel"].response_instruction,
)

print("\n\U0001f9ea Test 9: Settings() with the 'generic' profile is fully re-branded")
generic_settings = Settings(profile_id="generic")
test("brand differs from the default profile", generic_settings.brand.app_name != default_settings.brand.app_name)
test("orchestrator renamed", generic_settings.agents["orchestrator"].name == "Orchestrator")
test("still has all six agents", set(generic_settings.agents) == set(AGENT_KEYS))
test(
    "generic profile also has explicit conservative controls",
    generic_settings.agents["orchestrator"].max_completion_tokens > 0
    and generic_settings.agents["orchestrator"].max_context_chars > 0,
)

# ─── app.config.Settings — invalid profile handling ────────────────────
print("\n\U0001f9ea Test 10: Settings() rejects an invalid/unknown APP_PROFILE")
try:
    Settings(profile_id="../../etc")
    test("path traversal profile_id raises ProfileError", False)
except ProfileError:
    test("path traversal profile_id raises ProfileError", True)

try:
    Settings(profile_id="does-not-exist")
    test("unknown profile_id raises ProfileError", False)
except ProfileError:
    test("unknown profile_id raises ProfileError", True)

# ─── app.config.Settings — per-agent environment variable overrides ────
print("\n\U0001f9ea Test 11: AGENT_<KEY>_* environment variables override profile defaults")
with temp_env(
    AGENT_COST_SENTINEL_NAME="Ledger",
    AGENT_COST_SENTINEL_DEPLOYMENT="gpt-4o",
    AGENT_COST_SENTINEL_TEMPERATURE="0.2",
    AGENT_COST_SENTINEL_SUPPORTS_TEMPERATURE="true",
):
    overridden = Settings()
    cfg = overridden.agents["cost_sentinel"]
    test("name overridden", cfg.name == "Ledger")
    test("deployment overridden", cfg.deployment == "gpt-4o")
    test("temperature overridden", cfg.temperature == 0.2)
    test("supports_temperature overridden", cfg.supports_temperature is True)
    test("other agents untouched", overridden.agents["orchestrator"].name == "Grid Coordinator")
    test(
        "overriding one field doesn't clobber that agent's other unrelated fields",
        overridden.agents["cost_sentinel"].max_completion_tokens
        == default_settings.agents["cost_sentinel"].max_completion_tokens,
    )

print("\n\U0001f9ea Test 12: malformed per-agent overrides raise explicit errors (no silent fallback)")
with temp_env(AGENT_SCOUT_TEMPERATURE="not-a-number"):
    try:
        Settings()
        test("bad AGENT_*_TEMPERATURE raises ProfileError", False)
    except ProfileError:
        test("bad AGENT_*_TEMPERATURE raises ProfileError", True)

with temp_env(AGENT_SCOUT_SUPPORTS_TEMPERATURE="maybe"):
    try:
        Settings()
        test("bad AGENT_*_SUPPORTS_TEMPERATURE raises ProfileError", False)
    except ProfileError:
        test("bad AGENT_*_SUPPORTS_TEMPERATURE raises ProfileError", True)

print("\n\U0001f9ea Test 13: endpoint reference resolution")
with temp_env(
    AZURE_OPENAI_ENDPOINT_SECONDARY="https://eu2.openai.azure.com/",
    # Forced via env override rather than relying on any particular
    # checked-in profile's own endpoint_ref default (only "oge" ships
    # with endpoint_ref: "secondary" out of the box) — this test is
    # about the resolution logic itself, not a specific profile.
    AGENT_ORCHESTRATOR_ENDPOINT="secondary",
):
    configured = Settings()
    test(
        "endpoint_ref 'secondary' resolves when configured",
        configured.agents["orchestrator"].endpoint == "https://eu2.openai.azure.com/",
    )
    test(
        "openai_endpoint_eastus2 stays in sync",
        configured.openai_endpoint_eastus2 == "https://eu2.openai.azure.com/",
    )

with temp_env(AGENT_SCOUT_ENDPOINT="totally-bogus-name"):
    try:
        Settings()
        test("unknown named endpoint ref raises ProfileError", False)
    except ProfileError:
        test("unknown named endpoint ref raises ProfileError", True)

with temp_env(AGENT_SCOUT_ENDPOINT="https://custom.openai.azure.com/"):
    literal_url_settings = Settings()
    test(
        "a literal https:// endpoint ref is used as-is",
        literal_url_settings.agents["scout"].endpoint == "https://custom.openai.azure.com/",
    )

# ─── app.profiles: strict schema validation of the new controls ────────
print("\n\U0001f9ea Test 13b: profile.json schema validation — max_completion_tokens / max_context_chars")
for bad_value, label in [(-1, "negative"), (1.5, "float"), ("900", "string")]:
    bad_tokens_doc = minimal_profile_document(f"test-bad-tokens-{label}")
    bad_tokens_doc["agents"]["scout"]["max_completion_tokens"] = bad_value
    with temp_profile(f"test-bad-tokens-{label}", bad_tokens_doc) as profile_dir:
        try:
            load_profile_document(profile_dir, f"test-bad-tokens-{label}")
            test(f"max_completion_tokens rejects {label} value", False)
        except ProfileError as exc:
            test(f"max_completion_tokens rejects {label} value", "max_completion_tokens" in str(exc))

    bad_context_doc = minimal_profile_document(f"test-bad-context-{label}")
    bad_context_doc["agents"]["scout"]["max_context_chars"] = bad_value
    with temp_profile(f"test-bad-context-{label}", bad_context_doc) as profile_dir:
        try:
            load_profile_document(profile_dir, f"test-bad-context-{label}")
            test(f"max_context_chars rejects {label} value", False)
        except ProfileError as exc:
            test(f"max_context_chars rejects {label} value", "max_context_chars" in str(exc))

zero_is_valid_doc = minimal_profile_document("test-zero-tokens-ok")
zero_is_valid_doc["agents"]["scout"]["max_completion_tokens"] = 0
zero_is_valid_doc["agents"]["scout"]["max_context_chars"] = 0
with temp_profile("test-zero-tokens-ok", zero_is_valid_doc) as profile_dir:
    document = load_profile_document(profile_dir, "test-zero-tokens-ok")
    test("0 is a valid max_completion_tokens (provider default)", document["agents"]["scout"]["max_completion_tokens"] == 0)
    test("0 is a valid max_context_chars (no truncation)", document["agents"]["scout"]["max_context_chars"] == 0)

print("\n\U0001f9ea Test 13c: profile.json schema validation — response_instruction")
empty_instruction_doc = minimal_profile_document("test-empty-instruction")
empty_instruction_doc["agents"]["scout"]["response_instruction"] = "   "
with temp_profile("test-empty-instruction", empty_instruction_doc) as profile_dir:
    try:
        load_profile_document(profile_dir, "test-empty-instruction")
        test("blank response_instruction is rejected", False)
    except ProfileError as exc:
        test("blank response_instruction is rejected", "response_instruction" in str(exc))

wrong_type_instruction_doc = minimal_profile_document("test-wrong-type-instruction")
wrong_type_instruction_doc["agents"]["scout"]["response_instruction"] = 12345
with temp_profile("test-wrong-type-instruction", wrong_type_instruction_doc) as profile_dir:
    try:
        load_profile_document(profile_dir, "test-wrong-type-instruction")
        test("non-string response_instruction is rejected", False)
    except ProfileError as exc:
        test("non-string response_instruction is rejected", "response_instruction" in str(exc))

print("\n\U0001f9ea Test 13d: profile.json schema validation — pricing fields")
for field_name in ("input_cost_per_million", "output_cost_per_million"):
    bad_pricing_doc = minimal_profile_document(f"test-bad-{field_name}")
    bad_pricing_doc["agents"]["scout"][field_name] = -1.0
    with temp_profile(f"test-bad-{field_name}", bad_pricing_doc) as profile_dir:
        try:
            load_profile_document(profile_dir, f"test-bad-{field_name}")
            test(f"negative {field_name} is rejected", False)
        except ProfileError as exc:
            test(f"negative {field_name} is rejected", field_name in str(exc))

zero_pricing_doc = minimal_profile_document("test-zero-pricing-ok")
zero_pricing_doc["agents"]["scout"]["input_cost_per_million"] = 0
zero_pricing_doc["agents"]["scout"]["output_cost_per_million"] = 0.0
with temp_profile("test-zero-pricing-ok", zero_pricing_doc) as profile_dir:
    document = load_profile_document(profile_dir, "test-zero-pricing-ok")
    test("0 is valid pricing (no cost estimate)", document["agents"]["scout"]["input_cost_per_million"] == 0)

# ─── app.config.Settings — env overrides for the new controls ─────────
print("\n\U0001f9ea Test 13e: AGENT_<KEY>_* env overrides for the new controls")
with temp_env(
    AGENT_SCOUT_MAX_COMPLETION_TOKENS="321",
    AGENT_SCOUT_MAX_CONTEXT_CHARS="4321",
    AGENT_SCOUT_RESPONSE_INSTRUCTION="Test-only override instruction.",
    AGENT_SCOUT_INPUT_COST_PER_MILLION="2.5",
    AGENT_SCOUT_OUTPUT_COST_PER_MILLION="7.5",
):
    overridden = Settings()
    cfg = overridden.agents["scout"]
    test("max_completion_tokens overridden", cfg.max_completion_tokens == 321)
    test("max_context_chars overridden", cfg.max_context_chars == 4321)
    test("response_instruction overridden", cfg.response_instruction == "Test-only override instruction.")
    test("input_cost_per_million overridden", cfg.input_cost_per_million == 2.5)
    test("output_cost_per_million overridden", cfg.output_cost_per_million == 7.5)

print("\n\U0001f9ea Test 13f: malformed env overrides for the new controls raise explicit errors")
for bad_env, context_hint in [
    ({"AGENT_SCOUT_MAX_COMPLETION_TOKENS": "not-a-number"}, "MAX_COMPLETION_TOKENS"),
    ({"AGENT_SCOUT_MAX_COMPLETION_TOKENS": "-5"}, "MAX_COMPLETION_TOKENS"),
    ({"AGENT_SCOUT_MAX_COMPLETION_TOKENS": "1.5"}, "MAX_COMPLETION_TOKENS"),
    ({"AGENT_SCOUT_MAX_CONTEXT_CHARS": "not-a-number"}, "MAX_CONTEXT_CHARS"),
    ({"AGENT_SCOUT_MAX_CONTEXT_CHARS": "-1"}, "MAX_CONTEXT_CHARS"),
    ({"AGENT_SCOUT_INPUT_COST_PER_MILLION": "not-a-number"}, "INPUT_COST_PER_MILLION"),
    ({"AGENT_SCOUT_INPUT_COST_PER_MILLION": "-0.5"}, "INPUT_COST_PER_MILLION"),
    ({"AGENT_SCOUT_OUTPUT_COST_PER_MILLION": "not-a-number"}, "OUTPUT_COST_PER_MILLION"),
]:
    with temp_env(**bad_env):
        try:
            Settings()
            test(f"bad {context_hint} env override raises ProfileError", False)
        except ProfileError as exc:
            test(f"bad {context_hint} env override raises ProfileError", context_hint in str(exc))

print("\n\U0001f9ea Test 13g: every checked-in profile agent has the new fields populated")
for profile_id in ("oge", "generic", "power"):
    settings_for_profile = Settings(profile_id=profile_id)
    for key, cfg in settings_for_profile.agents.items():
        test(
            f"{profile_id}.{key}: max_completion_tokens/max_context_chars are non-negative ints",
            isinstance(cfg.max_completion_tokens, int) and isinstance(cfg.max_context_chars, int)
            and cfg.max_completion_tokens >= 0 and cfg.max_context_chars >= 0,
        )
        test(
            f"{profile_id}.{key}: response_instruction is a non-empty string",
            isinstance(cfg.response_instruction, str) and bool(cfg.response_instruction.strip()),
        )
        test(
            f"{profile_id}.{key}: pricing fields are non-negative numbers",
            cfg.input_cost_per_million >= 0 and cfg.output_cost_per_million >= 0,
        )


# ─── scripts/configure.py — validation helpers ─────────────────────────
print("\n\U0001f9ea Test 14: wizard subscription ID validation")
test("valid UUID accepted", wizard.validate_subscription_id(
    "11111111-2222-3333-4444-555555555555") == "11111111-2222-3333-4444-555555555555")
try:
    wizard.validate_subscription_id("not-a-uuid")
    test("invalid subscription id rejected", False)
except wizard.ConfigureError:
    test("invalid subscription id rejected", True)

print("\n\U0001f9ea Test 15: wizard endpoint/resource-id validation")
test("https endpoint accepted + trailing slash added",
     wizard.validate_https_url("https://acct.openai.azure.com", "x") == "https://acct.openai.azure.com/")
try:
    wizard.validate_https_url("http://acct.openai.azure.com/", "x")
    test("http (non-https) endpoint rejected", False)
except wizard.ConfigureError:
    test("http (non-https) endpoint rejected", True)

test("valid resource id accepted", wizard.validate_resource_id(
    "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan", "x"
).startswith("/subscriptions/"))
try:
    wizard.validate_resource_id("not-a-resource-id", "x")
    test("invalid resource id rejected", False)
except wizard.ConfigureError:
    test("invalid resource id rejected", True)

print("\n\U0001f9ea Test 16: wizard per-agent override flag parsing")
for new_field in (
    "max_completion_tokens", "max_context_chars", "response_instruction",
    "input_cost_per_million", "output_cost_per_million",
):
    test(f"AGENT_OVERRIDE_FIELDS includes {new_field!r}", new_field in wizard.AGENT_OVERRIDE_FIELDS)

key, field_name, value = wizard.parse_agent_override_flag("cost_sentinel:deployment=foundry-reasoning")
test("parses key/field/value", (key, field_name, value) == ("cost_sentinel", "deployment", "foundry-reasoning"))
try:
    wizard.parse_agent_override_flag("not_a_real_agent:deployment=x")
    test("unknown agent key rejected", False)
except wizard.ConfigureError:
    test("unknown agent key rejected", True)
try:
    wizard.parse_agent_override_flag("cost_sentinel:not_a_real_field=x")
    test("unknown field name rejected", False)
except wizard.ConfigureError:
    test("unknown field name rejected", True)
try:
    wizard.parse_agent_override_flag("malformed-no-colon-or-equals")
    test("malformed flag format rejected", False)
except wizard.ConfigureError:
    test("malformed flag format rejected", True)

key, field_name, value = wizard.parse_agent_override_flag("scout:max_completion_tokens=400")
test(
    "parses a new token/response/pricing control field",
    (key, field_name, value) == ("scout", "max_completion_tokens", "400"),
)
key, field_name, value = wizard.parse_agent_override_flag("cost_sentinel:response_instruction=Lead with the number.")
test(
    "parses response_instruction (value may contain spaces)",
    (key, field_name, value) == ("cost_sentinel", "response_instruction", "Lead with the number."),
)

print("\n\U0001f9ea Test 16b: wizard snake_case -> Bicep camelCase field-name translation")
camel_case_cases = {
    "deployment": "deployment",
    "supports_temperature": "supportsTemperature",
    "api_version": "apiVersion",
    "prompt_file": "promptFile",
    "max_completion_tokens": "maxCompletionTokens",
    "max_context_chars": "maxContextChars",
    "response_instruction": "responseInstruction",
    "input_cost_per_million": "inputCostPerMillion",
    "output_cost_per_million": "outputCostPerMillion",
}
for snake_case, expected_camel_case in camel_case_cases.items():
    test(
        f"{snake_case} -> {expected_camel_case}",
        wizard._to_bicep_field_name(snake_case) == expected_camel_case,
    )

print("\n\U0001f9ea Test 17: wizard answer validation")
valid_answers = wizard.Answers(
    profile_id="oge",
    subscription_id="11111111-2222-3333-4444-555555555555",
    openai_endpoint="https://acct.openai.azure.com/",
    openai_account_name="acct",
    openai_resource_group="rg",
    app_service_plan_id="/subscriptions/s/resourceGroups/rg/providers/Microsoft.Web/serverfarms/plan",
)
test("fully valid answers pass", wizard.validate_answers(valid_answers) == [])

incomplete_answers = wizard.Answers()
errors = wizard.validate_answers(incomplete_answers)
test("missing required fields reported", len(errors) >= 5)

bad_profile_answers = copy.deepcopy(valid_answers)
bad_profile_answers.profile_id = "does-not-exist"
errors = wizard.validate_answers(bad_profile_answers)
test("unknown profile reported", any("not found" in e for e in errors))

print("\n\U0001f9ea Test 18: wizard never echoes secrets")
test("empty secret redacted", wizard.redact("") == "(not set)")
test("set secret redacted, value never included", wizard.redact("super-secret-pat") == "(set, hidden)")
test("redacted string never contains the raw secret", "super-secret-pat" not in wizard.redact("super-secret-pat"))

print("\n\U0001f9ea Test 19: wizard .env generation")
env_content = wizard.generate_env_content(valid_answers)
test("APP_PROFILE set", "APP_PROFILE=oge" in env_content)
test("AZURE_OPENAI_ENDPOINT set", "AZURE_OPENAI_ENDPOINT=https://acct.openai.azure.com/" in env_content)
test("AZURE_SUBSCRIPTION_ID set", "AZURE_SUBSCRIPTION_ID=11111111-2222-3333-4444-555555555555" in env_content)
test("no ADO section when unset", "ADO_PAT" not in env_content)

answers_with_agent_override = copy.deepcopy(valid_answers)
answers_with_agent_override.set_agent_override("cost_sentinel", "deployment", "foundry-reasoning")
env_with_override = wizard.generate_env_content(answers_with_agent_override)
test("per-agent override written", "AGENT_COST_SENTINEL_DEPLOYMENT=foundry-reasoning" in env_with_override)

answers_with_new_controls = copy.deepcopy(valid_answers)
answers_with_new_controls.set_agent_override("scout", "max_completion_tokens", "400")
answers_with_new_controls.set_agent_override("scout", "response_instruction", "Alert format only.")
env_with_new_controls = wizard.generate_env_content(answers_with_new_controls)
test("max_completion_tokens override written", "AGENT_SCOUT_MAX_COMPLETION_TOKENS=400" in env_with_new_controls)
test("response_instruction override written", "AGENT_SCOUT_RESPONSE_INSTRUCTION=Alert format only." in env_with_new_controls)

answers_with_secret = copy.deepcopy(valid_answers)
answers_with_secret.ado_pat = "super-secret-pat"
env_with_secret = wizard.generate_env_content(answers_with_secret)
test("ADO_PAT written to .env content itself (consumed by the app, not the terminal)",
     "ADO_PAT=super-secret-pat" in env_with_secret)

print("\n\U0001f9ea Test 20: wizard bicepparam generation")
bicepparam_content = wizard.generate_bicepparam_content(valid_answers)
test("uses main.bicep", "using './main.bicep'" in bicepparam_content)
test("subscriptionId set", "param subscriptionId = '11111111-2222-3333-4444-555555555555'" in bicepparam_content)
test("appProfile set", "param appProfile = 'oge'" in bicepparam_content)
test("empty agentOverrides renders as {}", "param agentOverrides = {}" in bicepparam_content)

bicepparam_with_override = wizard.generate_bicepparam_content(answers_with_agent_override)
test("agentOverrides entry rendered", "cost_sentinel: { deployment: 'foundry-reasoning' }" in bicepparam_with_override)
test("otelServiceName always emitted (default blank)", "param otelServiceName = ''" in bicepparam_content)

bicepparam_with_new_controls = wizard.generate_bicepparam_content(answers_with_new_controls)
test(
    "new control fields are translated to camelCase in the Bicep object literal",
    "scout: { maxCompletionTokens: '400', responseInstruction: 'Alert format only.' }" in bicepparam_with_new_controls,
)

answers_with_legacy_snake_case_fields = copy.deepcopy(valid_answers)
answers_with_legacy_snake_case_fields.set_agent_override("cost_sentinel", "supports_temperature", "true")
answers_with_legacy_snake_case_fields.set_agent_override("cost_sentinel", "api_version", "2025-04-01-preview")
answers_with_legacy_snake_case_fields.set_agent_override("cost_sentinel", "prompt_file", "prompts/custom.txt")
bicepparam_with_legacy_fields = wizard.generate_bicepparam_content(answers_with_legacy_snake_case_fields)
test(
    "supports_temperature/api_version/prompt_file also reach Bicep as camelCase "
    "(previously would have been silently dropped by web-app.bicep's camelCase lookup)",
    "supportsTemperature: 'true'" in bicepparam_with_legacy_fields
    and "apiVersion: '2025-04-01-preview'" in bicepparam_with_legacy_fields
    and "promptFile: 'prompts/custom.txt'" in bicepparam_with_legacy_fields,
)
test(
    "no raw snake_case field name leaks into the Bicep object literal",
    "supports_temperature:" not in bicepparam_with_legacy_fields
    and "api_version:" not in bicepparam_with_legacy_fields
    and "prompt_file:" not in bicepparam_with_legacy_fields,
)

print("\n\U0001f9ea Test 21: wizard write_file refuses to write outside the repository")
try:
    wizard.ensure_within_repo(wizard.REPO_ROOT.parent / "should-not-be-written.env")
    test("path escaping the repo raises ConfigureError", False)
except wizard.ConfigureError:
    test("path escaping the repo raises ConfigureError", True)
test("in-repo path passes", wizard.ensure_within_repo(REPO_ROOT / "infra" / "main.bicepparam.example").exists())

print("\n\U0001f9ea Test 22: wizard --new-profile creates a valid, loadable profile")
new_profile_answers = wizard.Answers(
    new_profile=True,
    profile_id="test-wizard-new-profile",
    clone_from="generic",
    app_name="Test Wizard Co Ops",
    customer="Test Wizard Co",
    industry="Testing",
)
try:
    dest_dir = wizard.create_profile(new_profile_answers)
    document = load_profile_document(dest_dir, "test-wizard-new-profile")
    test("new profile id set", document["id"] == "test-wizard-new-profile")
    test("new profile app_name set", document["brand"]["app_name"] == "Test Wizard Co Ops")
    test("new profile still has all six agents", set(document["agents"]) == set(AGENT_KEYS))
    new_profile_settings = Settings(profile_id="test-wizard-new-profile")
    test("new profile loads via Settings", new_profile_settings.brand.customer == "Test Wizard Co")
finally:
    shutil.rmtree(PROFILES_ROOT / "test-wizard-new-profile", ignore_errors=True)

try:
    wizard.create_profile(wizard.Answers(new_profile=True, profile_id="oge", clone_from="generic"))
    test("cloning into an existing profile id is rejected", False)
except wizard.ConfigureError:
    test("cloning into an existing profile id is rejected", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
