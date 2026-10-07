"""Model backend protocol -- the seam between agent orchestration
(app/agents/analysis.py) and the actual model call, so a future Azure AI
Foundry Agent Service integration is a second implementation of this
same protocol, not a rewrite of the orchestration logic above it.

``DirectAzureOpenAIBackend`` is the operator-selected direct alternative:
it calls Azure OpenAI's chat.completions API directly (the same
``AzureOpenAI`` client construction as app/agents/runner.py::call_agent,
reused here -- see the import below), attempting structured output
(``response_format={"type": "json_schema", ...}``) when the agent's
profile config allows it, with an explicit, safe fallback to a plain
completion (parsed by app/agents/schema.py) when the deployment doesn't
support that.

``FoundryAgentServiceBackend`` (``AGENT_BACKEND=foundry``) runs each
specialist as a versioned Azure AI Foundry prompt agent, invoked through
the Foundry project's Responses API with read-only function tools from
app/agents/tools.py executed locally under a server-bound subscription
scope. See docs/FOUNDRY_ARCHITECTURE.md.
"""

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional, Protocol

import openai

from app import telemetry
from app.agents.runner import _get_client, estimate_cost_usd
from app.config import AgentConfig

__all__ = [
    "BackendCompletion",
    "ModelBackend",
    "DirectAzureOpenAIBackend",
    "ToolContext",
    "FoundryConfig",
    "FoundryAgentServiceBackend",
    "foundry_agent_name",
    "model_facing_tool_schema",
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
    backend_name: str = ""
    provider_response_ids: list = field(default_factory=list)
    tool_receipts: list = field(default_factory=list)
    round_limit_reached: bool = False


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
    """Calls Azure OpenAI directly when the operator selects the direct
    backend."""

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
            backend_name=self.name,
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
class ToolContext:
    """Server-bound scope for model-initiated tool calls. The model never
    chooses which subscriptions a tool reads -- the caller (app/agents/
    analysis.py) binds the same subscription scope the evidence bundle was
    built from, and the backend injects it into every tool call."""

    subscription_ids: tuple
    config: Optional[object] = None
    caller_roles: frozenset = frozenset({"operations_reader"})


@dataclass(frozen=True)
class FoundryConfig:
    """Azure AI Foundry Agent Service settings (see docs/FOUNDRY_ARCHITECTURE.md).

    ``project_endpoint`` is the Foundry project endpoint
    (``https://<resource>.services.ai.azure.com/api/projects/<project>``).
    ``model_deployment`` optionally overrides every agent's own
    ``deployment`` (useful when one Foundry project hosts all agents on a
    single model); blank keeps per-agent deployments.
    """

    project_endpoint: str = ""
    agent_prefix: str = "oge-ops"
    model_deployment: str = ""
    enable_tools: bool = True
    max_tool_rounds: int = 4
    max_tool_output_chars: int = 12000

    @property
    def configured(self) -> bool:
        return bool(self.project_endpoint)

    @classmethod
    def from_env(cls) -> "FoundryConfig":
        return cls(
            project_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip(),
            agent_prefix=(os.environ.get("FOUNDRY_AGENT_PREFIX", "").strip() or "oge-ops"),
            model_deployment=os.environ.get("FOUNDRY_MODEL_DEPLOYMENT", "").strip(),
            enable_tools=_env_bool("FOUNDRY_ENABLE_TOOLS", True),
            max_tool_rounds=_env_int("FOUNDRY_MAX_TOOL_ROUNDS", 4, minimum=0),
            max_tool_output_chars=_env_int("FOUNDRY_MAX_TOOL_OUTPUT_CHARS", 12000, minimum=1000),
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {raw!r}")


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# Arguments the server binds itself; never exposed to (or trusted from) the model.
_SERVER_BOUND_TOOL_ARGS = ("subscription_ids", "force_refresh")
_AGENT_NAME_MAX = 63


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def foundry_agent_name(prefix: str, agent_key: str, schema_name: str = "") -> str:
    """Deterministic Foundry agent name per (specialist, output schema).
    Foundry names: alphanumeric start/end, hyphens inside, <= 63 chars."""
    parts = [_slug(prefix), _slug(agent_key)]
    if schema_name:
        parts.append(_slug(schema_name))
    name = "-".join(p for p in parts if p)
    if len(name) > _AGENT_NAME_MAX:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = name[: _AGENT_NAME_MAX - 9].rstrip("-") + "-" + digest
    return name


def model_facing_tool_schema(schema: dict) -> dict:
    """Strip server-bound arguments from a tool's JSON Schema before it is
    registered with Foundry, so the model can neither see nor choose them."""
    properties = {k: v for k, v in schema.get("properties", {}).items() if k not in _SERVER_BOUND_TOOL_ARGS}
    required = [r for r in schema.get("required", []) if r not in _SERVER_BOUND_TOOL_ARGS]
    return {**schema, "properties": properties, "required": required}


def _to_input_items(agent_config: AgentConfig, messages: list) -> list:
    """Convert chat messages to Responses API input items. The agent's
    system prompt already lives in the Foundry agent version's
    ``instructions``, so an identical leading system message is dropped;
    any other system message becomes a ``developer`` message."""
    items = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            if content == agent_config.system_prompt:
                continue
            role = "developer"
        items.append({"type": "message", "role": role, "content": content})
    return items


def _default_credential():
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    client_id = os.environ.get("AZURE_CLIENT_ID")
    return ManagedIdentityCredential(client_id=client_id) if client_id else DefaultAzureCredential()


class FoundryAgentServiceBackend:
    """Runs each specialist as a versioned Azure AI Foundry prompt agent.

    * One Foundry agent per (agent_key, output schema). Its version holds
      the profile system prompt, model deployment, structured-output
      schema, and the read-only tool registry (app/agents/tools.py). A new
      version is published only when that definition's fingerprint changes.
    * Calls go through the project's OpenAI-compatible Responses API with
      an ``agent_reference``; ``function_call`` items are executed locally
      via ``app.agents.tools.execute_tool`` (validation, authorization,
      timeouts, result bounds unchanged) for at most ``max_tool_rounds``.
    * Auth is Entra ID only (managed identity / DefaultAzureCredential).
    * Token usage across every round is emitted through the same
      ``app.telemetry`` spans/counters the direct backend uses.
    """

    name = "foundry_agent_service"
    supports_tool_context = True

    _lock = threading.Lock()
    _published: dict = {}

    def __init__(self, config: Optional[FoundryConfig] = None, *, project_client=None, credential=None):
        self.config = config or FoundryConfig.from_env()
        self._project_client = project_client
        self._openai_client = None
        self._credential = credential

    def _project(self):
        if self._project_client is None:
            if not self.config.configured:
                raise RuntimeError(
                    "AGENT_BACKEND=foundry requires FOUNDRY_PROJECT_ENDPOINT "
                    "(https://<resource>.services.ai.azure.com/api/projects/<project>)."
                )
            from azure.ai.projects import AIProjectClient

            self._project_client = AIProjectClient(
                endpoint=self.config.project_endpoint, credential=self._credential or _default_credential(),
            )
        return self._project_client

    def _responses_client(self):
        if self._openai_client is None:
            self._openai_client = self._project().get_openai_client()
        return self._openai_client

    def _definition(self, agent_config: AgentConfig, json_schema: Optional[dict], schema_name: str):
        from azure.ai.projects.models import FunctionTool, PromptAgentDefinition, PromptAgentDefinitionTextOptions
        from app.agents import tools as tools_module

        model = self.config.model_deployment or agent_config.deployment
        kwargs = {"model": model, "instructions": agent_config.system_prompt}
        if agent_config.supports_temperature:
            kwargs["temperature"] = agent_config.temperature
        if self.config.enable_tools:
            kwargs["tools"] = [
                FunctionTool(
                    name=tool.name, description=tool.description,
                    parameters=model_facing_tool_schema(tool.parameters_schema), strict=False,
                )
                for tool in tools_module.TOOLS.values()
            ]
        if json_schema is not None and agent_config.supports_structured_output:
            kwargs["text"] = PromptAgentDefinitionTextOptions(format={
                "type": "json_schema", "name": schema_name or "structured_response", "strict": True, "schema": json_schema,
            })
        fingerprint_source = {
            "model": model, "instructions": agent_config.system_prompt,
            "temperature": kwargs.get("temperature"),
            "tools": [
                {"name": t.name, "description": t.description, "parameters": model_facing_tool_schema(t.parameters_schema)}
                for t in tools_module.TOOLS.values()
            ] if self.config.enable_tools else [],
            "schema": kwargs.get("text") and {"name": schema_name, "schema": json_schema},
        }
        fingerprint = hashlib.sha256(json.dumps(fingerprint_source, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return PromptAgentDefinition(**kwargs), model, fingerprint, "text" in kwargs

    def ensure_agent(self, agent_config: AgentConfig, json_schema: Optional[dict] = None, schema_name: str = "") -> tuple:
        """Publish (or reuse) the Foundry agent version for this specialist.
        Returns ``(agent_name, model_deployment, structured_output_used)``."""
        from azure.core.exceptions import ResourceNotFoundError

        agent_name = foundry_agent_name(self.config.agent_prefix, agent_config.key, schema_name)
        definition, model, fingerprint, structured = self._definition(agent_config, json_schema, schema_name)
        cache_key = (self.config.project_endpoint, agent_name)
        with self._lock:
            if self._published.get(cache_key) == fingerprint:
                return agent_name, model, structured
            project = self._project()
            current = None
            try:
                current = project.agents.get(agent_name=agent_name)
            except ResourceNotFoundError:
                current = None
            latest = getattr(getattr(current, "versions", None), "latest", None) if current is not None else None
            latest_meta = (getattr(latest, "metadata", None) or {}) if latest is not None else {}
            if latest_meta.get("definition_hash") != fingerprint:
                project.agents.create_version(
                    agent_name=agent_name, definition=definition,
                    description=f"{agent_config.name} -- {agent_config.role}"[:500],
                    metadata={
                        "definition_hash": fingerprint, "agent_key": agent_config.key,
                        "prompt_version": agent_config.prompt_version or "", "managed_by": "oge-agentic-ops",
                    },
                )
            self._published[cache_key] = fingerprint
        return agent_name, model, structured

    def complete(
        self, agent_config: AgentConfig, messages: list, *, json_schema: Optional[dict] = None, schema_name: str = "",
        tool_context: Optional[ToolContext] = None,
    ) -> BackendCompletion:
        agent_name, model, structured = self.ensure_agent(agent_config, json_schema, schema_name)
        client = self._responses_client()
        agent_ref = {"agent_reference": {"name": agent_name, "type": "agent_reference"}}
        base_kwargs = {"extra_body": agent_ref}
        if agent_config.max_completion_tokens > 0:
            base_kwargs["max_output_tokens"] = agent_config.max_completion_tokens

        prompt_tokens = completion_tokens = tool_calls = 0
        request_input = _to_input_items(agent_config, messages)
        previous_id = None
        rounds = 0
        finish_reason = None
        actual_model = model
        provider_response_ids = []
        tool_receipts = []
        round_limit_reached = False
        while True:
            kwargs = dict(base_kwargs, input=request_input)
            if previous_id:
                kwargs["previous_response_id"] = previous_id
            response = self._call(client, agent_config, model, kwargs)
            response_id = getattr(response, "id", None)
            if response_id:
                provider_response_ids.append(str(response_id))
            actual_model = getattr(response, "model", None) or actual_model
            usage = getattr(response, "usage", None)
            prompt_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            completion_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            calls = [item for item in (response.output or []) if getattr(item, "type", "") == "function_call"]
            finish_reason = getattr(response, "status", None)
            if not calls:
                break
            if rounds >= self.config.max_tool_rounds:
                finish_reason = "tool_round_limit"
                round_limit_reached = True
                break
            rounds += 1
            tool_calls += len(calls)
            request_input = []
            for call in calls:
                tool_output, receipt = self._run_tool(
                    call, tool_context, agent_key=agent_config.key, round_number=rounds,
                )
                request_input.append(tool_output)
                if receipt is not None:
                    tool_receipts.append(receipt)
            previous_id = response.id

        cost_usd = estimate_cost_usd(agent_config, prompt_tokens, completion_tokens)
        telemetry.record_usage(
            agent_key=agent_config.key, model=actual_model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=cost_usd,
        )
        return BackendCompletion(
            agent=agent_config.name, role=agent_config.role, model=actual_model,
            raw_text=getattr(response, "output_text", "") or "",
            structured_output_used=structured,
            usage={
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens, "estimated_cost_usd": cost_usd,
                "tool_calls": tool_calls, "tool_rounds": rounds, "foundry_agent": agent_name,
            },
            finish_reason=finish_reason,
            backend_name=self.name,
            provider_response_ids=provider_response_ids,
            tool_receipts=tool_receipts,
            round_limit_reached=round_limit_reached,
        )

    def _run_tool(
        self,
        call,
        tool_context: Optional[ToolContext],
        *,
        agent_key: str,
        round_number: int,
    ) -> tuple:
        from app.agents import tools as tools_module

        receipt = None
        if tool_context is None or not tool_context.subscription_ids:
            payload = {"status": "error", "error": "tool calls are unavailable: no server-bound subscription scope"}
        else:
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                arguments = None
            if not isinstance(arguments, dict):
                payload = {"status": "invalid_arguments", "error": "arguments must be a JSON object"}
            else:
                for key in _SERVER_BOUND_TOOL_ARGS:
                    arguments.pop(key, None)
                arguments["subscription_ids"] = list(tool_context.subscription_ids)
                result = tools_module.execute_tool(
                    call.name, arguments, caller_roles=set(tool_context.caller_roles), config=tool_context.config,
                )
                payload = result.to_dict()
                receipt = {
                    "agent_key": agent_key,
                    "round": round_number,
                    "tool_name": call.name,
                    "status": result.status,
                    "duration_ms": round(float(result.duration_ms or 0.0), 3),
                    "result_count": int(result.result_count or 0),
                }
        output = json.dumps(payload, default=str)
        if len(output) > self.config.max_tool_output_chars:
            if receipt is not None:
                receipt["status"] = "truncated"
            output = json.dumps({
                "status": "truncated", "tool_name": call.name,
                "partial": output[: self.config.max_tool_output_chars],
            })
        return {"type": "function_call_output", "call_id": call.call_id, "output": output}, receipt

    @staticmethod
    def _call(client, agent_config: AgentConfig, model: str, kwargs: dict):
        with telemetry.agent_call_span(
            agent_key=agent_config.key, agent_name=agent_config.name, profile_id=_profile_id(), model=model,
        ) as span:
            response = client.responses.create(**kwargs)
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            span.set_response_model(getattr(response, "model", None))
            span.set_usage(input_tokens, output_tokens)
            status = getattr(response, "status", None)
            span.set_finish_reasons([status] if status else None)
            span.set_cost(estimate_cost_usd(agent_config, input_tokens, output_tokens))
        return response


_FOUNDRY_BACKENDS: dict = {}
_FOUNDRY_BACKENDS_LOCK = threading.Lock()


def get_backend(name: str = "") -> ModelBackend:
    """Return the configured/requested backend. `name` (or, if blank,
    the ``AGENT_BACKEND`` environment variable, default ``"direct"``)
    selects which one -- an unrecognized value raises ValueError rather
    than silently defaulting."""
    backend_name = (name or os.environ.get("AGENT_BACKEND", "direct")).strip().lower()
    if backend_name in ("direct", "direct_azure_openai", ""):
        return DirectAzureOpenAIBackend()
    if backend_name in ("foundry", "foundry_agent_service"):
        config = FoundryConfig.from_env()
        with _FOUNDRY_BACKENDS_LOCK:
            backend = _FOUNDRY_BACKENDS.get(config)
            if backend is None:
                backend = _FOUNDRY_BACKENDS[config] = FoundryAgentServiceBackend(config)
        return backend
    raise ValueError(f"Unknown AGENT_BACKEND {backend_name!r}; expected 'direct' or 'foundry'.")


def backend_health() -> dict:
    """Backend metadata for /api/health. ``foundry_configured`` only means
    FOUNDRY_PROJECT_ENDPOINT is set -- it is not a live connectivity check."""
    foundry_cfg = FoundryConfig.from_env()
    return {
        "active_backend": (os.environ.get("AGENT_BACKEND", "direct").strip().lower() or "direct"),
        "direct_azure_openai_available": True,
        "foundry_configured": foundry_cfg.configured,
        "foundry_implemented": True,
        "foundry_agent_prefix": foundry_cfg.agent_prefix,
    }
