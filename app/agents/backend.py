"""Model backend protocol -- the seam between agent orchestration
(app/agents/analysis.py) and the actual model call, so a future Azure AI
Foundry Agent Service integration is a second implementation of this
same protocol, not a rewrite of the orchestration logic above it.

``DirectAzureOpenAIBackend`` is what this app actually runs today: it
calls Azure OpenAI's chat.completions API directly (the same
``AzureOpenAI`` client construction as app/agents/runner.py::call_agent,
reused here -- see the import below), attempting structured output
(``response_format={"type": "json_schema", ...}``) when the agent's
profile config allows it, with an explicit, safe fallback to a plain
completion (parsed by app/agents/schema.py) when the deployment doesn't
support that.

``FoundryAgentServiceBackend`` is NOT implemented -- it raises
``NotImplementedError`` on every call. This is intentional and honest:
this app does not use Azure AI Foundry Agent Service at runtime today,
and this module must never claim otherwise (see
docs/FOUNDRY_ARCHITECTURE.md for the concrete migration plan). Setting
``AGENT_BACKEND=foundry`` fails loudly at call time rather than silently
falling back to Direct while claiming to be using Foundry.
"""

import os
from dataclasses import dataclass
from typing import Optional, Protocol

import openai

from app import telemetry
from app.agents.runner import _get_client, estimate_cost_usd
from app.config import AgentConfig

__all__ = [
    "BackendCompletion",
    "ModelBackend",
    "DirectAzureOpenAIBackend",
    "FoundryConfig",
    "FoundryAgentServiceBackend",
    "get_backend",
    "backend_health",
]


@dataclass(frozen=True)
class BackendCompletion:
    agent: str
    role: str
    model: str
    raw_text: str
    structured_output_used: bool
    usage: dict
    finish_reason: Optional[str] = None


class ModelBackend(Protocol):
    """Any backend app/agents/analysis.py can call. `json_schema`/
    `schema_name` are optional -- a backend that can't honor structured
    output must still return SOME raw_text (structured_output_used=False)
    so the caller's fallback parser (app/agents/schema.py) can try."""

    name: str

    def complete(
        self, agent_config: AgentConfig, messages: list, *, json_schema: Optional[dict] = None, schema_name: str = "",
    ) -> BackendCompletion: ...


class DirectAzureOpenAIBackend:
    """Calls Azure OpenAI directly -- the only backend this app actually
    runs today."""

    name = "direct_azure_openai"

    def complete(
        self, agent_config: AgentConfig, messages: list, *, json_schema: Optional[dict] = None, schema_name: str = "",
    ) -> BackendCompletion:
        client, deployment = _get_client(agent_config.deployment, agent_config.endpoint, agent_config.api_version)

        base_kwargs = {"model": deployment, "messages": messages}
        if agent_config.supports_temperature:
            base_kwargs["temperature"] = agent_config.temperature
        if agent_config.max_completion_tokens > 0:
            base_kwargs["max_completion_tokens"] = agent_config.max_completion_tokens

        response = None
        structured_output_used = False
        if json_schema is not None and agent_config.supports_structured_output:
            structured_kwargs = dict(base_kwargs)
            structured_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name or "structured_response", "strict": True, "schema": json_schema},
            }
            try:
                response = self._call(client, agent_config, structured_kwargs)
                structured_output_used = True
            except openai.BadRequestError:
                # The deployment doesn't support this response_format --
                # fall through to the plain completion below. This is
                # the ONLY exception type swallowed here; anything else
                # (auth, rate limit, timeout, ...) propagates unchanged.
                response = None

        if response is None:
            response = self._call(client, agent_config, base_kwargs)

        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens
        total_tokens = getattr(response.usage, "total_tokens", None) or (prompt_tokens + completion_tokens)
        cost_usd = estimate_cost_usd(agent_config, prompt_tokens, completion_tokens)
        finish_reason = response.choices[0].finish_reason if response.choices else None

        telemetry.record_usage(
            agent_key=agent_config.key, model=deployment,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=cost_usd,
        )

        return BackendCompletion(
            agent=agent_config.name, role=agent_config.role, model=agent_config.deployment,
            raw_text=response.choices[0].message.content or "",
            structured_output_used=structured_output_used,
            usage={
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": total_tokens, "estimated_cost_usd": cost_usd,
            },
            finish_reason=finish_reason,
        )

    @staticmethod
    def _call(client, agent_config: AgentConfig, kwargs: dict):
        with telemetry.agent_call_span(
            agent_key=agent_config.key, agent_name=agent_config.name,
            profile_id=_profile_id(), model=kwargs["model"],
        ) as span:
            response = client.chat.completions.create(**kwargs)
            finish_reason = response.choices[0].finish_reason if response.choices else None
            span.set_response_model(getattr(response, "model", None))
            span.set_usage(response.usage.prompt_tokens, response.usage.completion_tokens)
            span.set_finish_reasons([finish_reason] if finish_reason else None)
            span.set_cost(estimate_cost_usd(agent_config, response.usage.prompt_tokens, response.usage.completion_tokens))
        return response


