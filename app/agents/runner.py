"""Agent runner — calls individual specialist agents and the orchestrator.

Each specialist receives:
1. The user's question
2. Relevant Azure telemetry data (pre-fetched by the orchestrator)
3. Their system prompt (personality + rules)

The orchestrator receives all specialist outputs and synthesizes.
All agent reasoning is returned transparently to the UI.

Enforceable per-agent controls (see docs/MODEL_CONFIGURATION.md):
  * ``max_completion_tokens`` — capped via the Azure OpenAI
    ``max_completion_tokens`` request argument, when > 0.
  * ``max_context_chars`` — applied only to ``context_data`` (never the
    system prompt or the user's question) before it's sent.
  * ``response_instruction`` — a profile-configured tone/length/
    personality instruction appended as its own message on every call,
    replacing what used to be a hardcoded, name-keyed style-hint table
    here.
  * ``input_cost_per_million`` / ``output_cost_per_million`` — used to
    compute ``usage.estimated_cost_usd``, a caller-maintained telemetry
    estimate, not billing truth.

Every call is wrapped in an OpenTelemetry span (app/telemetry.py) that is
a no-op unless ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is configured.
No prompt, response, or system-instruction text is ever recorded there.
"""

import json
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential, get_bearer_token_provider
import os
from app.config import settings, AgentConfig
from app import telemetry

# Appended verbatim (with the original length noted) when context_data is
# cut down to max_context_chars. Deliberately does NOT attempt to
# re-close/repair JSON structure — the point is to make truncation
# obvious to both the model and anyone reading logs, not to fabricate a
# valid-looking (but fictitious) document.
_TRUNCATION_MARKER = (
    "\n\n[...CONTEXT TRUNCATED at {limit} characters (original length: "
    "{original} characters). Data beyond this point was omitted and may "
    "leave this truncated to an incomplete/invalid JSON document...]"
)


def truncate_context(context_data: str, max_context_chars: int) -> str:
    """Truncate ``context_data`` to at most ``max_context_chars`` characters.

    ``max_context_chars <= 0`` means "no truncation" (the default) — most
    profiles set this generously since real Azure environment scans can
    be large JSON payloads. Character counts are only an *approximation*
    of token counts (roughly 3-4 characters per token for typical
    English/JSON text) — this is a coarse safety cap, not exact token
    accounting. Only ``context_data`` is ever truncated; the system
    prompt and the user's question are never touched.
    """
    if max_context_chars <= 0 or len(context_data) <= max_context_chars:
        return context_data
    marker = _TRUNCATION_MARKER.format(limit=max_context_chars, original=len(context_data))
    return context_data[:max_context_chars] + marker


def estimate_cost_usd(agent_config: AgentConfig, prompt_tokens: int, completion_tokens: int) -> float:
    """Caller-maintained pricing estimate for telemetry — NOT billing truth.

    See docs/MODEL_CONFIGURATION.md and cross-check actual spend with
    Azure Cost Management; ``input_cost_per_million``/
    ``output_cost_per_million`` default to 0.0 (no estimate) unless a
    profile/override sets them.
    """
    input_cost = (prompt_tokens / 1_000_000) * agent_config.input_cost_per_million
    output_cost = (completion_tokens / 1_000_000) * agent_config.output_cost_per_million
    return round(input_cost + output_cost, 6)


def _get_client(deployment: str, endpoint: str = "", api_version: str = "") -> tuple[AzureOpenAI, str]:
    """Get an AzureOpenAI client configured for the given deployment.

    If endpoint is provided, uses that instead of the default.
    This enables per-agent endpoint routing when models live in
    different regions/accounts.
    """
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        credential = ManagedIdentityCredential(client_id=client_id)
    else:
        credential = DefaultAzureCredential()

    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )

    azure_endpoint = endpoint or settings.openai_endpoint

    client = AzureOpenAI(
        azure_endpoint=azure_endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version or settings.openai_api_version,
    )
    return client, deployment


