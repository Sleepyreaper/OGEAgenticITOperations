#!/usr/bin/env python3
"""Setup wizard for deploying this app for a new customer/profile.

Generates local, git-ignored deployment artifacts:

  - .env                     (local dev environment / App Service settings reference)
  - infra/main.bicepparam    (Bicep deployment parameters)

Never modifies the checked-in `.env.example` / `infra/main.bicepparam.example`
templates, and never prints secrets (e.g. the optional Azure DevOps PAT).

Stdlib only — no third-party packages required to run this script, so it
works in a fresh clone before `pip install -r requirements.txt`.

Usage:
    python3 scripts/configure.py                        # interactive wizard
    python3 scripts/configure.py --non-interactive ...   # scripted / CI, via flags
    python3 scripts/configure.py --answers answers.json  # fully scripted, via file
    python3 scripts/configure.py --list-profiles         # list checked-in profiles
    python3 scripts/configure.py --help

Examples:
    # Fully automated, single Azure OpenAI account, default "power" profile:
    python3 scripts/configure.py --non-interactive \\
        --profile power \\
        --subscription-id 11111111-2222-3333-4444-555555555555 \\
        --openai-endpoint https://my-account.openai.azure.com/ \\
        --openai-account-name my-account --openai-resource-group rg-openai \\
        --app-service-plan-id /subscriptions/.../serverfarms/my-plan

    # Create a new branded profile cloned from "generic", with per-agent overrides:
    python3 scripts/configure.py --non-interactive \\
        --new-profile contoso --clone-from generic \\
        --app-name "Contoso Ops" --customer Contoso --industry Retail \\
        --subscription-id 11111111-2222-3333-4444-555555555555 \\
        --openai-endpoint https://contoso-openai.openai.azure.com/ \\
        --openai-account-name contoso-openai --openai-resource-group rg-openai \\
        --app-service-plan-id /subscriptions/.../serverfarms/contoso-plan \\
        --agent cost_sentinel:deployment=foundry-reasoning \\
        --agent cost_sentinel:endpoint=secondary \\
        --agent cost_sentinel:max_completion_tokens=900 \\
        --agent cost_sentinel:response_instruction="Lead with the dollar figure, 3-5 sentences."

    # Existing profile ("oge" is the checked-in legacy/example profile;
    # use --profile <your-id> for anything else), tightening one agent's
    # token/response controls without forking the profile:
    python3 scripts/configure.py --non-interactive --profile oge \\
        --subscription-id 11111111-2222-3333-4444-555555555555 \\
        --openai-endpoint https://my-account.openai.azure.com/ \\
        --openai-account-name my-account --openai-resource-group rg-openai \\
        --app-service-plan-id /subscriptions/.../serverfarms/my-plan \\
        --agent scout:max_completion_tokens=300 \\
        --agent scout:max_context_chars=8000
"""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Only import from app.profiles — it (and app/__init__.py) has zero
# third-party dependencies, so this script stays stdlib-only even though
# it lives inside the `app` package's sibling tree.
from app.profiles import (  # noqa: E402
    AGENT_KEYS,
    DEFAULT_API_VERSION,
    DEFAULT_PROFILE,
    PROFILES_ROOT,
    ProfileError,
    is_valid_profile_id,
    list_profiles,
    load_profile_document,
    resolve_profile_dir,
)

AGENT_OVERRIDE_FIELDS = (
    "name", "role", "deployment", "endpoint",
    "temperature", "supports_temperature", "api_version", "prompt_file",
    # Enforceable token/response/personality controls (see
    # docs/MODEL_CONFIGURATION.md). Values are always passed through as
    # plain strings here — app/config.py does the strict int/float/bool
    # parsing and raises ProfileError on anything malformed at app startup.
    "max_completion_tokens", "max_context_chars", "response_instruction",
    "input_cost_per_million", "output_cost_per_million",
)

_SUBSCRIPTION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_KNOWN_OPENAI_HOST_HINTS = ("openai.azure.com", "cognitiveservices.azure.com", "services.ai.azure.com")


class ConfigureError(ValueError):
    """Raised for invalid wizard input/answers — always caught and reported cleanly."""


# ─── Validation helpers (shared by interactive and non-interactive paths) ──

