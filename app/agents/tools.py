"""Typed, read-only tool registry wrapping bounded operations services.

This is the Foundry-ready tool boundary this app exposes (see
docs/FOUNDRY_ARCHITECTURE.md): every tool is read-only, declares an
explicit JSON Schema for its arguments, a required role, a wall-clock
timeout, and a bound on how many result items it can return. There is
deliberately NO generic "run this ARM/KQL query" tool -- every tool
wraps exactly one already-bounded operations service
(app/operations/{brief,queue,handoff,snapshot}.py; see
app/operations/finding_lookup.py for get_finding_evidence).

``execute_tool`` is the single entry point: it validates arguments
against the tool's own schema, checks the caller's roles (when
provided) against the tool's ``required_role``, runs the handler with a
wall-clock timeout bound (via a small thread pool -- Python cannot
forcibly cancel a running thread, so a timeout here means "stop
waiting", not "guaranteed to stop the underlying call"), bounds the
result size, and records an OTEL span (tool name/result count/duration
only -- never arguments or result content) via app/telemetry.py.
"""

import concurrent.futures
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app import telemetry
from app.operations import brief as brief_service
from app.operations import handoff as handoff_service
from app.operations import queue as queue_service
from app.operations.config import OperationsConfig
from app.operations.finding_lookup import bounded_evidence_view, find_finding_item
from app.operations.models import utc_now
from app.operations.snapshot import get_snapshot

__all__ = [
    "ToolDefinition",
    "ToolResult",
    "ToolArgumentError",
    "ToolExecutionError",
    "TOOLS",
    "get_tool",
    "list_tool_definitions",
    "validate_arguments",
    "execute_tool",
]

_DEFAULT_ROLE = "operations_reader"
_MAX_FINDINGS_PAGE_SIZE = 25
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ops-tool")


class ToolArgumentError(ValueError):
    """Arguments failed the tool's JSON Schema validation."""


class ToolExecutionError(RuntimeError):
    """The tool's handler raised an explicit, expected error (e.g. an
    unknown finding_id) -- distinct from an unexpected exception, but
    handled identically by execute_tool (both become status='error')."""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters_schema: dict
    handler: Callable
    read_only: bool = True
    required_role: str = _DEFAULT_ROLE
    timeout_seconds: float = 15.0
    max_result_items: int = 25

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters_schema": self.parameters_schema,
            "read_only": self.read_only,
            "required_role": self.required_role,
            "timeout_seconds": self.timeout_seconds,
            "max_result_items": self.max_result_items,
        }


@dataclass
class ToolResult:
    tool_name: str
    status: str
    data: Optional[dict]
    result_count: int
    duration_ms: float
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "status": self.status,
            "data": self.data,
            "result_count": self.result_count,
            "duration_ms": round(self.duration_ms, 2),
            "error": self.error,
        }


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "string": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _validate_node(node_schema: dict, value, path: str) -> None:
    expected_type = node_schema.get("type")
    if expected_type is not None:
        check = _TYPE_CHECKS.get(expected_type)
        if check is None:
            raise ToolArgumentError(f"{path}: unsupported schema type {expected_type!r}")
        if not check(value):
            raise ToolArgumentError(f"{path}: expected type {expected_type!r}, got {type(value).__name__}")

    if "enum" in node_schema and value not in node_schema["enum"]:
        raise ToolArgumentError(f"{path}: must be one of {node_schema['enum']}, got {value!r}")

    if expected_type == "object":
        properties = node_schema.get("properties", {})
        for key in node_schema.get("required", []):
            if key not in value:
                raise ToolArgumentError(f"{path}: missing required property {key!r}")
        if node_schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ToolArgumentError(f"{path}: unexpected propert{'y' if len(unknown) == 1 else 'ies'} {unknown}")
        for key, subvalue in value.items():
            if key in properties:
                _validate_node(properties[key], subvalue, f"{path}.{key}")

    elif expected_type == "array":
        if "minItems" in node_schema and len(value) < node_schema["minItems"]:
            raise ToolArgumentError(f"{path}: expected at least {node_schema['minItems']} item(s), got {len(value)}")
        item_schema = node_schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate_node(item_schema, item, f"{path}[{i}]")

    elif expected_type in ("integer", "number"):
        if "minimum" in node_schema and value < node_schema["minimum"]:
            raise ToolArgumentError(f"{path}: must be >= {node_schema['minimum']}, got {value}")
        if "maximum" in node_schema and value > node_schema["maximum"]:
            raise ToolArgumentError(f"{path}: must be <= {node_schema['maximum']}, got {value}")


