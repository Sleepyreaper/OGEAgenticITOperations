"""Azure Monitor OpenTelemetry integration — spans for agent OpenAI calls.

Uses the ``azure-monitor-opentelemetry`` distro (the "one-stop" package),
which auto-instruments Flask and wires the OTel SDK to export to
Application Insights via ``APPLICATIONINSIGHTS_CONNECTION_STRING``. This
module does NOT separately instrument Flask — the distro already does
that, and doing it twice would double-count HTTP request telemetry.

Telemetry is a strict opt-in, keyed entirely off that one connection
string:

  * Connection string absent (the normal local-dev state) -> every public
    function here is a documented no-op, nothing is imported/configured,
    and no error is raised.
  * Connection string present but malformed -> ``configure_azure_monitor``
    is allowed to raise. That's a real misconfiguration and should fail
    loudly at startup, not be swallowed.

Workspace-based Application Insights maps this module's output as:

  * Flask HTTP requests             -> AppRequests   (via the distro)
  * ``agent_call_span`` (below)     -> AppDependencies (Properties = the
                                        ``gen_ai.*``/``ops.*`` attributes)
  * Standard library logging        -> AppTraces      (via the distro)
  * The optional counters/histogram -> AppMetrics

See docs/TELEMETRY.md for the full architecture, KQL query reference, and
sampling/retention/cost-estimate caveats.

GenAI span attribute names (``gen_ai.*``) below follow the OpenTelemetry
GenAI semantic conventions, which are Development/experimental as of
2026. They're spelled out literally here (rather than depending on the
separate, still-evolving ``opentelemetry-semantic-conventions-ai``
package) specifically so this module has no dependency on a convention
that could rename/relocate the constants out from under us.

Prompts, responses, system instructions, subscription IDs, endpoints, and
other Azure resource names are never recorded as span/metric attributes
by this module or by app/agents/runner.py — content capture is
intentionally off (see docs/TELEMETRY.md — "AppGenAIContent stays
empty").
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

# Module-level state, guarded by _initialized so init_telemetry() is safe
# to call more than once (e.g. across tests, or if create_app() is ever
# invoked twice in the same process) without double-configuring the SDK.
_initialized = False
_enabled = False
_tracer = None
_call_counter = None
_token_counter = None
_duration_histogram = None
_cost_counter = None
# Tool-call/routing/evaluation instrumentation (see tool_call_span,
# record_routing_decision, record_evaluation_metrics below) -- same
# no-op-unless-enabled convention as the agent-call counters above.
_tool_call_counter = None
_tool_duration_histogram = None
_routing_decision_counter = None
_evaluation_schema_valid_counter = None
_evaluation_schema_invalid_counter = None
_evaluation_unsupported_citation_counter = None


def init_telemetry() -> bool:
    """Idempotently configure Azure Monitor OpenTelemetry, if enabled.

    Reads ``APPLICATIONINSIGHTS_CONNECTION_STRING`` directly from the
    environment. Must be called before the Flask app is constructed (see
    app/main.py::create_app) so the distro's Flask auto-instrumentation
    attaches to the real app instance.

    Returns whether telemetry ended up enabled. A missing connection
    string is a normal, expected state (e.g. local dev) — this returns
    False silently in that case rather than raising.
    """
    global _initialized, _enabled, _tracer
    global _call_counter, _token_counter, _duration_histogram, _cost_counter
    global _tool_call_counter, _tool_duration_histogram, _routing_decision_counter
    global _evaluation_schema_valid_counter, _evaluation_schema_invalid_counter
    global _evaluation_unsupported_citation_counter

    if _initialized:
        return _enabled
    _initialized = True

    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        return False

    # Imported lazily (not at module top-level) so that importing
    # app.telemetry never requires azure-monitor-opentelemetry to be
    # installed unless telemetry is actually going to be enabled — e.g.
    # local dev without the connection string, or unit tests that only
    # exercise the disabled-path no-ops, work without the dependency.
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry import trace, metrics

    # Profile-safe default: profile ids are short generic slugs (e.g.
    # "power", "oge", a customer's own profile id) rather than a
    # free-text brand string, so this never leaks anything sensitive by
    # default. Standard OTEL_SERVICE_NAME always wins if set.
    if not os.environ.get("OTEL_SERVICE_NAME", "").strip():
        from app.config import settings  # local import: avoid any import-time cycle

        os.environ["OTEL_SERVICE_NAME"] = f"ops-council-{settings.profile_id}"

    # configure_azure_monitor reads OTEL_SERVICE_NAME (and any other
    # standard OTEL_* env vars, e.g. OTEL_TRACES_SAMPLER /
    # OTEL_TRACES_SAMPLER_ARG for sampling) itself — nothing else to pass
    # here. Any malformed connection string raises from this call, and
    # that's intentional: a broken app setting should fail loudly at
    # startup, not be silently swallowed into "telemetry disabled".
    configure_azure_monitor(connection_string=connection_string)

    _tracer = trace.get_tracer("ops_council.agents")
    meter = metrics.get_meter("ops_council.agents")
    _call_counter = meter.create_counter(
        "ops_council.agent.calls",
        unit="1",
        description="Agent OpenAI chat completion calls, by agent key and model.",
    )
    _token_counter = meter.create_counter(
        "ops_council.agent.tokens",
        unit="token",
        description="Prompt + completion tokens consumed, by agent key, model, and token type.",
    )
    _duration_histogram = meter.create_histogram(
        "ops_council.agent.duration",
        unit="ms",
        description="Agent OpenAI call latency, by agent key and model.",
    )
    _cost_counter = meter.create_counter(
        "ops_council.agent.cost_usd",
        unit="usd",
        description="Estimated cost (caller-maintained pricing, not billing truth), by agent key and model.",
    )
    _tool_call_counter = meter.create_counter(
        "ops_council.tool.calls",
        unit="1",
        description="Typed operations-tool invocations (app/agents/tools.py), by tool name and status.",
    )
    _tool_duration_histogram = meter.create_histogram(
        "ops_council.tool.duration",
        unit="ms",
        description="Typed operations-tool invocation latency, by tool name.",
    )
    _routing_decision_counter = meter.create_counter(
        "ops_council.routing.decisions",
        unit="1",
        description="Agent-analysis routing decisions (app/agents/routing.py), by debate/coordinator flags.",
    )
    _evaluation_schema_valid_counter = meter.create_counter(
        "ops_council.evaluation.schema_valid",
        unit="1",
        description="Grounded-analysis responses that parsed/validated against AGENT_ANALYSIS_JSON_SCHEMA.",
    )
    _evaluation_schema_invalid_counter = meter.create_counter(
        "ops_council.evaluation.schema_invalid",
        unit="1",
        description="Grounded-analysis responses that FAILED schema validation (see app/agents/schema.py).",
    )
    _evaluation_unsupported_citation_counter = meter.create_counter(
        "ops_council.evaluation.unsupported_citations",
        unit="1",
        description="Evidence ids cited by a model that were not present in the evidence bundle it was given.",
    )
    _enabled = True
    return True


def is_enabled() -> bool:
    """Whether telemetry was successfully configured (used by /api/health)."""
    return _enabled


def reset_for_tests() -> None:
    """Test-only hook: undo init_telemetry()'s idempotency guard.

    Production code never calls this. It exists so tests can exercise
    init_telemetry() more than once (e.g. once with no connection string,
    once with a fake one) in the same process without a real server
    restart.
    """
    global _initialized, _enabled, _tracer
    global _call_counter, _token_counter, _duration_histogram, _cost_counter
    global _tool_call_counter, _tool_duration_histogram, _routing_decision_counter
    global _evaluation_schema_valid_counter, _evaluation_schema_invalid_counter
    global _evaluation_unsupported_citation_counter
    _initialized = False
    _enabled = False
    _tracer = None
    _call_counter = None
    _token_counter = None
    _duration_histogram = None
    _cost_counter = None
    _tool_call_counter = None
    _tool_duration_histogram = None
    _routing_decision_counter = None
    _evaluation_schema_valid_counter = None
    _evaluation_schema_invalid_counter = None
    _evaluation_unsupported_citation_counter = None


class _SpanRecorder:
    """Thin wrapper so app/agents/runner.py never has to branch on
    ``is_enabled()`` itself — every method here is a no-op when telemetry
    isn't configured (``span is None``)."""

    __slots__ = ("_span",)

    def __init__(self, span):
        self._span = span

    def set_response_model(self, model: Optional[str]) -> None:
        if self._span is not None and model:
            self._span.set_attribute("gen_ai.response.model", model)

    def set_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        if self._span is not None:
            self._span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
            self._span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)

    def set_finish_reasons(self, reasons: Optional[Sequence[str]]) -> None:
        if self._span is not None and reasons:
            self._span.set_attribute("gen_ai.response.finish_reasons", list(reasons))

    def set_cost(self, cost_usd: float) -> None:
        if self._span is not None:
            self._span.set_attribute("ops.estimated_cost_usd", cost_usd)


