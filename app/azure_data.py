"""Azure data services — Resource Graph, Monitor, Cost Management.

Provides the factual data that agents reason over. Everything here uses
the Managed Identity (AZURE_CLIENT_ID) for auth, scoped to Reader on
the target subscription.
"""

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from azure.monitor.query import LogsQueryClient
from datetime import timedelta
import os
import json


def _credential():
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def _subscription_id():
    return os.environ.get(
        "AZURE_SUBSCRIPTION_ID",
        # fallback: extract from the Key Vault URI's subscription if set
        "",
    )


# ─── Subscription Discovery ─────────────────────────────────────

def list_subscriptions() -> list[dict]:
    """List all Azure subscriptions accessible to the Managed Identity."""
    from azure.mgmt.resource import SubscriptionClient
    cred = _credential()
    client = SubscriptionClient(cred)
    subs = []
    for sub in client.subscriptions.list():
        subs.append({
            "id": sub.subscription_id,
            "name": sub.display_name,
            "state": sub.state.value if sub.state else "Unknown",
            "tenant": sub.tenant_id,
        })
    return subs


# ─── Resource Graph ──────────────────────────────────────────────

def query_resource_graph(query: str, subscription_id: str = None,
                         subscription_ids: list[str] = None) -> list[dict]:
    """Run an ARG query and return rows as dicts.
    Supports single sub (subscription_id) or multiple (subscription_ids)."""
    cred = _credential()
    client = ResourceGraphClient(cred)
    if subscription_ids:
        subs = subscription_ids
    else:
        subs = [subscription_id or _subscription_id()]
    request = QueryRequest(
        subscriptions=subs,
        query=query,
    )
    response = client.resources(request)
    return response.data if isinstance(response.data, list) else []


def get_all_resources(subscription_id: str = None,
                      subscription_ids: list[str] = None) -> list[dict]:
    return query_resource_graph(
        "Resources | project name, type, location, resourceGroup, "
        "tags, properties.provisioningState, sku, subscriptionId",
        subscription_id, subscription_ids,
    )


def get_resource_health(subscription_id: str = None) -> list[dict]:
    return query_resource_graph(
        "HealthResources | where type =~ 'microsoft.resourcehealth/availabilitystatuses' "
        "| project name=properties.targetResourceName, status=properties.availabilityState, "
        "resourceGroup, summary=properties.summary",
        subscription_id,
    )


def get_orphaned_disks(subscription_id: str = None) -> list[dict]:
    return query_resource_graph(
        "Resources | where type =~ 'Microsoft.Compute/disks' "
        "| where isempty(managedBy) "
        "| project name, resourceGroup, location, sku.name, properties.diskSizeGB, tags",
        subscription_id,
    )


def get_public_endpoints(subscription_id: str = None) -> list[dict]:
    return query_resource_graph(
        "Resources | where type =~ 'Microsoft.Network/publicIPAddresses' "
        "| project name, resourceGroup, ipAddress=properties.ipAddress, "
        "allocation=properties.publicIPAllocationMethod, associated=properties.ipConfiguration.id",
        subscription_id,
    )


def get_tagging_compliance(subscription_id: str = None) -> list[dict]:
    return query_resource_graph(
        "ResourceContainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
        "| extend supportOwner = tags['support-owner'] "
        "| project name, supportOwner, location, tags",
        subscription_id,
    )


# ─── Log Analytics ───────────────────────────────────────────────

def query_logs(query: str, workspace_id: str = None, timespan: timedelta = None) -> list[dict]:
    """Run a KQL query against Log Analytics."""
    cred = _credential()
    client = LogsQueryClient(cred)
    ws = workspace_id or os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "")
    ts = timespan or timedelta(hours=24)

    response = client.query_workspace(ws, query, timespan=ts)

    rows = []
    if hasattr(response, "tables"):
        for table in response.tables:
            columns = [col.name for col in table.columns]
            for row in table.rows:
                rows.append(dict(zip(columns, row)))
    return rows


def get_recent_activity_errors(hours: int = 24) -> list[dict]:
    return query_logs(
        f"AzureActivity | where TimeGenerated > ago({hours}h) "
        "| where ActivityStatusValue == 'Failed' "
        "| project TimeGenerated, OperationNameValue, ResourceGroup, "
        "CallerIpAddress, Properties_d, ActivityStatusValue "
        "| order by TimeGenerated desc | take 50"
    )


def get_deployment_failures(hours: int = 24) -> list[dict]:
    return query_logs(
        f"AzureActivity | where TimeGenerated > ago({hours}h) "
        "| where OperationNameValue has 'deployments' and ActivityStatusValue == 'Failed' "
        "| project TimeGenerated, OperationNameValue, ResourceGroup, Properties_d "
        "| order by TimeGenerated desc | take 20"
    )


# ─── Metrics (VM utilization for cost/sizing analysis) ───────────

def get_vm_metrics_summary(resource_id: str, metric: str = "Percentage CPU",
                           hours: int = 168) -> dict:
    """Get avg/max/min for a VM metric over the specified window.
    Uses azure-mgmt-monitor since azure-monitor-query v2 removed MetricsQueryClient.
    """
    from azure.mgmt.monitor import MonitorManagementClient
    from datetime import datetime, timezone

    cred = _credential()
    # Extract subscription ID from the resource ID
    parts = resource_id.split("/")
    sub_id = parts[2] if len(parts) > 2 else _subscription_id()
    client = MonitorManagementClient(cred, sub_id)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    timespan = f"{start.isoformat()}/{end.isoformat()}"

    response = client.metrics.list(
        resource_uri=resource_id,
        metricnames=metric,
        timespan=timespan,
        interval=timedelta(hours=1),
        aggregation="Average",
    )

    values = []
    for m in response.value:
        for ts in m.timeseries:
            for dp in ts.data:
                if dp.average is not None:
                    values.append(dp.average)

    if not values:
        return {"metric": metric, "avg": None, "max": None, "min": None, "hours": hours}

    return {
        "metric": metric,
        "avg": round(sum(values) / len(values), 2),
        "max": round(max(values), 2),
        "min": round(min(values), 2),
        "hours": hours,
        "data_points": len(values),
    }