def validate_arguments(schema: dict, arguments) -> None:
    _validate_node(schema, arguments, "arguments")


_SUBSCRIPTION_IDS_PROPERTY = {"type": "array", "items": {"type": "string"}, "minItems": 1}
_FORCE_REFRESH_PROPERTY = {"type": "boolean"}


def _bounded_object_schema(extra_properties: dict, required_extra: list) -> dict:
    properties = {"subscription_ids": _SUBSCRIPTION_IDS_PROPERTY, "force_refresh": _FORCE_REFRESH_PROPERTY}
    properties.update(extra_properties)
    return {
        "type": "object", "properties": properties,
        "required": ["subscription_ids"] + required_extra, "additionalProperties": False,
    }


def _subscription_ids_arg(arguments: dict) -> list:
    return list(arguments["subscription_ids"])


def _handle_get_executive_brief(arguments: dict, config: OperationsConfig):
    snapshot = get_snapshot(
        _subscription_ids_arg(arguments), config=config, force_refresh=bool(arguments.get("force_refresh"))
    )
    return brief_service.build_brief(snapshot), 1


def _handle_list_prioritized_findings(arguments: dict, config: OperationsConfig):
    snapshot = get_snapshot(
        _subscription_ids_arg(arguments), config=config, force_refresh=bool(arguments.get("force_refresh"))
    )
    page_size = min(int(arguments.get("page_size", queue_service.DEFAULT_PAGE_SIZE)), _MAX_FINDINGS_PAGE_SIZE)
    result = queue_service.build_queue(
        snapshot.findings,
        status=arguments.get("status"), category=arguments.get("category"),
        severity=arguments.get("severity"), owner=arguments.get("owner"),
        page=int(arguments.get("page", 1)), page_size=page_size,
    )
    return result, len(result["items"])


def _handle_get_finding_evidence(arguments: dict, config: OperationsConfig):
    snapshot = get_snapshot(
        _subscription_ids_arg(arguments), config=config, force_refresh=bool(arguments.get("force_refresh"))
    )
    item = find_finding_item(snapshot, arguments["finding_id"])
    if item is None:
        raise ToolExecutionError(f"finding {arguments['finding_id']!r} not found in the current snapshot")
    view = bounded_evidence_view(item["finding"])
    return view, len(view.get("evidence") or [])


def _handle_get_capacity_watch(arguments: dict, config: OperationsConfig):
    snapshot = get_snapshot(
        _subscription_ids_arg(arguments), config=config, force_refresh=bool(arguments.get("force_refresh"))
    )
    envelopes_by_source = {e.source: e for e in snapshot.envelopes}
    watch = handoff_service.capacity_watch(envelopes_by_source)
    return {"capacity_watch": watch}, len(watch)


def _handle_get_recent_changes(arguments: dict, config: OperationsConfig):
    snapshot = get_snapshot(
        _subscription_ids_arg(arguments), config=config, force_refresh=bool(arguments.get("force_refresh"))
    )
    envelopes_by_source = {e.source: e for e in snapshot.envelopes}
    changes = handoff_service.recent_changes_since(envelopes_by_source, now=utc_now())
    return {"recent_changes": changes}, len(changes)


def _handle_get_source_coverage(arguments: dict, config: OperationsConfig):
    snapshot = get_snapshot(
        _subscription_ids_arg(arguments), config=config, force_refresh=bool(arguments.get("force_refresh"))
    )
    return {"coverage": snapshot.coverage}, snapshot.coverage.get("total_sources", 0)


