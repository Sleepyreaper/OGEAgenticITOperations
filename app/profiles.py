"""Profile loading and validation for the reusable multi-agent platform.

A "profile" is a checked-in directory under ``profiles/<id>/`` that supplies
branding metadata, per-agent display/model configuration, and system prompt
files. The ``APP_PROFILE`` environment variable selects which profile the
running app loads (see app/config.py). Customers create new profiles by
copying ``profiles/generic`` to a new directory and editing it — the ``oge``
profile reproduces this app's original default behavior exactly.

This module has zero third-party dependencies and no import-time side
effects (it does not read the environment or instantiate anything), so it
is safe to import from both the Flask app (app/config.py) and stdlib-only
tooling such as scripts/configure.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_ROOT = REPO_ROOT / "profiles"

DEFAULT_PROFILE = "oge"
DEFAULT_API_VERSION = "2025-01-01-preview"

# app/agents/runner.py, app/main.py, and app/agents/demos.py hardcode these
# six agent keys throughout the orchestration/debate logic and the public
# API (e.g. the "agents" arrays accepted by /api/ask and returned by demo
# scenarios). Profiles configure exactly these six — changing the *set* of
# agents is a larger architectural change than this config layer covers.
AGENT_KEYS: tuple[str, ...] = (
    "orchestrator",
    "cost_sentinel",
    "standards_architect",
    "diagnostics_sre",
    "scout",
    "compliance_inspector",
)

# Lowercase kebab/underscore identifier, 1-50 chars. Deliberately excludes
# "/", "\", "." and other path-traversal-relevant characters.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")

_REQUIRED_BRAND_KEYS = (
    "app_name",           # short name, e.g. nav label + chat welcome message
    "app_title",          # full <title> tag text
    "tagline_line1",      # nav top small-caps line
    "tagline_line2",      # nav subtitle line
    "executive_subtitle", # Executive/Reliability view header subtitle
    "logo_path",          # web-relative path, e.g. "/static/my-logo.svg"
    "logo_alt",            # <img alt="...">
)
_OPTIONAL_BRAND_KEYS = ("customer", "industry")

_REQUIRED_AGENT_KEYS = ("name", "role", "deployment", "prompt_file")
_OPTIONAL_AGENT_KEYS = ("endpoint_ref", "temperature", "supports_temperature", "api_version")


class ProfileError(ValueError):
    """Raised when a profile id, directory, or profile.json is invalid.

    Deliberately a plain, explicit exception (not silently swallowed) —
    malformed configuration must fail loudly at startup.
    """


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_valid_profile_id(profile_id: str) -> bool:
    return bool(_PROFILE_ID_RE.fullmatch(profile_id or ""))


def list_profiles() -> list[str]:
    """Return the ids of all checked-in profiles (dirs with a profile.json)."""
    if not PROFILES_ROOT.is_dir():
        return []
    return sorted(
        p.name
        for p in PROFILES_ROOT.iterdir()
        if p.is_dir() and is_valid_profile_id(p.name) and (p / "profile.json").is_file()
    )


def resolve_profile_dir(profile_id: str) -> Path:
    """Resolve ``profile_id`` to a directory under PROFILES_ROOT.

    Rejects anything that isn't a plain identifier (blocking path traversal
    such as "../../etc" or absolute paths) and anything that doesn't resolve
    to a real, in-bounds directory.
    """
    if not is_valid_profile_id(profile_id):
        raise ProfileError(
            f"Invalid profile id {profile_id!r}: must match ^[a-z0-9][a-z0-9_-]{{0,49}}$ "
            "(lowercase letters, digits, hyphens, underscores only)."
        )
    profiles_root_resolved = PROFILES_ROOT.resolve()
    candidate = (PROFILES_ROOT / profile_id).resolve()
    try:
        candidate.relative_to(profiles_root_resolved)
    except ValueError:
        raise ProfileError(
            f"Invalid profile id {profile_id!r}: resolves outside {profiles_root_resolved}."
        )
    if not candidate.is_dir():
        available = list_profiles()
        raise ProfileError(
            f"Profile {profile_id!r} not found: expected a directory at {candidate}. "
            f"Available profiles: {available or '(none)'}."
        )
    return candidate


def load_profile_document(profile_dir: Path, profile_id: str) -> dict:
    """Load and strictly validate ``profiles/<id>/profile.json``.

    Collects every problem found and raises a single ProfileError describing
    all of them, rather than failing on (and hiding) just the first one.
    """
    profile_json = profile_dir / "profile.json"
    if not profile_json.is_file():
        raise ProfileError(f"Profile {profile_id!r} is missing profile.json at {profile_json}.")

    raw = profile_json.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"Profile {profile_id!r}: profile.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"Profile {profile_id!r}: profile.json must contain a JSON object.")

    errors: list[str] = []

    for key in ("id", "brand", "agents"):
        if key not in data:
            errors.append(f"missing required top-level key '{key}'")
    if "id" in data and data["id"] != profile_id:
        errors.append(
            f"profile.json 'id' ({data['id']!r}) does not match directory name ({profile_id!r})"
        )

    brand = data.get("brand")
    if brand is not None and not isinstance(brand, dict):
        errors.append("'brand' must be a JSON object")
    elif isinstance(brand, dict):
        for key in _REQUIRED_BRAND_KEYS:
            value = brand.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"brand.{key} must be a non-empty string")
        for key in _OPTIONAL_BRAND_KEYS:
            if key in brand and not isinstance(brand[key], str):
                errors.append(f"brand.{key} must be a string")
        unknown_brand_keys = set(brand) - set(_REQUIRED_BRAND_KEYS) - set(_OPTIONAL_BRAND_KEYS)
        if unknown_brand_keys:
            errors.append(f"brand has unknown keys: {sorted(unknown_brand_keys)}")

    agents = data.get("agents")
    if agents is not None and not isinstance(agents, dict):
        errors.append("'agents' must be a JSON object")
    elif isinstance(agents, dict):
        missing_agents = set(AGENT_KEYS) - set(agents)
        extra_agents = set(agents) - set(AGENT_KEYS)
        if missing_agents:
            errors.append(f"agents is missing required keys: {sorted(missing_agents)}")
        if extra_agents:
            errors.append(
                f"agents has unsupported keys (this app only supports {list(AGENT_KEYS)}): "
                f"{sorted(extra_agents)}"
            )
        for agent_key, agent_data in agents.items():
            if agent_key not in AGENT_KEYS:
                continue
            if not isinstance(agent_data, dict):
                errors.append(f"agents.{agent_key} must be a JSON object")
                continue
            for key in _REQUIRED_AGENT_KEYS:
                value = agent_data.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"agents.{agent_key}.{key} must be a non-empty string")
            if "temperature" in agent_data and not _is_number(agent_data["temperature"]):
                errors.append(f"agents.{agent_key}.temperature must be a number")
            if "supports_temperature" in agent_data and not isinstance(
                agent_data["supports_temperature"], bool
            ):
                errors.append(f"agents.{agent_key}.supports_temperature must be a boolean")
            if "api_version" in agent_data and not isinstance(agent_data["api_version"], str):
                errors.append(f"agents.{agent_key}.api_version must be a string")
            if "endpoint_ref" in agent_data and not isinstance(agent_data["endpoint_ref"], str):
                errors.append(f"agents.{agent_key}.endpoint_ref must be a string")
            unknown_agent_keys = (
                set(agent_data) - set(_REQUIRED_AGENT_KEYS) - set(_OPTIONAL_AGENT_KEYS)
            )
            if unknown_agent_keys:
                errors.append(f"agents.{agent_key} has unknown keys: {sorted(unknown_agent_keys)}")

    if errors:
        raise ProfileError(
            f"Profile {profile_id!r} is malformed ({len(errors)} problem(s)):\n  - "
            + "\n  - ".join(errors)
        )

    return data


def load_prompt(profile_dir: Path, prompt_file: str, context: str) -> str:
    """Load a per-agent system prompt file, staying inside the profile dir."""
    profile_dir_resolved = profile_dir.resolve()
    candidate = (profile_dir / prompt_file).resolve()
    try:
        candidate.relative_to(profile_dir_resolved)
    except ValueError:
        raise ProfileError(f"{context}: prompt_file {prompt_file!r} escapes the profile directory.")
    if not candidate.is_file():
        raise ProfileError(f"{context}: prompt_file {prompt_file!r} not found at {candidate}.")
    text = candidate.read_text(encoding="utf-8").strip()
    if not text:
        raise ProfileError(f"{context}: prompt_file {prompt_file!r} is empty.")
    return text