def call_agent(agent_config: AgentConfig, user_message: str,
               context_data: str = "") -> dict:
    """Call a single specialist agent and return its response."""
    client, deployment = _get_client(
        agent_config.deployment, agent_config.endpoint, agent_config.api_version
    )

    messages = [
        {"role": "system", "content": agent_config.system_prompt},
    ]

    if context_data:
        context_data = truncate_context(context_data, agent_config.max_context_chars)
        messages.append({
            "role": "user",
            "content": f"Here is the current Azure environment data:\n\n{context_data}"
        })

    messages.append({"role": "user", "content": user_message})

    # Profile-configured tone/length/personality instruction, appended as
    # its own message rather than concatenated into the user's question.
    # Replaces the old hardcoded, display-name-keyed style_hints table —
    # profiles now own this entirely (see profiles/<id>/profile.json).
    if agent_config.response_instruction:
        messages.append({
            "role": "user",
            "content": f"RESPONSE INSTRUCTION: {agent_config.response_instruction}",
        })

    kwargs = {"model": deployment, "messages": messages}

    # Some deployments (e.g. o-series/GPT-5-style reasoning models) only
    # accept the default temperature and error on any other value — each
    # agent declares whether its deployment supports a custom temperature.
    if agent_config.supports_temperature:
        kwargs["temperature"] = agent_config.temperature

    # 0 (the default) means "use the provider's default completion
    # length" — the argument is omitted entirely rather than sent as 0
    # (which most providers would reject or treat as "no output").
    if agent_config.max_completion_tokens > 0:
        kwargs["max_completion_tokens"] = agent_config.max_completion_tokens

    with telemetry.agent_call_span(
        agent_key=agent_config.key,
        agent_name=agent_config.name,
        profile_id=settings.profile_id,
        model=deployment,
    ) as span:
        response = client.chat.completions.create(**kwargs)

        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens
        total_tokens = getattr(response.usage, "total_tokens", None) or (prompt_tokens + completion_tokens)
        cost_usd = estimate_cost_usd(agent_config, prompt_tokens, completion_tokens)
        finish_reason = response.choices[0].finish_reason if response.choices else None

        span.set_response_model(getattr(response, "model", None))
        span.set_usage(prompt_tokens, completion_tokens)
        span.set_finish_reasons([finish_reason] if finish_reason else None)
        span.set_cost(cost_usd)

    telemetry.record_usage(
        agent_key=agent_config.key,
        model=deployment,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )

    return {
        "agent": agent_config.name,
        "role": agent_config.role,
        "model": agent_config.deployment,
        "response": response.choices[0].message.content,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": cost_usd,
        },
    }


def run_council(user_message: str, context_data: str = "",
                agents_to_consult: list[str] = None) -> dict:
    """Run the full Ops Council with debate: specialists → rebuttals → synthesis.

    Round 1: Each specialist gives their independent analysis.
    Round 2: Each specialist sees the others' takes and can argue, agree, or refine.
    Round 3: Pipeline synthesizes with full debate context.
    """
    if agents_to_consult is None:
        agents_to_consult = ["cost_sentinel", "standards_architect",
                             "diagnostics_sre", "scout"]

    # ── Round 1: Independent analysis ──
    specialist_outputs = {}
    for agent_key in agents_to_consult:
        agent_cfg = settings.agents.get(agent_key)
        if not agent_cfg:
            continue
        result = call_agent(agent_cfg, user_message, context_data)
        specialist_outputs[agent_key] = result

    # ── Round 2: Rebuttals (if 2+ specialists) ──
    rebuttal_outputs = {}
    if len(specialist_outputs) >= 2:
        # Build the round 1 summary for each agent to react to
        round1_summary = ""
        for key, output in specialist_outputs.items():
            round1_summary += f"\n--- {output['agent']} said ---\n"
            round1_summary += output["response"]
            round1_summary += "\n"

        for agent_key in agents_to_consult:
            agent_cfg = settings.agents.get(agent_key)
            if not agent_cfg:
                continue

            rebuttal_prompt = f"""You've seen what the rest of the crew said in Round 1:
{round1_summary}

React in 2-3 sentences MAX. Call out disagreements by name, acknowledge good points, state your position. This is a quick crew huddle, not a report."""

            rebuttal = call_agent(agent_cfg, rebuttal_prompt, context_data)
            rebuttal_outputs[agent_key] = rebuttal

    # ── Round 3: Pipeline synthesizes the full debate ──
    synthesis_input = f"""User question: {user_message}

Azure environment data:
{context_data}

=== ROUND 1: Initial Analysis ===
"""
    for key, output in specialist_outputs.items():
        synthesis_input += f"\n--- {output['agent']} ---\n"
        synthesis_input += output["response"]
        synthesis_input += "\n"

    if rebuttal_outputs:
        synthesis_input += "\n=== ROUND 2: Crew Debate ===\n"
        for key, output in rebuttal_outputs.items():
            synthesis_input += f"\n--- {output['agent']} (rebuttal) ---\n"
            synthesis_input += output["response"]
            synthesis_input += "\n"

    orchestrator_cfg = settings.agents["orchestrator"]
    orchestrator_result = call_agent(orchestrator_cfg, synthesis_input)

    return {
        "question": user_message,
        "specialists": specialist_outputs,
        "rebuttals": rebuttal_outputs if rebuttal_outputs else None,
        "synthesis": orchestrator_result,
    }