def _profile_id() -> str:
    from app.config import settings  # local import: avoid a module-load-order cycle with app.config

    return settings.profile_id


@dataclass(frozen=True)
class FoundryConfig:
    """Inert configuration metadata for a FUTURE Azure AI Foundry Agent
    Service integration -- read from the environment but never acted on
    by DirectAzureOpenAIBackend. See docs/FOUNDRY_ARCHITECTURE.md."""

    project_endpoint: str = ""
    agent_id: str = ""
    model_deployment: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.project_endpoint and self.agent_id)

    @classmethod
    def from_env(cls) -> "FoundryConfig":
        return cls(
            project_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip(),
            agent_id=os.environ.get("FOUNDRY_AGENT_ID", "").strip(),
            model_deployment=os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "").strip(),
        )


class FoundryAgentServiceBackend:
    """NOT implemented. Exists so the ``ModelBackend`` protocol has a
    concrete second implementation to design against, and so
    ``AGENT_BACKEND=foundry`` fails loudly and specifically rather than
    with an import error or a silent fallback to Direct."""

    name = "foundry_agent_service"

    def __init__(self, config: Optional[FoundryConfig] = None):
        self.config = config or FoundryConfig.from_env()

    def complete(
        self, agent_config: AgentConfig, messages: list, *, json_schema: Optional[dict] = None, schema_name: str = "",
    ) -> BackendCompletion:
        raise NotImplementedError(
            "Foundry Agent Service backend is not implemented in this runtime -- this app calls "
            "Azure OpenAI directly via DirectAzureOpenAIBackend. See docs/FOUNDRY_ARCHITECTURE.md "
            "for the concrete migration plan (threads-per-incident, managed identity auth, typed "
            "tool registration, tracing, and evaluations) before setting AGENT_BACKEND=foundry."
        )


def get_backend(name: str = "") -> ModelBackend:
    """Return the configured/requested backend. `name` (or, if blank,
    the ``AGENT_BACKEND`` environment variable, default ``"direct"``)
    selects which one -- an unrecognized value raises ValueError rather
    than silently defaulting."""
    backend_name = (name or os.environ.get("AGENT_BACKEND", "direct")).strip().lower()
    if backend_name in ("direct", "direct_azure_openai", ""):
        return DirectAzureOpenAIBackend()
    if backend_name in ("foundry", "foundry_agent_service"):
        return FoundryAgentServiceBackend(FoundryConfig.from_env())
    raise ValueError(f"Unknown AGENT_BACKEND {backend_name!r}; expected 'direct' or 'foundry'.")


def backend_health() -> dict:
    """Honest backend metadata for /api/health -- `foundry_implemented`
    is hardcoded False; it is not a runtime-detected value, because
    there is nothing to detect yet (see FoundryAgentServiceBackend)."""
    foundry_cfg = FoundryConfig.from_env()
    return {
        "active_backend": (os.environ.get("AGENT_BACKEND", "direct").strip().lower() or "direct"),
        "direct_azure_openai_available": True,
        "foundry_configured": foundry_cfg.configured,
        "foundry_implemented": False,
    }
