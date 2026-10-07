#!/usr/bin/env python3
"""Test FoundryAgentServiceBackend (app/agents/backend.py) -- agent
version publishing/fingerprinting, the Responses API function-tool loop,
server-bound tool scope, usage accounting, and the analysis-layer wiring
(tool_context opt-in + bounded parallel specialist fan-out). No real
Azure calls: a fake AIProjectClient/OpenAI client is injected.

Run: python3 tests/test_agent_foundry_backend.py
"""
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

from azure.core.exceptions import ResourceNotFoundError  # noqa: E402

from app import telemetry  # noqa: E402
from app.agents import analysis as analysis_mod  # noqa: E402
from app.agents import backend as backend_mod  # noqa: E402
from app.agents import tools as tools_mod  # noqa: E402
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
        key="cost_sentinel", name="Barrel Counter", role="Finds waste.", deployment="gpt-5.6-sol",
        system_prompt="You are a cost analyst.", temperature=1.0, supports_temperature=False,
        endpoint="", api_version="2025-01-01-preview", max_completion_tokens=0, max_context_chars=0,
        response_instruction="", input_cost_per_million=1.0, output_cost_per_million=2.0,
        prompt_version="v1", supports_structured_output=True,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


class Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def fn_call(name, arguments, call_id="call_1"):
    return Obj(type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id)


