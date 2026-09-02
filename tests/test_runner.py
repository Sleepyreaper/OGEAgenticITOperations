#!/usr/bin/env python3
"""Test the agent runner's pure helpers and per-call request building
(app/agents/runner.py) — context truncation, cost estimation, and the
kwargs/messages passed to the (mocked) OpenAI client. No real Azure/OpenAI
calls are made: app.agents.runner._get_client is monkeypatched with a fake
client that records what it was called with.

Run: python3 tests/test_runner.py
"""
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Telemetry must stay disabled (the normal state without a connection
# string) for these tests — assert that up front, and never set the env
# var here, so call_agent's telemetry span is a guaranteed no-op and
# doesn't need mocking of its own.
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

from app.config import AgentConfig  # noqa: E402
from app.agents.runner import call_agent, truncate_context, estimate_cost_usd  # noqa: E402
from app import telemetry  # noqa: E402

telemetry.reset_for_tests()

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


def make_agent_config(**overrides) -> AgentConfig:
    defaults = dict(
        key="cost_sentinel",
        name="Cost & Capacity Analyst",
        role="Finds waste.",
        deployment="gpt-5.6-terra",
        system_prompt="You are a cost analyst.",
        temperature=1.0,
        supports_temperature=False,
        endpoint="",
        api_version="2025-01-01-preview",
        max_completion_tokens=0,
        max_context_chars=0,
        response_instruction="",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = FakeMessage(content)
        self.finish_reason = finish_reason


class FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens if total_tokens is not None else prompt_tokens + completion_tokens


class FakeResponse:
    def __init__(self, content="A response.", prompt_tokens=100, completion_tokens=50,
                 model="gpt-5.6-terra-2026-01-01", finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]
        self.usage = FakeUsage(prompt_tokens, completion_tokens)
        self.model = model


class FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, response):
        self.completions = FakeCompletions(response)
        self.chat = FakeChat(self.completions)


@contextmanager
def fake_client(response=None):
    """Patch app.agents.runner._get_client to return a FakeClient, and
    yield the FakeCompletions object so the test can inspect the last
    call's kwargs."""
    response = response or FakeResponse()
    client = FakeClient(response)

    def _fake_get_client(deployment, endpoint="", api_version=""):
        return client, deployment

    with patch("app.agents.runner._get_client", side_effect=_fake_get_client):
        yield client.completions


# ─── truncate_context ───────────────────────────────────────────────────
print("\n\U0001f9ea Test 1: truncate_context — convention and marker behavior")
short_text = "x" * 100
test("0 means no truncation, even for long text", truncate_context("y" * 5000, 0) == "y" * 5000)
test("text shorter than the cap is returned unchanged", truncate_context(short_text, 200) == short_text)
test("text exactly at the cap is returned unchanged", truncate_context(short_text, 100) == short_text)

long_text = "z" * 500
truncated = truncate_context(long_text, 200)
test("truncated text starts with the first N characters verbatim", truncated.startswith("z" * 200))
test("truncated text is longer than the cap (marker appended)", len(truncated) > 200)
test("truncation marker mentions TRUNCATED", "TRUNCATED" in truncated)
test("truncation marker mentions the cap (200)", "200 characters" in truncated)
test("truncation marker mentions the original length (500)", "500 characters" in truncated)
test("truncation marker warns about invalid/incomplete JSON, not fabricated JSON", "invalid" in truncated.lower())


# ─── estimate_cost_usd ──────────────────────────────────────────────────
print("\n\U0001f9ea Test 2: estimate_cost_usd — formula and zero-pricing default")
zero_pricing_cfg = make_agent_config(input_cost_per_million=0.0, output_cost_per_million=0.0)
test("zero pricing -> zero cost regardless of tokens", estimate_cost_usd(zero_pricing_cfg, 100_000, 50_000) == 0.0)

priced_cfg = make_agent_config(input_cost_per_million=2.0, output_cost_per_million=8.0)
expected = round((1_000_000 / 1_000_000) * 2.0 + (500_000 / 1_000_000) * 8.0, 6)
test("cost formula: (prompt/1e6)*input + (completion/1e6)*output",
     estimate_cost_usd(priced_cfg, 1_000_000, 500_000) == expected)
test("cost formula with small token counts", estimate_cost_usd(priced_cfg, 100, 50) == round((100/1e6)*2.0 + (50/1e6)*8.0, 6))


# ─── call_agent — request building via a mocked OpenAI client ──────────
print("\n\U0001f9ea Test 3: call_agent sends max_completion_tokens only when > 0")
cfg_with_cap = make_agent_config(max_completion_tokens=900)
with fake_client() as completions:
    call_agent(cfg_with_cap, "What's driving our spend?")
    test("max_completion_tokens included when positive", completions.last_kwargs.get("max_completion_tokens") == 900)

cfg_no_cap = make_agent_config(max_completion_tokens=0)
with fake_client() as completions:
    call_agent(cfg_no_cap, "What's driving our spend?")
    test("max_completion_tokens omitted when 0 (provider default)", "max_completion_tokens" not in completions.last_kwargs)

print("\n\U0001f9ea Test 4: call_agent truncates only context_data, never the system prompt/question")
long_context = "C" * 1000
long_question = "Q" * 1000  # deliberately long — must NOT be truncated
cfg_small_context_cap = make_agent_config(max_context_chars=100, system_prompt="S" * 1000)
with fake_client() as completions:
    call_agent(cfg_small_context_cap, long_question, context_data=long_context)
    messages = completions.last_kwargs["messages"]
    all_text = " ".join(m["content"] for m in messages)
    test("system prompt appears in full, untruncated", "S" * 1000 in all_text)
    test("user question appears in full, untruncated", "Q" * 1000 in all_text)
    context_message = next(m["content"] for m in messages if "environment data" in m["content"])
    test("context_data was truncated to the cap", "C" * 100 in context_message and "C" * 101 not in context_message)
    test("truncation marker present in the context message", "TRUNCATED" in context_message)

print("\n\U0001f9ea Test 5: call_agent appends response_instruction as its own message")
cfg_with_instruction = make_agent_config(response_instruction="Lead with the dollar figure.")
with fake_client() as completions:
    call_agent(cfg_with_instruction, "Why is this so expensive?")
    messages = completions.last_kwargs["messages"]
    instruction_messages = [m for m in messages if m["content"] == "RESPONSE INSTRUCTION: Lead with the dollar figure."]
    test("response_instruction sent as its own distinct message", len(instruction_messages) == 1)

cfg_no_instruction = make_agent_config(response_instruction="")
with fake_client() as completions:
    call_agent(cfg_no_instruction, "Why is this so expensive?")
    messages = completions.last_kwargs["messages"]
    test("no RESPONSE INSTRUCTION message when response_instruction is empty",
         not any("RESPONSE INSTRUCTION" in m["content"] for m in messages))

print("\n\U0001f9ea Test 6: call_agent only sends temperature when supports_temperature is True")
cfg_supports_temp = make_agent_config(supports_temperature=True, temperature=0.3)
with fake_client() as completions:
    call_agent(cfg_supports_temp, "question")
    test("temperature included when supported", completions.last_kwargs.get("temperature") == 0.3)

cfg_no_temp = make_agent_config(supports_temperature=False, temperature=0.3)
with fake_client() as completions:
    call_agent(cfg_no_temp, "question")
    test("temperature omitted when not supported", "temperature" not in completions.last_kwargs)

print("\n\U0001f9ea Test 7: call_agent's returned usage — tokens + estimated cost, without breaking existing keys")
cfg_priced = make_agent_config(input_cost_per_million=1.0, output_cost_per_million=2.0)
with fake_client(FakeResponse(prompt_tokens=200, completion_tokens=100)) as completions:
    result = call_agent(cfg_priced, "question")
    usage = result["usage"]
    test("prompt_tokens present (existing key)", usage["prompt_tokens"] == 200)
    test("completion_tokens present (existing key)", usage["completion_tokens"] == 100)
    test("total_tokens present (new key)", usage["total_tokens"] == 300)
    test("estimated_cost_usd present (new key)", usage["estimated_cost_usd"] == round((200/1e6)*1.0 + (100/1e6)*2.0, 6))
    test("agent/role/model/response keys unchanged", {"agent", "role", "model", "response"} <= set(result.keys()))
    test("agent name reflects the config", result["agent"] == cfg_priced.name)
    test("model reflects the configured deployment (existing behavior)", result["model"] == cfg_priced.deployment)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
