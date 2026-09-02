#!/usr/bin/env python3
"""Test the model backend protocol (app/agents/backend.py) --
DirectAzureOpenAIBackend's structured-output request + its explicit,
safe fallback to a plain completion when the deployment rejects
response_format, and that FoundryAgentServiceBackend is honestly NOT
implemented. No real Azure/OpenAI calls: app.agents.backend._get_client
is monkeypatched with a fake client (same pattern as tests/test_runner.py).

Run: python3 tests/test_agent_backend.py
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

import httpx  # noqa: E402
import openai  # noqa: E402

from app import telemetry  # noqa: E402
from app.agents import backend as backend_mod  # noqa: E402
from app.config import AgentConfig  # noqa: E402

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
        key="cost_sentinel", name="Cost Analyst", role="Finds waste.", deployment="gpt-5.6-terra",
        system_prompt="You are a cost analyst.", temperature=1.0, supports_temperature=False,
        endpoint="", api_version="2025-01-01-preview", max_completion_tokens=0, max_context_chars=0,
        response_instruction="", input_cost_per_million=0.0, output_cost_per_million=0.0,
        prompt_version="v1", supports_structured_output=True,
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
    def __init__(self, prompt_tokens=10, completion_tokens=5, total_tokens=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens if total_tokens is not None else prompt_tokens + completion_tokens


class FakeResponse:
    def __init__(self, content="{}", model="gpt-5.6-terra-2026-01-01", finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]
        self.usage = FakeUsage()
        self.model = model


def _bad_request_error():
    request = httpx.Request("POST", "https://example.openai.azure.com/")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError("response_format is not supported for this model", response=response, body=None)


class FakeCompletions:
    """Raises `first_error` on the FIRST call (simulating a deployment
    rejecting response_format), then returns `response` on every
    subsequent call -- lets a single fake client cover both the
    structured-attempt-then-fallback path in one test."""

    def __init__(self, response, first_error=None):
        self._response = response
        self._first_error = first_error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._first_error is not None and len(self.calls) == 1:
            raise self._first_error
        return self._response


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, response, first_error=None):
        self.completions = FakeCompletions(response, first_error=first_error)
        self.chat = FakeChat(self.completions)


print("\n\U0001f9ea Test 1: structured output requested and accepted -- one call, response_format present")
client = FakeClient(FakeResponse(content='{"conclusion": "ok"}'))
with patch("app.agents.backend._get_client", side_effect=lambda d, e="", v="": (client, d)):
    completion = backend_mod.DirectAzureOpenAIBackend().complete(
        make_agent_config(), [{"role": "user", "content": "hi"}],
        json_schema={"type": "object"}, schema_name="agent_analysis_result",
    )
test("exactly one underlying API call was made", len(client.completions.calls) == 1)
test("response_format was sent on that call", "response_format" in client.completions.calls[0])
test("response_format uses strict json_schema mode", client.completions.calls[0]["response_format"]["json_schema"]["strict"] is True)
test("structured_output_used is True", completion.structured_output_used is True)
test("raw_text is the model's content", completion.raw_text == '{"conclusion": "ok"}')
test("usage dict has prompt/completion/total/cost", set(completion.usage) == {"prompt_tokens", "completion_tokens", "total_tokens", "estimated_cost_usd"})


print("\n\U0001f9ea Test 2: structured output rejected by the deployment -- explicit, safe fallback")
client2 = FakeClient(FakeResponse(content="plain text answer"), first_error=_bad_request_error())
with patch("app.agents.backend._get_client", side_effect=lambda d, e="", v="": (client2, d)):
    completion2 = backend_mod.DirectAzureOpenAIBackend().complete(
        make_agent_config(), [{"role": "user", "content": "hi"}],
        json_schema={"type": "object"}, schema_name="agent_analysis_result",
    )
test("exactly two underlying API calls were made (structured attempt + fallback)", len(client2.completions.calls) == 2)
test("the first call attempted response_format", "response_format" in client2.completions.calls[0])
test("the second (fallback) call did NOT send response_format", "response_format" not in client2.completions.calls[1])
test("structured_output_used is False after falling back", completion2.structured_output_used is False)
test("raw_text still comes back so the caller's own parser (app/agents/schema.py) can try", completion2.raw_text == "plain text answer")


print("\n\U0001f9ea Test 3: supports_structured_output=False skips the structured attempt entirely")
client3 = FakeClient(FakeResponse(content="plain text"))
with patch("app.agents.backend._get_client", side_effect=lambda d, e="", v="": (client3, d)):
    completion3 = backend_mod.DirectAzureOpenAIBackend().complete(
        make_agent_config(supports_structured_output=False), [{"role": "user", "content": "hi"}],
        json_schema={"type": "object"}, schema_name="agent_analysis_result",
    )
test("exactly one call made (no wasted structured attempt)", len(client3.completions.calls) == 1)
test("that one call never sent response_format", "response_format" not in client3.completions.calls[0])
test("structured_output_used is False", completion3.structured_output_used is False)


print("\n\U0001f9ea Test 4: a non-BadRequestError exception propagates unchanged (never silently swallowed)")
class FakeCompletionsRaisingOther:
    def create(self, **kwargs):
        raise RuntimeError("network is down")


client4 = FakeClient(FakeResponse())
client4.completions = FakeCompletionsRaisingOther()
client4.chat = FakeChat(client4.completions)
with patch("app.agents.backend._get_client", side_effect=lambda d, e="", v="": (client4, d)):
    try:
        backend_mod.DirectAzureOpenAIBackend().complete(
            make_agent_config(), [{"role": "user", "content": "hi"}], json_schema={"type": "object"},
        )
        test("a non-BadRequestError exception propagates unchanged", False)
    except RuntimeError as exc:
        test("a non-BadRequestError exception propagates unchanged", "network is down" in str(exc))


print("\n\U0001f9ea Test 5: FoundryAgentServiceBackend is honestly NOT implemented")
foundry_backend = backend_mod.FoundryAgentServiceBackend()
test("name identifies it as the Foundry backend", foundry_backend.name == "foundry_agent_service")
try:
    foundry_backend.complete(make_agent_config(), [{"role": "user", "content": "hi"}])
    test("calling FoundryAgentServiceBackend.complete() raises NotImplementedError", False)
except NotImplementedError as exc:
    test("calling FoundryAgentServiceBackend.complete() raises NotImplementedError", "not implemented" in str(exc).lower())


print("\n\U0001f9ea Test 6: get_backend()/backend_health() are honest about what's active")
os.environ.pop("AGENT_BACKEND", None)
default_backend = backend_mod.get_backend()
test("default backend is DirectAzureOpenAIBackend", isinstance(default_backend, backend_mod.DirectAzureOpenAIBackend))

health = backend_mod.backend_health()
test("active_backend defaults to 'direct'", health["active_backend"] == "direct")
test("foundry_implemented is always False (never claims a fake integration)", health["foundry_implemented"] is False)

os.environ["AGENT_BACKEND"] = "foundry"
try:
    foundry_selected = backend_mod.get_backend()
    test("AGENT_BACKEND=foundry returns a FoundryAgentServiceBackend", isinstance(foundry_selected, backend_mod.FoundryAgentServiceBackend))
finally:
    os.environ.pop("AGENT_BACKEND", None)

try:
    backend_mod.get_backend("not-a-real-backend")
    test("an unrecognized backend name raises ValueError", False)
except ValueError:
    test("an unrecognized backend name raises ValueError", True)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