TOOLS: dict = {
    "get_executive_brief": ToolDefinition(
        name="get_executive_brief",
        description="Deterministic executive operations brief (business impact, reliability, capacity, decisions required) -- no LLM call.",
        parameters_schema=_bounded_object_schema({}, []),
        handler=_handle_get_executive_brief,
        timeout_seconds=15.0, max_result_items=1,
    ),
    "list_prioritized_findings": ToolDefinition(
        name="list_prioritized_findings",
        description="Paginated, priority-ordered operations findings, optionally filtered by status/category/severity/owner.",
        parameters_schema=_bounded_object_schema({
            "status": {"type": "string"}, "category": {"type": "string"}, "severity": {"type": "string"},
            "owner": {"type": "string"}, "page": {"type": "integer", "minimum": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": _MAX_FINDINGS_PAGE_SIZE},
        }, []),
        handler=_handle_list_prioritized_findings,
        timeout_seconds=15.0, max_result_items=_MAX_FINDINGS_PAGE_SIZE,
    ),
    "get_finding_evidence": ToolDefinition(
        name="get_finding_evidence",
        description="Bounded evidence detail for exactly one finding id.",
        parameters_schema=_bounded_object_schema({"finding_id": {"type": "string"}}, ["finding_id"]),
        handler=_handle_get_finding_evidence,
        timeout_seconds=15.0, max_result_items=10,
    ),
    "get_capacity_watch": ToolDefinition(
        name="get_capacity_watch",
        description="Capacity/quota line items currently in warning or critical threshold state.",
        parameters_schema=_bounded_object_schema({}, []),
        handler=_handle_get_capacity_watch,
        timeout_seconds=15.0, max_result_items=25,
    ),
    "get_recent_changes": ToolDefinition(
        name="get_recent_changes",
        description="Azure changes correlated within the last 24h change/health window.",
        parameters_schema=_bounded_object_schema({}, []),
        handler=_handle_get_recent_changes,
        timeout_seconds=15.0, max_result_items=25,
    ),
    "get_source_coverage": ToolDefinition(
        name="get_source_coverage",
        description="Evidence source coverage/gap inventory (ok/error/not_configured counts by source).",
        parameters_schema=_bounded_object_schema({}, []),
        handler=_handle_get_source_coverage,
        timeout_seconds=15.0, max_result_items=1,
    ),
}


def get_tool(name: str) -> Optional[ToolDefinition]:
    return TOOLS.get(name)


def list_tool_definitions() -> list:
    return [tool.to_dict() for tool in TOOLS.values()]


def _bound_result(data, max_items: int):
    """Defensive backstop: truncate any top-level list value to
    `max_items`, regardless of whether the handler already bounded it
    internally (e.g. via page_size)."""
    if not isinstance(data, dict):
        return data
    bounded = dict(data)
    for key, value in bounded.items():
        if isinstance(value, list) and len(value) > max_items:
            bounded[key] = value[:max_items]
    return bounded


def execute_tool(
    name: str,
    arguments: dict,
    *,
    caller_roles: Optional[set] = None,
    config: Optional[OperationsConfig] = None,
) -> ToolResult:
    """Validate, authorize, and run one registered tool -- see module
    docstring for the full contract. Never raises for an expected failure
    mode (unknown tool/denied/invalid arguments/timeout/handler error);
    every one of those becomes an explicit ToolResult instead."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(tool_name=name, status="error", data=None, result_count=0, duration_ms=0.0, error=f"unknown tool {name!r}")

    if caller_roles is not None and tool.required_role not in caller_roles:
        return ToolResult(
            tool_name=name, status="denied", data=None, result_count=0, duration_ms=0.0,
            error=f"caller is missing required role {tool.required_role!r}",
        )

    try:
        validate_arguments(tool.parameters_schema, arguments)
    except ToolArgumentError as exc:
        return ToolResult(tool_name=name, status="invalid_arguments", data=None, result_count=0, duration_ms=0.0, error=str(exc))

    config = config or OperationsConfig.from_env()
    start = time.monotonic()
    status, error, data, count = "ok", None, None, 0

    with telemetry.tool_call_span(tool_name=name) as span:
        try:
            future = _EXECUTOR.submit(tool.handler, arguments, config)
            data, count = future.result(timeout=tool.timeout_seconds)
        except concurrent.futures.TimeoutError:
            status, error = "timeout", f"exceeded {tool.timeout_seconds}s timeout bound"
        except ToolExecutionError as exc:
            status, error = "error", str(exc)
        except Exception as exc:  # noqa: BLE001 -- last-resort tool boundary, mirrors app/operations/routes.py's route-boundary convention: never a bare except/pass, always converted into an explicit ToolResult
            status, error = "error", str(exc)
        span.set_status(status)
        span.set_result_count(count)

    duration_ms = (time.monotonic() - start) * 1000
    bounded_data = _bound_result(data, tool.max_result_items) if data is not None else None
    return ToolResult(tool_name=name, status=status, data=bounded_data, result_count=count, duration_ms=duration_ms, error=error)