def run_council_streaming(user_message: str, context_data: str = "",
                          agents_to_consult: list[str] = None):
    """Generator that yields each agent result as it completes.

    Yields dicts with: {phase, agent_key, result}
    Phases: "round1", "round2", "synthesis"
    """
    if agents_to_consult is None:
        agents_to_consult = ["cost_sentinel", "standards_architect",
                             "diagnostics_sre", "scout"]

    # ── Round 1: Independent analysis ──
    specialist_outputs = {}
    for agent_key in agents_to_consult:
        agent_cfg = settings.agents.get(agent_key)
        if not agent_cfg:
            continue
        result = call_agent(agent_cfg, user_message, context_data)
        specialist_outputs[agent_key] = result
        yield {"phase": "round1", "agent_key": agent_key, "result": result}

    # ── Round 2: Rebuttals (if 2+ specialists) ──
    rebuttal_outputs = {}
    if len(specialist_outputs) >= 2:
        round1_summary = ""
        for key, output in specialist_outputs.items():
            round1_summary += f"\n--- {output['agent']} said ---\n"
            round1_summary += output["response"]
            round1_summary += "\n"

        for agent_key in agents_to_consult:
            agent_cfg = settings.agents.get(agent_key)
            if not agent_cfg:
                continue

            rebuttal_prompt = f"""You've seen what the rest of the crew said in Round 1:
{round1_summary}

React in 2-3 sentences MAX. Call out disagreements by name, acknowledge good points, state your position. This is a quick crew huddle, not a report."""

            rebuttal = call_agent(agent_cfg, rebuttal_prompt, context_data)
            rebuttal_outputs[agent_key] = rebuttal
            yield {"phase": "round2", "agent_key": agent_key, "result": rebuttal}

    # ── Round 3: Pipeline synthesizes ──
    synthesis_input = f"""User question: {user_message}

Azure environment data:
{context_data}

=== ROUND 1: Initial Analysis ===
"""
    for key, output in specialist_outputs.items():
        synthesis_input += f"\n--- {output['agent']} ---\n"
        synthesis_input += output["response"]
        synthesis_input += "\n"

    if rebuttal_outputs:
        synthesis_input += "\n=== ROUND 2: Crew Debate ===\n"
        for key, output in rebuttal_outputs.items():
            synthesis_input += f"\n--- {output['agent']} (rebuttal) ---\n"
            synthesis_input += output["response"]
            synthesis_input += "\n"

    orchestrator_cfg = settings.agents["orchestrator"]
    orchestrator_result = call_agent(orchestrator_cfg, synthesis_input)
    yield {"phase": "synthesis", "agent_key": "orchestrator", "result": orchestrator_result}
