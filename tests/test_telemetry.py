#!/usr/bin/env python3
"""Test app/telemetry.py — no-op behavior without a connection string, and
span/attribute correctness with an in-memory OpenTelemetry span exporter
(no real Azure Monitor configuration, no network calls).

Requires the azure-monitor-opentelemetry / opentelemetry-sdk packages from
requirements.txt to be installed (see the project .venv). Skips cleanly
with a clear message if they aren't available, rather than failing the
whole test run in an environment that hasn't installed the new dependency.

Run: python3 tests/test_telemetry.py
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Telemetry reads this once, lazily, inside init_telemetry() — make sure
# no real value leaks in from the outer shell environment before we start
# controlling it explicitly per-test below.
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

from app import telemetry  # noqa: E402

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


# ─── Test 1: no connection string -> telemetry stays fully disabled ────
print("\n\U0001f9ea Test 1: init_telemetry() without a connection string is a no-op")
telemetry.reset_for_tests()
os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
test("is_enabled() is False before init", telemetry.is_enabled() is False)
result = telemetry.init_telemetry()
test("init_telemetry() returns False", result is False)
test("is_enabled() is False after init", telemetry.is_enabled() is False)

# The span/usage helpers must still be safely callable — no exceptions,
# no attribute errors — even though telemetry is disabled.
no_op_ok = True
try:
    with telemetry.agent_call_span(agent_key="scout", agent_name="Operations Monitor",
                                    profile_id="power", model="gpt-5.6-luna") as span:
        span.set_response_model("gpt-5.6-luna-2026-01-01")
        span.set_usage(10, 5)
        span.set_finish_reasons(["stop"])
        span.set_cost(0.0)
    telemetry.record_usage(agent_key="scout", model="gpt-5.6-luna", prompt_tokens=10, completion_tokens=5, cost_usd=0.0)
except Exception:
    no_op_ok = False
test("agent_call_span/record_usage no-op cleanly when disabled", no_op_ok)

no_op_reraises = False
try:
    with telemetry.agent_call_span(agent_key="scout", agent_name="Operations Monitor",
                                    profile_id="power", model="gpt-5.6-luna"):
        raise ValueError("boom")
except ValueError:
    no_op_reraises = True
test("agent_call_span still re-raises exceptions when disabled (never swallows)", no_op_reraises)

telemetry.reset_for_tests()

# ─── Test 2+: span attributes via an in-memory exporter (no Azure calls) ──
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    OTEL_SDK_AVAILABLE = True
except ImportError:
    OTEL_SDK_AVAILABLE = False

if not OTEL_SDK_AVAILABLE:
    print(
        "\n\u26a0\ufe0f  opentelemetry-sdk not installed (azure-monitor-opentelemetry from "
        "requirements.txt) — skipping span-attribute tests. Install into a project .venv "
        "(`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`) to run them."
    )
else:
    print("\n\U0001f9ea Test 2: agent_call_span records the documented gen_ai.*/ops.* attributes")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Wire the module's private state directly to a throwaway in-memory
    # TracerProvider — this exercises the exact same code path
    # init_telemetry() would (real spans, real attribute-setting) without
    # ever calling configure_azure_monitor() or touching a real Azure
    # Monitor connection string/endpoint.
    telemetry._enabled = True
    telemetry._tracer = provider.get_tracer("test")
    telemetry._call_counter = None
    telemetry._token_counter = None
    telemetry._duration_histogram = None
    telemetry._cost_counter = None

    with telemetry.agent_call_span(
        agent_key="cost_sentinel", agent_name="Cost & Capacity Analyst",
        profile_id="power", model="gpt-5.6-terra",
    ) as span:
        span.set_response_model("gpt-5.6-terra-2026-01-01")
        span.set_usage(120, 80)
        span.set_finish_reasons(["stop"])
        span.set_cost(0.00084)

    spans = exporter.get_finished_spans()
    test("exactly one span recorded for the call", len(spans) == 1)
    attrs = spans[0].attributes if spans else {}
    test("gen_ai.operation.name == 'chat'", attrs.get("gen_ai.operation.name") == "chat")
    test("gen_ai.provider.name == 'azure.ai.openai'", attrs.get("gen_ai.provider.name") == "azure.ai.openai")
    test("gen_ai.request.model == deployment name", attrs.get("gen_ai.request.model") == "gpt-5.6-terra")
    test("gen_ai.response.model set when provided", attrs.get("gen_ai.response.model") == "gpt-5.6-terra-2026-01-01")
    test("gen_ai.usage.input_tokens", attrs.get("gen_ai.usage.input_tokens") == 120)
    test("gen_ai.usage.output_tokens", attrs.get("gen_ai.usage.output_tokens") == 80)
    test("gen_ai.response.finish_reasons", list(attrs.get("gen_ai.response.finish_reasons", [])) == ["stop"])
    test("ops.agent.key == agent key (not display name)", attrs.get("ops.agent.key") == "cost_sentinel")
    test("ops.agent.name == profile display name", attrs.get("ops.agent.name") == "Cost & Capacity Analyst")
    test("ops.profile == loaded profile id", attrs.get("ops.profile") == "power")
    test("ops.estimated_cost_usd recorded", abs(attrs.get("ops.estimated_cost_usd", -1) - 0.00084) < 1e-9)
    test(
        "no prompt/response/system-instruction content captured as an attribute",
        not any("prompt" in str(k).lower() and "usage" not in str(k).lower() for k in attrs.keys())
        and not any(isinstance(v, str) and ("You are" in v or "RESPONSE INSTRUCTION" in v) for v in attrs.values()),
    )

    print("\n\U0001f9ea Test 3: agent_call_span records exceptions with ERROR status and re-raises")
    exporter.clear()
    raised = False
    try:
        with telemetry.agent_call_span(
            agent_key="scout", agent_name="Operations Monitor", profile_id="power", model="gpt-5.6-luna",
        ):
            raise RuntimeError("simulated Azure OpenAI failure")
    except RuntimeError:
        raised = True
    test("exception propagates unchanged (never swallowed)", raised)

    error_spans = exporter.get_finished_spans()
    test("a span was still recorded for the failed call", len(error_spans) == 1)
    if error_spans:
        test("span status is ERROR", error_spans[0].status.status_code.name == "ERROR")
        test("exception event recorded on the span", any(e.name == "exception" for e in error_spans[0].events))

    telemetry.reset_for_tests()


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