@contextmanager
def agent_call_span(
    *, agent_key: str, agent_name: str, profile_id: str, model: str
) -> Iterator[_SpanRecorder]:
    """Wrap a single agent OpenAI chat completion call.

    Always usable regardless of whether telemetry is enabled — yields a
    ``_SpanRecorder`` that's a no-op when it isn't. When enabled, records
    a span landing in AppDependencies with the ``gen_ai.*``/``ops.*``
    attributes described in docs/TELEMETRY.md, records exceptions with an
    ERROR span status, and (if any exception propagates) re-raises it
    unchanged — this module never swallows a caller's exception.
    """
    if not _enabled:
        yield _SpanRecorder(None)
        return

    from opentelemetry.trace import Status, StatusCode

    attributes = {
        "ops.agent.key": agent_key,
        "gen_ai.request.model": model,
    }
    start = time.monotonic()
    with _tracer.start_as_current_span("agent_chat_completion") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "azure.ai.openai")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("ops.agent.key", agent_key)
        span.set_attribute("ops.agent.name", agent_name)
        span.set_attribute("ops.profile", profile_id)
        recorder = _SpanRecorder(span)
        try:
            yield recorder
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            if _duration_histogram is not None:
                _duration_histogram.record(duration_ms, attributes)
            if _call_counter is not None:
                _call_counter.add(1, attributes)