def validate_subscription_id(value: str) -> str:
    value = (value or "").strip()
    if not _SUBSCRIPTION_ID_RE.match(value):
        raise ConfigureError(
            f"'{value}' doesn't look like an Azure subscription ID "
            "(expected a UUID, e.g. 11111111-2222-3333-4444-555555555555)."
        )
    return value


def validate_https_url(value: str, label: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigureError(f"{label} must be a full https:// URL, got {value!r}.")
    if not value.endswith("/"):
        value += "/"
    return value


def warn_if_unexpected_openai_host(value: str) -> str | None:
    """Return a warning string (not an error) if the host doesn't look like Azure OpenAI/AI Foundry."""
    host = urlparse(value).netloc.lower()
    if any(hint in host for hint in _KNOWN_OPENAI_HOST_HINTS):
        return None
    return (
        f"warning: '{host}' doesn't look like a standard Azure OpenAI / AI Foundry "
        "endpoint host — double check it's correct."
    )


def validate_resource_id(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value.startswith("/subscriptions/") or "/resourceGroups/" not in value:
        raise ConfigureError(
            f"{label} must be a full Azure resource ID "
            f"(e.g. /subscriptions/<id>/resourceGroups/<rg>/providers/...), got {value!r}."
        )
    return value


def validate_profile_choice(value: str) -> str:
    value = (value or "").strip()
    if not is_valid_profile_id(value):
        raise ConfigureError(
            f"{value!r} is not a valid profile id "
            "(lowercase letters, digits, hyphens, underscores only)."
        )
    return value


def validate_agent_key(value: str) -> str:
    if value not in AGENT_KEYS:
        raise ConfigureError(f"{value!r} is not a known agent key. Must be one of: {list(AGENT_KEYS)}.")
    return value


def parse_agent_override_flag(raw: str) -> tuple[str, str, str]:
    """Parse '--agent KEY:FIELD=VALUE' into (key, field, value)."""
    match = re.match(r"^([a-z_]+):([a-z_]+)=(.*)$", raw)
    if not match:
        raise ConfigureError(
            f"--agent value {raw!r} must look like 'agent_key:field=value', "
            f"e.g. 'cost_sentinel:deployment=foundry-reasoning'."
        )
    key, field_name, value = match.groups()
    validate_agent_key(key)
    if field_name not in AGENT_OVERRIDE_FIELDS:
        raise ConfigureError(
            f"{field_name!r} is not a supported agent field. Must be one of: {list(AGENT_OVERRIDE_FIELDS)}."
        )
    return key, field_name, value


# ─── Answers model ─────────────────────────────────────────────────────

@dataclass
class Answers:
    profile_id: str = DEFAULT_PROFILE
    new_profile: bool = False
    clone_from: str = "generic"
    app_name: str = ""
    customer: str = ""
    industry: str = ""

    subscription_id: str = ""
    openai_endpoint: str = ""
    openai_endpoint_secondary: str = ""
    openai_deployment: str = "foundry-gpt"
    openai_api_version: str = DEFAULT_API_VERSION
    openai_account_name: str = ""
    openai_resource_group: str = ""

    prefix: str = "opscouncil"
    location: str = "westus2"
    app_service_plan_id: str = ""
    deployer_principal_id: str = ""
    public_network_access: str = "Disabled"

    azure_client_id: str = ""
    key_vault_uri: str = ""
    log_analytics_workspace_id: str = ""
    # OpenTelemetry service.name (see docs/TELEMETRY.md). Blank (default)
    # lets the app derive a profile-safe "ops-council-<profile_id>" name
    # at startup instead.
    otel_service_name: str = ""

    # agent_key -> {field: value}
    agent_overrides: dict = field(default_factory=dict)

    ado_org_url: str = ""
    ado_project: str = ""
    ado_repo: str = ""
    ado_pat: str = ""  # never logged/printed — see redact_for_display()

    def set_agent_override(self, key: str, field_name: str, value: str) -> None:
        self.agent_overrides.setdefault(key, {})[field_name] = value


REQUIRED_FIELDS = (
    "subscription_id",
    "openai_endpoint",
    "openai_account_name",
    "openai_resource_group",
    "app_service_plan_id",
)


def validate_answers(answers: Answers) -> list[str]:
    """Return a list of problems (empty = valid). Applied regardless of source."""
    errors: list[str] = []
    for name in REQUIRED_FIELDS:
        if not getattr(answers, name):
            errors.append(f"'{name}' is required.")

    if answers.subscription_id:
        try:
            validate_subscription_id(answers.subscription_id)
        except ConfigureError as exc:
            errors.append(str(exc))

    if answers.openai_endpoint:
        try:
            validate_https_url(answers.openai_endpoint, "openai-endpoint")
        except ConfigureError as exc:
            errors.append(str(exc))

    if answers.openai_endpoint_secondary:
        try:
            validate_https_url(answers.openai_endpoint_secondary, "openai-endpoint-secondary")
        except ConfigureError as exc:
            errors.append(str(exc))

    if answers.app_service_plan_id:
        try:
            validate_resource_id(answers.app_service_plan_id, "app-service-plan-id")
        except ConfigureError as exc:
            errors.append(str(exc))

    if answers.new_profile:
        if not is_valid_profile_id(answers.profile_id):
            errors.append(f"new profile id {answers.profile_id!r} is invalid.")
        if not is_valid_profile_id(answers.clone_from):
            errors.append(f"clone-from {answers.clone_from!r} is invalid.")
        elif answers.clone_from not in list_profiles():
            errors.append(f"clone-from profile {answers.clone_from!r} not found.")
        if not answers.app_name.strip():
            errors.append("--app-name is required when creating a new profile.")
        if (PROFILES_ROOT / answers.profile_id).exists():
            errors.append(
                f"profiles/{answers.profile_id} already exists — pick a different id or edit it directly "
                "instead of using --new-profile."
            )
    else:
        if not is_valid_profile_id(answers.profile_id):
            errors.append(f"profile id {answers.profile_id!r} is invalid.")
        elif answers.profile_id not in list_profiles():
            errors.append(
                f"profile {answers.profile_id!r} not found. Available: {list_profiles()}."
            )

    if answers.public_network_access not in ("Enabled", "Disabled"):
        errors.append("public-network-access must be 'Enabled' or 'Disabled'.")

    return errors


# ─── Interactive prompts ───────────────────────────────────────────────

def _prompt(label: str, default: str = "", validator=None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            raw = getpass.getpass(f"{label}{suffix}: ")
        else:
            raw = input(f"{label}{suffix}: ").strip()
        value = raw or default
        if validator is None:
            return value
        try:
            return validator(value)
        except ConfigureError as exc:
            print(f"  ! {exc}")


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{label} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _make_existing_profile_validator(existing: list[str]):
    """Build a validator confirming the answer names a checked-in profile."""

    def _validate(value: str) -> str:
        if value not in existing:
            raise ConfigureError(f"{value!r} not found. Available: {existing}.")
        return value

    return _validate


def build_answers_interactive() -> Answers:
    answers = Answers()
    print("=" * 60)
    print(" Setup wizard — reusable multi-agent Azure operations app")
    print("=" * 60)
    print()

    existing = list_profiles()
    print(f"Checked-in profiles: {existing}")
    if _prompt_yes_no("Create a NEW profile (rebrand for a new customer)?", default=False):
        answers.new_profile = True
        answers.clone_from = _prompt(
            "Clone which existing profile as a starting point?", default="generic",
            validator=_make_existing_profile_validator(existing),
        )
        answers.profile_id = _prompt(
            "New profile id (kebab-case, becomes profiles/<id>/)", validator=validate_profile_choice
        )
        answers.app_name = _prompt("App display name (e.g. 'Contoso Ops Council')")
        while not answers.app_name.strip():
            print("  ! app name is required for a new profile.")
            answers.app_name = _prompt("App display name")
        answers.customer = _prompt("Customer name (optional, for docs/branding)", default="")
        answers.industry = _prompt("Industry (optional, for docs/branding)", default="")
    else:
        answers.profile_id = _prompt(
            "Which profile should this deployment use?", default=DEFAULT_PROFILE,
            validator=_make_existing_profile_validator(existing),
        )

    print()
    print("── Azure environment ──")
    answers.subscription_id = _prompt(
        "Azure subscription ID to monitor", validator=validate_subscription_id
    )
    answers.prefix = _prompt("Resource naming prefix", default=answers.prefix or "opscouncil")
    answers.location = _prompt("Azure region", default=answers.location or "westus2")
    answers.app_service_plan_id = _prompt(
        "Existing App Service Plan resource ID",
        validator=lambda v: validate_resource_id(v, "App Service Plan resource ID"),
    )
    answers.public_network_access = "Enabled" if _prompt_yes_no(
        "Make the web app reachable directly over the public internet (simpler standalone demo, weaker isolation)?",
        default=False,
    ) else "Disabled"

    print()
    print("── Azure OpenAI ──")
    answers.openai_endpoint = _prompt(
        "Primary Azure OpenAI endpoint", validator=lambda v: validate_https_url(v, "openai-endpoint")
    )
    warning = warn_if_unexpected_openai_host(answers.openai_endpoint)
    if warning:
        print(f"  {warning}")
    answers.openai_account_name = _prompt("Primary Azure OpenAI account name")
    answers.openai_resource_group = _prompt("Resource group containing that account")
    answers.openai_deployment = _prompt("Default model deployment name", default="foundry-gpt")
    answers.openai_api_version = _prompt("Default API version", default=DEFAULT_API_VERSION)

    if _prompt_yes_no("Configure a second Azure OpenAI endpoint (for models in another account/region)?", default=False):
        answers.openai_endpoint_secondary = _prompt(
            "Secondary Azure OpenAI endpoint",
            validator=lambda v: validate_https_url(v, "openai-endpoint-secondary"),
        )

    print()
    print("── Per-agent deployment/endpoint (blank = use profile defaults) ──")
    profile_dir = resolve_profile_dir(answers.clone_from if answers.new_profile else answers.profile_id)
    document = load_profile_document(profile_dir, profile_dir.name)
    if _prompt_yes_no("Customize per-agent deployment/endpoint now?", default=False):
        for key in AGENT_KEYS:
            agent_doc = document["agents"][key]
            print(f"  {key} (profile default: {agent_doc['name']!r}, deployment={agent_doc['deployment']!r})")
            deployment = _prompt(f"    {key} deployment name", default="")
            if deployment:
                answers.set_agent_override(key, "deployment", deployment)
            endpoint = _prompt(f"    {key} endpoint (primary/secondary/https://... )", default="")
            if endpoint:
                answers.set_agent_override(key, "endpoint", endpoint)

    print()
    print("── Optional: Azure DevOps integration (Phase 2 proposals) ──")
    if _prompt_yes_no("Configure Azure DevOps integration now?", default=False):
        answers.ado_org_url = _prompt("ADO organization URL", default="")
        answers.ado_project = _prompt("ADO project", default="")
        answers.ado_repo = _prompt("ADO repo", default="")
        answers.ado_pat = _prompt("ADO personal access token (input hidden)", secret=True)

    return answers


# ─── Non-interactive / CLI-flag path ────────────────────────────────────

def build_answers_non_interactive(args: argparse.Namespace) -> Answers:
    answers = Answers()

    if args.answers:
        try:
            raw = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigureError(f"could not read --answers file: {exc}") from exc
        for key, value in raw.items():
            if key == "agent_overrides":
                for agent_key, fields_ in (value or {}).items():
                    for field_name, field_value in (fields_ or {}).items():
                        answers.set_agent_override(agent_key, field_name, str(field_value))
                continue
            if not hasattr(answers, key):
                raise ConfigureError(f"unknown key {key!r} in --answers file.")
            setattr(answers, key, value)

    # Individual CLI flags always take precedence over the answers file.
    if args.new_profile:
        answers.new_profile = True
        answers.profile_id = args.new_profile
        answers.clone_from = args.clone_from
    elif args.profile:
        answers.new_profile = False
        answers.profile_id = args.profile

    if args.app_name is not None:
        answers.app_name = args.app_name
    if args.customer is not None:
        answers.customer = args.customer
    if args.industry is not None:
        answers.industry = args.industry

    if args.subscription_id is not None:
        answers.subscription_id = args.subscription_id
    if args.openai_endpoint is not None:
        answers.openai_endpoint = args.openai_endpoint
    if args.openai_endpoint_secondary is not None:
        answers.openai_endpoint_secondary = args.openai_endpoint_secondary
    if args.openai_deployment is not None:
        answers.openai_deployment = args.openai_deployment
    if args.openai_api_version is not None:
        answers.openai_api_version = args.openai_api_version
    if args.openai_account_name is not None:
        answers.openai_account_name = args.openai_account_name
    if args.openai_resource_group is not None:
        answers.openai_resource_group = args.openai_resource_group

    if args.prefix is not None:
        answers.prefix = args.prefix
    if args.location is not None:
        answers.location = args.location
    if args.app_service_plan_id is not None:
        answers.app_service_plan_id = args.app_service_plan_id
    if args.deployer_principal_id is not None:
        answers.deployer_principal_id = args.deployer_principal_id
    if args.public_network_access is not None:
        answers.public_network_access = args.public_network_access
    if args.otel_service_name is not None:
        answers.otel_service_name = args.otel_service_name

    if args.azure_client_id is not None:
        answers.azure_client_id = args.azure_client_id
    if args.key_vault_uri is not None:
        answers.key_vault_uri = args.key_vault_uri
    if args.log_analytics_workspace_id is not None:
        answers.log_analytics_workspace_id = args.log_analytics_workspace_id

    for raw in args.agent or []:
        key, field_name, value = parse_agent_override_flag(raw)
        answers.set_agent_override(key, field_name, value)

    if not args.skip_ado:
        if args.ado_org_url is not None:
            answers.ado_org_url = args.ado_org_url
        if args.ado_project is not None:
            answers.ado_project = args.ado_project
        if args.ado_repo is not None:
            answers.ado_repo = args.ado_repo
        # ADO_PAT is deliberately NOT a CLI flag (shell history / process list
        # exposure). Pick it up from the environment if already set, or from
        # the --answers file (a local file the user controls) — never log it.
        answers.ado_pat = os.environ.get(args.ado_pat_env, "") or answers.ado_pat

    return answers


# ─── Output generation ──────────────────────────────────────────────────

def redact(value: str) -> str:
    if not value:
        return "(not set)"
    return "(set, hidden)"


def new_profile_brand(clone_doc: dict, answers: Answers) -> dict:
    """Derive a new profile's brand block from the clone template + wizard answers."""
    brand = copy.deepcopy(clone_doc["brand"])
    brand["app_name"] = answers.app_name
    brand["app_title"] = f"{answers.app_name} — Multi-Agent Cloud Operations Intelligence"
    brand["customer"] = answers.customer
    brand["industry"] = answers.industry
    if answers.industry:
        brand["tagline_line1"] = answers.industry.upper()
    if answers.customer:
        brand["tagline_line2"] = f"{answers.customer} \u00b7 Ops Council"
        brand["executive_subtitle"] = (
            f"{answers.customer} \u00b7 {answers.industry}" if answers.industry else f"{answers.customer} \u00b7 Ops Council"
        )
    return brand


def create_profile(answers: Answers) -> Path:
    """Clone profiles/<clone_from>/ to profiles/<profile_id>/ and rebrand it."""
    source_dir = resolve_profile_dir(answers.clone_from)
    dest_dir = PROFILES_ROOT / answers.profile_id
    if dest_dir.exists():
        raise ConfigureError(f"profiles/{answers.profile_id} already exists.")

    shutil.copytree(source_dir, dest_dir)

    clone_doc = load_profile_document(source_dir, answers.clone_from)
    new_doc = copy.deepcopy(clone_doc)
    new_doc["id"] = answers.profile_id
    new_doc["brand"] = new_profile_brand(clone_doc, answers)

    profile_json_path = dest_dir / "profile.json"
    profile_json_path.write_text(json.dumps(new_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Created profiles/{answers.profile_id}/ (cloned from profiles/{answers.clone_from}/).")
    print(
        f"  Edit profiles/{answers.profile_id}/profile.json and "
        f"profiles/{answers.profile_id}/prompts/*.txt to finish customizing branding and agent prompts."
    )
    return dest_dir


def generate_env_content(answers: Answers) -> str:
    lines = [
        "# Generated by scripts/configure.py — do not commit (see .gitignore).",
        f"# Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "# Re-run scripts/configure.py to regenerate. See .env.example for full docs.",
        "",
        f"APP_PROFILE={answers.profile_id}",
        "",
        "# ── Azure OpenAI ──",
        f"AZURE_OPENAI_ENDPOINT={answers.openai_endpoint}",
        f"AZURE_OPENAI_DEPLOYMENT={answers.openai_deployment}",
        f"AZURE_OPENAI_API_VERSION={answers.openai_api_version}",
    ]
    if answers.openai_endpoint_secondary:
        lines.append(f"AZURE_OPENAI_ENDPOINT_SECONDARY={answers.openai_endpoint_secondary}")

    if answers.agent_overrides:
        lines.append("")
        lines.append("# ── Per-agent overrides ──")
        for key in AGENT_KEYS:
            fields_ = answers.agent_overrides.get(key)
            if not fields_:
                continue
            for field_name, value in fields_.items():
                env_name = f"AGENT_{key.upper()}_{field_name.upper()}"
                lines.append(f"{env_name}={value}")

    lines += [
        "",
        "# ── Azure environment ──",
        f"AZURE_CLIENT_ID={answers.azure_client_id}",
        f"AZURE_SUBSCRIPTION_ID={answers.subscription_id}",
        f"KEY_VAULT_URI={answers.key_vault_uri}",
        f"LOG_ANALYTICS_WORKSPACE_ID={answers.log_analytics_workspace_id}",
    ]

    if answers.ado_org_url or answers.ado_project or answers.ado_repo or answers.ado_pat:
        lines += [
            "",
            "# ── Azure DevOps integration (Phase 2 proposals) ──",
            f"ADO_ORG_URL={answers.ado_org_url}",
            f"ADO_PROJECT={answers.ado_project}",
            f"ADO_REPO={answers.ado_repo}",
            f"ADO_PAT={answers.ado_pat}",
        ]

    return "\n".join(lines) + "\n"


def _bicep_object_literal(pairs: dict, indent: str = "  ") -> str:
    if not pairs:
        return "{}"
    lines = ["{"]
    for key, value in pairs.items():
        inner = ", ".join(f"{k}: '{v}'" for k, v in value.items())
        lines.append(f"{indent}{key}: {{ {inner} }}")
    lines.append("}")
    return "\n".join(lines)


def _to_bicep_field_name(field_name: str) -> str:
    """Translate a snake_case agent-override field name (as used for env
    var suffixes / ``--agent`` flags / ``--answers`` JSON) to the
    camelCase key ``agentOverrides`` expects in Bicep (see
    ``infra/main.bicep`` / ``infra/modules/web-app.bicep``). Fields with
    no underscore (e.g. ``deployment``) are returned unchanged.
    """
    head, *rest = field_name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def generate_bicepparam_content(answers: Answers) -> str:
    additional_accounts: dict = {}
    if answers.openai_endpoint_secondary:
        additional_accounts["secondary"] = {
            "endpoint": answers.openai_endpoint_secondary,
            "accountName": "",
            "resourceGroup": "",
        }

    agent_overrides_literal = "{}"
    if answers.agent_overrides:
        entries = []
        for key in AGENT_KEYS:
            fields_ = answers.agent_overrides.get(key)
            if not fields_:
                continue
            # Bicep's agentOverrides schema uses camelCase field names
            # (supportsTemperature, apiVersion, promptFile, ...) while the
            # env-var/CLI/answers-file side of this wizard uses snake_case
            # (matching AGENT_<KEY>_<FIELD> env var suffixes) — translate
            # here so a --agent/--answers override actually reaches the
            # app when deployed via Bicep instead of being silently
            # dropped by an unrecognized key.
            inner = ", ".join(f"{_to_bicep_field_name(k)}: '{v}'" for k, v in fields_.items())
            entries.append(f"  {key}: {{ {inner} }}")
        agent_overrides_literal = "{\n" + "\n".join(entries) + "\n}" if entries else "{}"

    additional_accounts_literal = "{}"
    if additional_accounts:
        entries = []
        for key, value in additional_accounts.items():
            inner = ", ".join(f"{k}: '{v}'" for k, v in value.items())
            entries.append(f"  {key}: {{ {inner} }}")
        additional_accounts_literal = "{\n" + "\n".join(entries) + "\n}"

    return "\n".join([
        "// Generated by scripts/configure.py — do not commit (see .gitignore).",
        f"// Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "// Re-run scripts/configure.py to regenerate. See main.bicepparam.example for full docs.",
        "",
        "using './main.bicep'",
        "",
        f"param prefix = '{answers.prefix}'",
        f"param location = '{answers.location}'",
        f"param appProfile = '{answers.profile_id}'",
        f"param existingAppServicePlanId = '{answers.app_service_plan_id}'",
        f"param subscriptionId = '{answers.subscription_id}'",
        f"param openaiAccountName = '{answers.openai_account_name}'",
        f"param openaiResourceGroup = '{answers.openai_resource_group}'",
        f"param openaiEndpoint = '{answers.openai_endpoint}'",
        f"param openaiDeploymentName = '{answers.openai_deployment}'",
        f"param openaiApiVersion = '{answers.openai_api_version}'",
        f"param additionalOpenAiAccounts = {additional_accounts_literal}",
        f"param agentOverrides = {agent_overrides_literal}",
        f"param otelServiceName = '{answers.otel_service_name}'",
        f"param publicNetworkAccess = '{answers.public_network_access}'",
        f"param deployerPrincipalId = '{answers.deployer_principal_id}'",
        "",
    ])


def ensure_within_repo(path: Path) -> Path:
    """Resolve `path` and confirm it's inside REPO_ROOT. Raises ConfigureError otherwise."""
    resolved = path.resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ConfigureError(f"refusing to write outside the repository: {path}") from exc
    return resolved


def write_file(path: Path, content: str, force: bool, dry_run: bool) -> None:
    ensure_within_repo(path)

    if dry_run:
        print(f"--- would write {path.relative_to(REPO_ROOT)} ---")
        print(content)
        return

    if path.exists() and not force:
        raise ConfigureError(f"{path.relative_to(REPO_ROOT)} already exists. Pass --force to overwrite.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"Wrote {path.relative_to(REPO_ROOT)}")


# ─── CLI ─────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configure.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--non-interactive", action="store_true",
                         help="Skip all prompts; use --answers/CLI flags only. Fails if required values are missing.")
    parser.add_argument("--answers", type=Path, default=None,
                         help="JSON file with pre-filled answers (see Answers fields); CLI flags override it.")
    parser.add_argument("--list-profiles", action="store_true", help="List checked-in profiles and exit.")

    profile_group = parser.add_argument_group("profile selection")
    profile_group.add_argument("--profile", default=None, help="Use an existing checked-in profile (default: power).")
    profile_group.add_argument("--new-profile", default=None, metavar="ID",
                                help="Create a new profile with this id (cloned from --clone-from).")
    profile_group.add_argument("--clone-from", default="generic", help="Profile to clone when using --new-profile.")
    profile_group.add_argument("--app-name", default=None, help="Display name for a new profile.")
    profile_group.add_argument("--customer", default=None, help="Customer name for a new profile (optional).")
    profile_group.add_argument("--industry", default=None, help="Industry for a new profile (optional).")

    azure_group = parser.add_argument_group("Azure environment")
    azure_group.add_argument("--subscription-id", default=None, help="Azure subscription ID to monitor.")
    azure_group.add_argument("--prefix", default=None, help="Resource naming prefix (default: opscouncil).")
    azure_group.add_argument("--location", default=None, help="Azure region (default: westus2).")
    azure_group.add_argument("--app-service-plan-id", default=None, help="Existing App Service Plan resource ID.")
    azure_group.add_argument("--deployer-principal-id", default=None, help="Deployer AAD object ID (optional).")
    azure_group.add_argument("--public-network-access", choices=["Enabled", "Disabled"], default=None,
                              help="Whether the web app is reachable over the public internet (default: Disabled).")
    azure_group.add_argument("--azure-client-id", default=None, help="Managed identity client ID (usually left for Bicep output).")
    azure_group.add_argument("--key-vault-uri", default=None, help="Key Vault URI (usually left for Bicep output).")
    azure_group.add_argument("--log-analytics-workspace-id", default=None, help="Log Analytics workspace ID (usually left for Bicep output).")

    observability_group = parser.add_argument_group("Observability")
    observability_group.add_argument(
        "--otel-service-name", default=None,
        help="OpenTelemetry service.name reported to Application Insights (default: a profile-safe "
             "'ops-council-<profile>' derived by the app at startup). See docs/TELEMETRY.md.",
    )

    openai_group = parser.add_argument_group("Azure OpenAI")
    openai_group.add_argument("--openai-endpoint", default=None, help="Primary Azure OpenAI endpoint (https://...).")
    openai_group.add_argument("--openai-endpoint-secondary", default=None, help="Optional secondary Azure OpenAI endpoint.")
    openai_group.add_argument("--openai-deployment", default=None, help="Default model deployment name (default: foundry-gpt).")
    openai_group.add_argument("--openai-api-version", default=None, help=f"Default API version (default: {DEFAULT_API_VERSION}).")
    openai_group.add_argument("--openai-account-name", default=None, help="Primary Azure OpenAI account name.")
    openai_group.add_argument("--openai-resource-group", default=None, help="Resource group containing that account.")
    openai_group.add_argument("--agent", action="append", default=[], metavar="KEY:FIELD=VALUE",
                               help="Repeatable per-agent override, e.g. cost_sentinel:deployment=foundry-reasoning.")

    ado_group = parser.add_argument_group("Azure DevOps (optional, Phase 2 proposals)")
    ado_group.add_argument("--skip-ado", action="store_true", help="Don't configure Azure DevOps integration.")
    ado_group.add_argument("--ado-org-url", default=None)
    ado_group.add_argument("--ado-project", default=None)
    ado_group.add_argument("--ado-repo", default=None)
    ado_group.add_argument("--ado-pat-env", default="ADO_PAT",
                            help="Environment variable to read the ADO PAT from (never a CLI flag; default: ADO_PAT).")

    out_group = parser.add_argument_group("output")
    out_group.add_argument("--env-out", type=Path, default=REPO_ROOT / ".env", help="Where to write the .env file.")
    out_group.add_argument("--bicepparam-out", type=Path, default=REPO_ROOT / "infra" / "main.bicepparam",
                            help="Where to write the Bicep parameters file.")
    out_group.add_argument("--force", action="store_true", help="Overwrite existing generated files.")
    out_group.add_argument("--dry-run", action="store_true", help="Print what would be written, without writing.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_profiles:
        for profile_id in list_profiles():
            print(profile_id)
        return 0

    try:
        if args.non_interactive or args.answers:
            answers = build_answers_non_interactive(args)
        else:
            answers = build_answers_interactive()

        errors = validate_answers(answers)
        if errors:
            print("Configuration is invalid:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1

        if answers.new_profile:
            create_profile(answers)

        # Re-validate the (new or existing) profile loads cleanly before
        # generating deployment artifacts that reference it.
        profile_dir = resolve_profile_dir(answers.profile_id)
        load_profile_document(profile_dir, answers.profile_id)

        env_content = generate_env_content(answers)
        bicepparam_content = generate_bicepparam_content(answers)

        write_file(args.env_out, env_content, args.force, args.dry_run)
        write_file(args.bicepparam_out, bicepparam_content, args.force, args.dry_run)

        if not args.dry_run:
            print()
            print("Summary (secrets redacted):")
            print(f"  profile:              {answers.profile_id}")
            print(f"  subscription:         {answers.subscription_id}")
            print(f"  openai endpoint:      {answers.openai_endpoint}")
            print(f"  openai endpoint (2):  {answers.openai_endpoint_secondary or '(not set)'}")
            print(f"  ado pat:              {redact(answers.ado_pat)}")
            print()
            print("Next steps:")
            print("  1. Review infra/main.bicepparam and .env (both git-ignored).")
            if answers.new_profile:
                print(f"  2. Edit profiles/{answers.profile_id}/profile.json and prompts/*.txt to finish branding.")
            print("  3. Deploy model deployments in Azure AI Foundry, then: cd infra && bash deploy.sh")
            print("  4. Grant RBAC (see DEPLOYMENT.md), deploy the app, then verify with GET /api/health.")

        return 0

    except (ConfigureError, ProfileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