def response(rid, output, text="", input_tokens=100, output_tokens=20, status="completed"):
    return Obj(
        id=rid, output=output, output_text=text, status=status, model="gpt-5.6-sol",
        usage=Obj(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeResponses:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._scripted) > 1:
            return self._scripted.pop(0)
        return self._scripted[0]


class FakeAgents:
    def __init__(self, existing_hash=None):
        self.existing_hash = existing_hash
        self.get_calls = 0
        self.created = []

    def get(self, agent_name):
        self.get_calls += 1
        if self.existing_hash is None:
            raise ResourceNotFoundError("not found")
        return Obj(name=agent_name, versions=Obj(latest=Obj(metadata={"definition_hash": self.existing_hash})))

    def create_version(self, agent_name, definition, description=None, metadata=None):
        self.created.append({"agent_name": agent_name, "definition": definition, "metadata": metadata})
        self.existing_hash = metadata["definition_hash"]
        return Obj(name=agent_name, version=str(len(self.created)), metadata=metadata)


class FakeProject:
    def __init__(self, scripted, existing_hash=None):
        self.agents = FakeAgents(existing_hash)
        self.responses = FakeResponses(scripted)

    def get_openai_client(self):
        return Obj(responses=self.responses)


def make_backend(project, **config_overrides):
    backend_mod.FoundryAgentServiceBackend._published.clear()
    cfg = dict(project_endpoint="https://example.services.ai.azure.com/api/projects/p")
    cfg.update(config_overrides)
    return backend_mod.FoundryAgentServiceBackend(backend_mod.FoundryConfig(**cfg), project_client=project)


SCHEMA = {"type": "object", "properties": {"conclusion": {"type": "string"}}, "required": ["conclusion"], "additionalProperties": False}
SCOPE = backend_mod.ToolContext(subscription_ids=("sub-a",))


print("\n\U0001f9ea Test 1: deterministic, Foundry-valid agent names")
name = backend_mod.foundry_agent_name("oge-ops", "cost_sentinel", "agent_analysis_result")
test("name is prefix-key-schema slug", name == "oge-ops-cost-sentinel-agent-analysis-result")
long_name = backend_mod.foundry_agent_name("x" * 40, "compliance_inspector", "agent_analysis_result")
test("long names are capped at 63 chars", len(long_name) <= 63)
test("long names stay deterministic", long_name == backend_mod.foundry_agent_name("x" * 40, "compliance_inspector", "agent_analysis_result"))
test("names start/end alphanumeric", long_name[0].isalnum() and long_name[-1].isalnum())


print("\n\U0001f9ea Test 2: server-bound tool args are hidden from the model")
schema = backend_mod.model_facing_tool_schema(tools_mod.TOOLS["get_finding_evidence"].parameters_schema)
test("subscription_ids removed from properties", "subscription_ids" not in schema["properties"])
test("force_refresh removed from properties", "force_refresh" not in schema["properties"])
test("subscription_ids removed from required", "subscription_ids" not in schema["required"])
test("tool-specific required args kept", schema["required"] == ["finding_id"])
test("original registry schema untouched", "subscription_ids" in tools_mod.TOOLS["get_finding_evidence"].parameters_schema["required"])


print("\n\U0001f9ea Test 3: first call publishes one agent version with model/instructions/schema/tools")
project = FakeProject([response("r1", [], text='{"conclusion":"ok"}')])
fb = make_backend(project)
completion = fb.complete(
    make_agent_config(), [{"role": "system", "content": "You are a cost analyst."}, {"role": "user", "content": "hi"}],
    json_schema=SCHEMA, schema_name="agent_analysis_result", tool_context=SCOPE,
)
test("exactly one agent version created", len(project.agents.created) == 1)
created = project.agents.created[0]
test("agent named per specialist+schema", created["agent_name"] == "oge-ops-cost-sentinel-agent-analysis-result")
test("definition uses the agent's deployment", created["definition"].model == "gpt-5.6-sol")
test("definition instructions are the profile system prompt", created["definition"].instructions == "You are a cost analyst.")
test("structured-output schema baked into the definition", created["definition"].text["format"]["schema"] == SCHEMA)
test("every registry tool registered", sorted(t["name"] for t in created["definition"].tools) == sorted(tools_mod.TOOLS))
test("metadata carries definition_hash + agent_key", created["metadata"]["agent_key"] == "cost_sentinel" and created["metadata"]["definition_hash"])
call = project.responses.calls[0]
test("call references the agent", call["extra_body"] == {"agent_reference": {"name": created["agent_name"], "type": "agent_reference"}})
test("no per-call 'text' (Foundry rejects it when an agent is referenced)", "text" not in call)
test("no per-call 'model' (the agent version owns it)", "model" not in call)
test("duplicate system prompt dropped from input", all(item["content"] != "You are a cost analyst." for item in call["input"]))
test("input items are typed messages", all(item["type"] == "message" for item in call["input"]))
test("raw_text is output_text", completion.raw_text == '{"conclusion":"ok"}')
test("structured_output_used is True", completion.structured_output_used is True)
test("usage carries tokens and estimated cost", completion.usage["total_tokens"] == 120 and completion.usage["estimated_cost_usd"] == round(100 / 1e6 * 1.0 + 20 / 1e6 * 2.0, 6))


print("\n\U0001f9ea Test 4: unchanged definition is not republished; changed prompt is")
fb.complete(make_agent_config(), [{"role": "user", "content": "again"}], json_schema=SCHEMA, schema_name="agent_analysis_result")
test("cached fingerprint: no extra get/create", project.agents.get_calls == 1 and len(project.agents.created) == 1)
fb.complete(make_agent_config(system_prompt="New prompt."), [{"role": "user", "content": "x"}], json_schema=SCHEMA, schema_name="agent_analysis_result")
test("changed system prompt publishes a new version", len(project.agents.created) == 2)
existing_hash = project.agents.created[-1]["metadata"]["definition_hash"]
project2 = FakeProject([response("r1", [], text="{}")], existing_hash=existing_hash)
fb2 = make_backend(project2)
fb2.complete(make_agent_config(system_prompt="New prompt."), [{"role": "user", "content": "x"}], json_schema=SCHEMA, schema_name="agent_analysis_result")
test("matching remote definition_hash is reused (fresh process)", len(project2.agents.created) == 0 and project2.agents.get_calls == 1)


print("\n\U0001f9ea Test 5: function-call loop executes tools with server-bound scope")
project = FakeProject([
    response("r1", [fn_call("get_finding_evidence", {"finding_id": "cst-1", "subscription_ids": ["evil-sub"], "force_refresh": True})], input_tokens=50, output_tokens=10),
    response("r2", [], text='{"conclusion":"done"}', input_tokens=70, output_tokens=15),
])
fb = make_backend(project)
seen = []


def fake_execute(name, arguments, *, caller_roles=None, config=None):
    seen.append({"name": name, "arguments": arguments, "caller_roles": caller_roles})
    return tools_mod.ToolResult(tool_name=name, status="ok", data={"id": "cst-1"}, result_count=1, duration_ms=1.0)


with patch.object(tools_mod, "execute_tool", side_effect=fake_execute):
    completion = fb.complete(make_agent_config(), [{"role": "user", "content": "why?"}], json_schema=SCHEMA, schema_name="s", tool_context=SCOPE)
test("tool executed once", len(seen) == 1 and seen[0]["name"] == "get_finding_evidence")
test("model-supplied subscription_ids overridden by server scope", seen[0]["arguments"]["subscription_ids"] == ["sub-a"])
test("model-supplied force_refresh dropped", "force_refresh" not in seen[0]["arguments"])
test("caller role is operations_reader", seen[0]["caller_roles"] == {"operations_reader"})
second = project.responses.calls[1]
test("follow-up chains previous_response_id", second["previous_response_id"] == "r1")
test("follow-up sends function_call_output for the call id", second["input"][0]["type"] == "function_call_output" and second["input"][0]["call_id"] == "call_1")
test("tool output is the ToolResult JSON", json.loads(second["input"][0]["output"])["status"] == "ok")
test("usage summed across rounds", completion.usage["prompt_tokens"] == 120 and completion.usage["completion_tokens"] == 25)
test("tool call/round counts reported", completion.usage["tool_calls"] == 1 and completion.usage["tool_rounds"] == 1)
test("final text returned", completion.raw_text == '{"conclusion":"done"}')


print("\n\U0001f9ea Test 6: tool loop is bounded")
project = FakeProject([response("rN", [fn_call("get_capacity_watch", {})])])
fb = make_backend(project, max_tool_rounds=2)
with patch.object(tools_mod, "execute_tool", side_effect=fake_execute):
    completion = fb.complete(make_agent_config(), [{"role": "user", "content": "loop"}], tool_context=SCOPE)
test("stops after max_tool_rounds + 1 model calls", len(project.responses.calls) == 3)
test("finish_reason reports tool_round_limit", completion.finish_reason == "tool_round_limit")


print("\n\U0001f9ea Test 7: no tool scope -> explicit tool error, never an unscoped Azure read")
seen.clear()
project = FakeProject([response("r1", [fn_call("get_capacity_watch", {})]), response("r2", [], text="{}")])
fb = make_backend(project)
with patch.object(tools_mod, "execute_tool", side_effect=fake_execute):
    fb.complete(make_agent_config(), [{"role": "user", "content": "x"}])
test("execute_tool never called", seen == [])
test("model receives an explicit error output", json.loads(project.responses.calls[1]["input"][0]["output"])["status"] == "error")


print("\n\U0001f9ea Test 8: per-agent controls map onto the definition / call")
project = FakeProject([response("r1", [], text="plain")])
fb = make_backend(project, enable_tools=False)
completion = fb.complete(
    make_agent_config(supports_structured_output=False, supports_temperature=True, temperature=0.2, max_completion_tokens=900),
    [{"role": "system", "content": "extra rule"}, {"role": "user", "content": "x"}], json_schema=SCHEMA, schema_name="s",
)
definition = project.agents.created[0]["definition"]
test("no schema baked when the deployment lacks structured output", definition.get("text") is None)
test("structured_output_used False", completion.structured_output_used is False)
test("temperature set when supported", definition.temperature == 0.2)
test("tools omitted when FOUNDRY_ENABLE_TOOLS is false", not definition.get("tools"))
test("max_completion_tokens -> max_output_tokens", project.responses.calls[0]["max_output_tokens"] == 900)
test("non-prompt system message becomes developer message", project.responses.calls[0]["input"][0]["role"] == "developer")
project = FakeProject([response("r1", [], text="{}")])
make_backend(project, model_deployment="gpt-5.6-luna").complete(make_agent_config(), [{"role": "user", "content": "x"}])
test("FOUNDRY_MODEL_DEPLOYMENT overrides per-agent deployment", project.agents.created[0]["definition"].model == "gpt-5.6-luna")


print("\n\U0001f9ea Test 9: FoundryConfig.from_env parsing")
with patch.dict(os.environ, {"FOUNDRY_PROJECT_ENDPOINT": " https://e/api/projects/p ", "FOUNDRY_MAX_TOOL_ROUNDS": "2", "FOUNDRY_ENABLE_TOOLS": "false"}):
    cfg = backend_mod.FoundryConfig.from_env()
test("endpoint trimmed + configured", cfg.project_endpoint == "https://e/api/projects/p" and cfg.configured)
test("max_tool_rounds parsed", cfg.max_tool_rounds == 2)
test("enable_tools parsed", cfg.enable_tools is False)
test("default prefix oge-ops", cfg.agent_prefix == "oge-ops")
with patch.dict(os.environ, {"FOUNDRY_ENABLE_TOOLS": "maybe"}):
    try:
        backend_mod.FoundryConfig.from_env()
        test("invalid boolean rejected", False)
    except ValueError:
        test("invalid boolean rejected", True)
with patch.dict(os.environ, {"AGENT_BACKEND": "foundry", "FOUNDRY_PROJECT_ENDPOINT": "https://e/api/projects/p"}):
    test("get_backend reuses one Foundry backend per config", backend_mod.get_backend() is backend_mod.get_backend())


print("\n\U0001f9ea Test 10: analysis passes tool scope only to opting-in backends")


class OptInBackend:
    name = "opt_in"
    supports_tool_context = True

    def __init__(self):
        self.kwargs = []

    def complete(self, agent_config, messages, *, json_schema=None, schema_name="", tool_context=None):
        self.kwargs.append(tool_context)
        return backend_mod.BackendCompletion(agent="a", role="r", model="m", raw_text="{}", structured_output_used=False, usage={})


class LegacyBackend:
    name = "legacy"

    def complete(self, agent_config, messages, *, json_schema=None, schema_name=""):
        return backend_mod.BackendCompletion(agent="a", role="r", model="m", raw_text="{}", structured_output_used=False, usage={})


bundle = Obj(to_prompt_json=lambda: "{}")
opt_in = OptInBackend()
analysis_mod._call_specialist("cost_sentinel", question="q", bundle=bundle, backend=opt_in, tool_context=SCOPE)
test("opt-in backend receives tool_context", opt_in.kwargs == [SCOPE])
try:
    analysis_mod._call_specialist("cost_sentinel", question="q", bundle=bundle, backend=LegacyBackend(), tool_context=SCOPE)
    test("legacy backend signature still works", True)
except TypeError:
    test("legacy backend signature still works", False)


print("\n\U0001f9ea Test 11: specialist fan-out is parallel, bounded, and order-preserving")
active = {"now": 0, "max": 0}
lock = threading.Lock()


def slow_call(key):
    with lock:
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
    time.sleep(0.05)
    with lock:
        active["now"] -= 1
    return key.upper()


keys = ["scout", "cost_sentinel", "diagnostics_sre", "standards_architect", "compliance_inspector"]
with patch.dict(os.environ, {"ANALYSIS_MAX_PARALLEL_SPECIALISTS": "3"}):
    result = analysis_mod._fan_out(keys, slow_call)
test("results preserve routing order", list(result) == keys and result["scout"] == "SCOUT")
test("calls ran concurrently", active["max"] > 1)
test("concurrency bounded by ANALYSIS_MAX_PARALLEL_SPECIALISTS", active["max"] <= 3)
active["max"] = 0
with patch.dict(os.environ, {"ANALYSIS_MAX_PARALLEL_SPECIALISTS": "1"}):
    analysis_mod._fan_out(keys, slow_call)
test("ANALYSIS_MAX_PARALLEL_SPECIALISTS=1 runs sequentially", active["max"] == 1)


def _receipts(completion):
    return getattr(completion, "tool_receipts", None)


def _assert_receipt(label, receipt):
    test(f"{label} has agent/round/tool/status/duration/count", isinstance(receipt, dict) and {
        "agent_key", "round", "tool_name", "status", "duration_ms", "result_count",
    } <= set(receipt))
    blob = json.dumps(receipt)
    test(f"{label} has no arguments, scope, or output", "arguments" not in receipt and "subscription_ids" not in blob and "output" not in receipt)


print("\n\U0001f9ea Test 12: Foundry proof fields and tool receipts")
project = FakeProject([response("r0", [], text='{"conclusion":"none"}', status="completed")])
zero = make_backend(project).complete(make_agent_config(), [{"role": "user", "content": "quiet"}], tool_context=SCOPE)
test("zero-tool completion identifies foundry_agent_service", getattr(zero, "backend_name", None) == "foundry_agent_service")
test("zero-tool completion has empty tool receipts", _receipts(zero) == [])
test("zero-tool completion records provider response ids", isinstance(getattr(zero, "provider_response_ids", None), (list, tuple)))
test("zero-tool limit is not recorded as a tool call", getattr(zero, "tool_round_limited", False) is False)

project = FakeProject([
    response("r1", [fn_call("get_capacity_watch", {"subscription_ids": ["evil"]})]),
    response("r2", [], text='{"conclusion":"done"}'),
])
with patch.object(tools_mod, "execute_tool", return_value=tools_mod.ToolResult(
    tool_name="get_capacity_watch", status="ok", data={"rows": [1]}, result_count=1, duration_ms=4,
)):
    ok = make_backend(project).complete(make_agent_config(), [{"role": "user", "content": "why?"}], tool_context=SCOPE)
ok_receipts = _receipts(ok)
test("successful tool records one receipt", isinstance(ok_receipts, list) and len(ok_receipts) == 1)
if isinstance(ok_receipts, list) and ok_receipts:
    _assert_receipt("successful tool", ok_receipts[0])
    test("successful tool names the registry tool", ok_receipts[0].get("tool_name") == "get_capacity_watch")
    test("successful tool status is ok", ok_receipts[0].get("status") == "ok")

project = FakeProject([
    response("r1", [fn_call("get_capacity_watch", {})]),
    response("r2", [], text="{}"),
])
with patch.object(tools_mod, "execute_tool", return_value=tools_mod.ToolResult(
    tool_name="get_capacity_watch", status="error", data={"error": "boom"}, result_count=0, duration_ms=2,
)):
    failed = make_backend(project).complete(make_agent_config(), [{"role": "user", "content": "x"}], tool_context=SCOPE)
failed_receipts = _receipts(failed)
test("tool failure still records a receipt", isinstance(failed_receipts, list) and len(failed_receipts) == 1 and failed_receipts[0].get("status") == "error")
if isinstance(failed_receipts, list) and failed_receipts:
    test("tool failure receipt does not include the error body", "boom" not in json.dumps(failed_receipts[0]))

project = FakeProject([response("rN", [fn_call("get_capacity_watch", {})])])
with patch.object(tools_mod, "execute_tool", return_value=tools_mod.ToolResult(
    tool_name="get_capacity_watch", status="ok", data={}, result_count=0, duration_ms=1,
)):
    limited = make_backend(project, max_tool_rounds=1).complete(
        make_agent_config(), [{"role": "user", "content": "loop"}], tool_context=SCOPE,
    )
limited_receipts = _receipts(limited) or []
test("round limit is separate from executed receipts", getattr(limited, "tool_round_limited", None) is True or getattr(limited, "finish_reason", None) == "tool_round_limit")
test("unexecuted round-limit call is not a tool receipt", isinstance(_receipts(limited), list) and len(limited_receipts) == 1)
test("round-limit receipt is the executed call only", not any(item.get("status") == "tool_round_limit" for item in limited_receipts if isinstance(item, dict)))


print("\n🧪 Test 13: oversized Foundry tool output is truncated in both the receipt and the payload")
project = FakeProject([
    response("r1", [fn_call("get_capacity_watch", {"subscription_ids": ["evil-sub"]})]),
    response("r2", [], text='{"conclusion":"done"}', input_tokens=70, output_tokens=15),
])
big_tool_result = tools_mod.ToolResult(
    tool_name="get_capacity_watch",
    status="ok",
    data={"rows": [{"value": "x" * 4000}]},
    result_count=1,
    duration_ms=9,
)
with patch.object(tools_mod, "execute_tool", return_value=big_tool_result):
    truncated = make_backend(project, max_tool_output_chars=120).complete(
        make_agent_config(), [{"role": "user", "content": "overflow"}], tool_context=SCOPE,
    )
truncated_receipts = _receipts(truncated) or []
test("oversized tool output still records one receipt", len(truncated_receipts) == 1)
if truncated_receipts:
    test("oversized tool output receipt is marked truncated", truncated_receipts[0].get("status") == "truncated")
    payload = json.loads(project.responses.calls[1]["input"][0]["output"])
    test("oversized tool output is sent back as a truncated payload", payload["status"] == "truncated" and payload["tool_name"] == "get_capacity_watch")
    test("truncated payload carries the partial output marker", "partial" in payload and payload["partial"])
else:
    test("oversized tool output receipt is marked truncated", False)
    test("oversized tool output is sent back as a truncated payload", False)
    test("truncated payload carries the partial output marker", False)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
