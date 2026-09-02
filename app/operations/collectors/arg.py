"""Shared Azure Resource Graph (ARG) query helper for operations
collectors.

Mirrors app.operations.collectors.http.arm_get's role for ARM REST: every
collector that queries Resource Graph goes through `arg_query` so error
handling and dependency injection are consistent and centrally testable
-- no collector calls `app.azure_data.query_resource_graph` (or the
underlying azure-mgmt-resourcegraph SDK) directly.

Reuses app.azure_data.query_resource_graph as the real default
implementation (already used elsewhere in this codebase, e.g.
app.azure_data.get_deep_analysis) rather than adding a second ARG client.
"""

from typing import Callable

from app.azure_data import query_resource_graph as default_query_resource_graph
from app.operations.errors import OperationsCollectionError

__all__ = ["QueryResourceGraphFn", "default_query_resource_graph", "arg_query"]

# Matches app.azure_data.query_resource_graph's signature exactly:
# (query, subscription_id=None, subscription_ids=None) -> list[dict].
QueryResourceGraphFn = Callable[..., list]


def arg_query(
    query: str,
    *,
    subscription_ids: list,
    source: str,
    query_fn: QueryResourceGraphFn = default_query_resource_graph,
) -> list:
    """Run a Resource Graph KQL `query` scoped to `subscription_ids` and
    return the resulting rows.

    Always raises OperationsCollectionError -- never returns a
    success-shaped empty list -- if the underlying query call raises for
    any reason (auth failure, throttling, a malformed query). A query
    that runs successfully but matches nothing is a legitimate empty
    list, not an error.
    """
    if not subscription_ids:
        raise ValueError("subscription_ids is required")
    try:
        rows = query_fn(query, subscription_ids=list(subscription_ids))
    except Exception as exc:
        raise OperationsCollectionError(source, "Resource Graph query failed", detail=str(exc)) from exc
    if rows is None:
        raise OperationsCollectionError(source, "Resource Graph query returned no result (None)")
    return rows
