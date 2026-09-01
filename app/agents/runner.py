"""Agent runner — calls individual specialist agents and the orchestrator.

Each specialist receives:
1. The user's question
2. Relevant Azure telemetry data (pre-fetched by the orchestrator)
3. Their system prompt (personality + rules)

The orchestrator receives all specialist outputs and synthesizes.
All agent reasoning is returned transparently to the UI.
"""

import json
from openai import AzureOpenAI
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential, get_bearer_token_provider
import os
from app.config import settings, AgentConfig


def _get_client(deployment: str, endpoint: str = "") -> tuple[AzureOpenAI, str]:
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
        api_version="2025-01-01-preview",
    )
    return client, deployment


def call_agent(agent_config: AgentConfig, user_message: str,
               context_data: str = "") -> dict:
    """Call a single specialist agent and return its response."""
    client, deployment = _get_client(agent_config.deployment, agent_config.endpoint)

    messages = [
        {"role": "system", "content": agent_config.system_prompt},
    ]

    if context_data:
        messages.append({
            "role": "user",
            "content": f"Here is the current Azure environment data:\n\n{context_data}"
        })

    # Style instruction per agent personality
    style_hints = {
        "Meter Reader": "Respond like a sharp cost analyst reading numbers off a control room display. Lead with the dollar figure. 3-5 sentences, all business.",
        "The Lineman": "Respond like a grizzled field veteran. Blunt, confident, maybe a little salty. Lead with whether this is safe to touch or not. 3-5 sentences.",
        "Blackout": "Respond like a calm incident commander. Timeline first, then root cause, then fix. Structured and clinical. 3-5 sentences.",
        "Arc Flash": "Respond like an alert system — short, punchy, severity-tagged. Use 🔴🟡🔵 severity indicators. 2-4 sentences max.",
        "Grid Dispatch": "Respond as a crisp executive readout. No debate recap — just the bottom line recommendation with key tradeoffs in 3-5 sentences.",
        "The Regulator": "Respond like a regulatory inspector filing a citation. Lead with the violation, classify it (policy bug / misconfiguration / exemption / workaround abuse), then state the required fix. 3-5 sentences, by the book.",
    }
    style = style_hints.get(agent_config.name, "Keep your response to 3-5 concise sentences.")
    messages.append({"role": "user", "content": user_message + f"\n\nSTYLE: {style}"})

    kwargs = {"model": deployment, "messages": messages}

    # These models only support default temperature (1.0)
    fixed_temp_models = ("foundry-gpt", "foundry-reasoning", "foundry-nano")
    if agent_config.deployment not in fixed_temp_models:
        kwargs["temperature"] = agent_config.temperature

    response = client.chat.completions.create(**kwargs)

    return {
        "agent": agent_config.name,
        "role": agent_config.role,
        "model": agent_config.deployment,
        "response": response.choices[0].message.content,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
        },
    }


def run_council(user_message: str, context_data: str = "",
                agents_to_consult: list[str] = None) -> dict:
    """Run the full Cloud Weather Ops with debate: specialists → rebuttals → synthesis.

    Round 1: Each specialist gives their independent analysis.
    Round 2: Each specialist sees the others' takes and can argue, agree, or refine.
    Round 3: Grid Dispatch synthesizes with full debate context.
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

            rebuttal_prompt = f"""You've seen what the rest of the grid team said in Round 1:
{round1_summary}

React in 2-3 sentences MAX. Call out disagreements by name, acknowledge good points, state your position. This is a quick crew huddle, not a report."""

            rebuttal = call_agent(agent_cfg, rebuttal_prompt, context_data)
            rebuttal_outputs[agent_key] = rebuttal

    # ── Round 3: Grid Dispatch synthesizes the full debate ──
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

            rebuttal_prompt = f"""You've seen what the rest of the grid team said in Round 1:
{round1_summary}

React in 2-3 sentences MAX. Call out disagreements by name, acknowledge good points, state your position. This is a quick crew huddle, not a report."""

            rebuttal = call_agent(agent_cfg, rebuttal_prompt, context_data)
            rebuttal_outputs[agent_key] = rebuttal
            yield {"phase": "round2", "agent_key": agent_key, "result": rebuttal}

    # ── Round 3: Grid Dispatch synthesizes ──
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