def record_usage(
    *, agent_key: str, model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float
) -> None:
    """Record token/cost counter metrics for a completed agent call.

    A no-op when telemetry isn't enabled. Kept separate from
    ``agent_call_span`` so callers that only have usage numbers *after*
    the span context has already recorded latency/call-count can still
    emit them (metrics, unlike span attributes, don't need to be set
    before the ``with`` block exits).
    """
    if not _enabled:
        return
    attributes = {"ops.agent.key": agent_key, "gen_ai.request.model": model}
    if _token_counter is not None:
        _token_counter.add(prompt_tokens, {**attributes, "gen_ai.token.type": "input"})
        _token_counter.add(completion_tokens, {**attributes, "gen_ai.token.type": "output"})
    if _cost_counter is not None and cost_usd:
        _cost_counter.add(cost_usd, attributes)


class _ToolSpanRecorder:
    """Same no-op-unless-enabled convention as ``_SpanRecorder`` above,
    scoped to one typed operations-tool invocation
    (app/agents/tools.py::execute_tool). Records ONLY the tool name,
    result count, and status -- never the tool's arguments or result
    content (which may include finding titles/summaries)."""

    __slots__ = ("_span",)

    def __init__(self, span):
        self._span = span

    def set_result_count(self, count: int) -> None:
        if self._span is not None:
            self._span.set_attribute("ops.tool.result_count", count)

    def set_status(self, status: str) -> None:
        if self._span is not None:
            self._span.set_attribute("ops.tool.status", status)


@contextmanager
def tool_call_span(*, tool_name: str) -> Iterator[_ToolSpanRecorder]:
    """Wrap one typed operations-tool invocation
    (app/agents/tools.py::execute_tool) -- a parent/child span alongside
    ``agent_call_span`` so tool execution is auditable the same way
    agent calls are. Always usable regardless of whether telemetry is
    enabled; re-raises any exception unchanged."""
    if not _enabled:
        yield _ToolSpanRecorder(None)
        return

    from opentelemetry.trace import Status, StatusCode

    attributes = {"ops.tool.name": tool_name}
    start = time.monotonic()
    with _tracer.start_as_current_span("ops_tool_execution") as span:
        span.set_attribute("ops.tool.name", tool_name)
        recorder = _ToolSpanRecorder(span)
        try:
            yield recorder
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            if _tool_duration_histogram is not None:
                _tool_duration_histogram.record(duration_ms, attributes)
            if _tool_call_counter is not None:
                _tool_call_counter.add(1, attributes)


def record_routing_decision(*, debate: bool, specialist_count: int, coordinator_included: bool) -> None:
    """No-op unless telemetry is enabled. Records ONLY the routing shape
    (a count and two booleans) -- never the question text or any
    evidence content (see app/agents/routing.py)."""
    if not _enabled or _routing_decision_counter is None:
        return
    _routing_decision_counter.add(1, {
        "ops.routing.debate": debate,
        "ops.routing.specialist_count": specialist_count,
        "ops.routing.coordinator_included": coordinator_included,
    })


def record_evaluation_metrics(*, schema_valid: bool, unsupported_citation_count: int) -> None:
    """No-op unless telemetry is enabled. Records ONLY the deterministic
    evaluation counts computed by app.agents.evaluation -- never any
    prompt/response content (see app/agents/evaluation.py)."""
    if not _enabled:
        return
    if schema_valid and _evaluation_schema_valid_counter is not None:
        _evaluation_schema_valid_counter.add(1)
    if not schema_valid and _evaluation_schema_invalid_counter is not None:
        _evaluation_schema_invalid_counter.add(1)
    if unsupported_citation_count and _evaluation_unsupported_citation_counter is not None:
        _evaluation_unsupported_citation_counter.add(unsupported_citation_count)
