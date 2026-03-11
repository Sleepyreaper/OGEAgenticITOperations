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


def _get_client(deployment: str) -> tuple[AzureOpenAI, str]:
    """Get an AzureOpenAI client configured for the given deployment."""
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        credential = ManagedIdentityCredential(client_id=client_id)
    else:
        credential = DefaultAzureCredential()

    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )

    client = AzureOpenAI(
        azure_endpoint=settings.openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2025-01-01-preview",
    )
    return client, deployment


def call_agent(agent_config: AgentConfig, user_message: str,
               context_data: str = "") -> dict:
    """Call a single specialist agent and return its response."""
    client, deployment = _get_client(agent_config.deployment)

    messages = [
        {"role": "system", "content": agent_config.system_prompt},
    ]

    if context_data:
        messages.append({
            "role": "user",
            "content": f"Here is the current Azure environment data:\n\n{context_data}"
        })

    messages.append({"role": "user", "content": user_message})

    kwargs = {"model": deployment, "messages": messages}

    # Reasoning models (o4-mini, o3-mini) don't support temperature
    if agent_config.deployment not in ("o4MiniAgent",):
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
    """Run the full Ops Council: specialist agents → orchestrator synthesis.

    Returns a dict with all individual agent responses and the final synthesis.
    """
    if agents_to_consult is None:
        agents_to_consult = ["cost_sentinel", "standards_architect",
                             "diagnostics_sre", "scout"]

    # Phase 1: Gather specialist perspectives
    specialist_outputs = {}
    for agent_key in agents_to_consult:
        agent_cfg = settings.agents.get(agent_key)
        if not agent_cfg:
            continue
        result = call_agent(agent_cfg, user_message, context_data)
        specialist_outputs[agent_key] = result

    # Phase 2: Orchestrator synthesizes
    synthesis_input = f"""User question: {user_message}

Azure environment data:
{context_data}

Specialist agent analyses:
"""
    for key, output in specialist_outputs.items():
        synthesis_input += f"\n--- {output['agent']} ({output['role']}) ---\n"
        synthesis_input += output["response"]
        synthesis_input += "\n"

    orchestrator_cfg = settings.agents["orchestrator"]
    orchestrator_result = call_agent(orchestrator_cfg, synthesis_input)

    return {
        "question": user_message,
        "specialists": specialist_outputs,
        "synthesis": orchestrator_result,
    }
