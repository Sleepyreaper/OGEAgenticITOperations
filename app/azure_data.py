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
        state = sub.state
        if hasattr(state, 'value'):
            state = state.value
        subs.append({
            "id": sub.subscription_id,
            "name": sub.display_name,
            "state": str(state) if state else "Unknown",
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


def get_resource_health(subscription_id: str = None,
                       subscription_ids: list[str] = None) -> list[dict]:
    return query_resource_graph(
        "HealthResources | where type =~ 'microsoft.resourcehealth/availabilitystatuses' "
        "| project name=properties.targetResourceName, status=properties.availabilityState, "
        "resourceGroup, summary=properties.summary",
        subscription_id, subscription_ids,
    )


def get_orphaned_disks(subscription_id: str = None,
                      subscription_ids: list[str] = None) -> list[dict]:
    return query_resource_graph(
        "Resources | where type =~ 'Microsoft.Compute/disks' "
        "| where isempty(managedBy) "
        "| project name, resourceGroup, location, sku.name, properties.diskSizeGB, tags, subscriptionId",
        subscription_id, subscription_ids,
    )


def get_public_endpoints(subscription_id: str = None,
                        subscription_ids: list[str] = None) -> list[dict]:
    return query_resource_graph(
        "Resources | where type =~ 'Microsoft.Network/publicIPAddresses' "
        "| project name, resourceGroup, ipAddress=properties.ipAddress, "
        "allocation=properties.publicIPAllocationMethod, associated=properties.ipConfiguration.id, subscriptionId",
        subscription_id, subscription_ids,
    )


def get_tagging_compliance(subscription_id: str = None,
                          subscription_ids: list[str] = None) -> list[dict]:
    return query_resource_graph(
        "ResourceContainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups' "
        "| extend supportOwner = tags['support-owner'] "
        "| project name, supportOwner, location, tags, subscriptionId",
        subscription_id, subscription_ids,
    )


# ─── Deep Intelligence (things Advisor can't do) ────────────────

def get_deep_analysis(subscription_id: str = None,
                      subscription_ids: list[str] = None) -> dict:
    """Cross-resource correlation analysis — connects dots across the entire environment.
    This is what makes us better than Advisor."""
    subs = subscription_ids or [subscription_id or _subscription_id()]

    # Idle App Service Plans (paying for compute with no apps)
    idle_plans = query_resource_graph(
        "Resources | where type =~ 'Microsoft.Web/serverfarms' "
        "| project name, resourceGroup, sku=sku.name, tier=sku.tier, "
        "numberOfSites=properties.numberOfSites",
        subscription_ids=subs,
    )
    idle_plans = [p for p in idle_plans if p.get("numberOfSites", 0) == 0]

    # Orphaned NSGs (attached to nothing)
    all_nsgs = query_resource_graph(
        "Resources | where type =~ 'Microsoft.Network/networkSecurityGroups' "
        "| project name, resourceGroup, subnets=properties.subnets, nics=properties.networkInterfaces",
        subscription_ids=subs,
    )
    orphaned_nsgs = [n for n in all_nsgs if not n.get("subnets") and not n.get("nics")]

    # VNets with empty subnets (allocated address space nobody's using)
    vnets = query_resource_graph(
        "Resources | where type =~ 'Microsoft.Network/virtualNetworks' "
        "| mvexpand subnet=properties.subnets "
        "| extend subnetName=tostring(subnet.name), ipConfigs=subnet.properties.ipConfigurations "
        "| project vnetName=name, subnetName, resourceGroup, "
        "addressPrefix=tostring(subnet.properties.addressPrefix), "
        "connectedDevices=array_length(subnet.properties.ipConfigurations), "
        "delegations=array_length(subnet.properties.delegations)",
        subscription_ids=subs,
    )
    empty_subnets = [v for v in vnets if (v.get("connectedDevices") or 0) == 0 and (v.get("delegations") or 0) == 0]

    # Resources with no diagnostic settings (monitoring blind spots)
    # We check by looking at resources that SHOULD have diag settings
    monitored_types = query_resource_graph(
        "Resources | where type in~ ('Microsoft.Compute/virtualMachines', "
        "'Microsoft.Web/sites', 'Microsoft.Sql/servers', "
        "'Microsoft.KeyVault/vaults', 'Microsoft.Storage/storageAccounts', "
        "'Microsoft.Network/applicationGateways', 'Microsoft.ContainerService/managedClusters', "
        "'Microsoft.DBforPostgreSQL/flexibleServers') "
        "| project name, type, resourceGroup, location",
        subscription_ids=subs,
    )

    # Recovery Vaults with nothing protected
    recovery_vaults = query_resource_graph(
        "Resources | where type =~ 'Microsoft.RecoveryServices/vaults' "
        "| project name, resourceGroup, location",
        subscription_ids=subs,
    )

    # Architecture ratios (smell detection)
    type_counts = {}
    all_resources = query_resource_graph(
        "Resources | summarize count() by type | order by count_ desc | take 20", subscription_ids=subs,
    )
    for r in all_resources:
        type_counts[r.get("type", "")] = r.get("count_", 0)

    nsg_count = type_counts.get("microsoft.network/networksecuritygroups", 0)
    vnet_count = type_counts.get("microsoft.network/virtualnetworks", 0)
    nic_count = type_counts.get("microsoft.network/networkinterfaces", 0)
    vm_count = type_counts.get("microsoft.compute/virtualmachines", 0)
    pe_count = type_counts.get("microsoft.network/privateendpoints", 0)
    disk_count = type_counts.get("microsoft.compute/disks", 0)

    architecture_smells = []
    if vnet_count > 0 and nsg_count / vnet_count > 3:
        architecture_smells.append(f"NSG sprawl: {nsg_count} NSGs for {vnet_count} VNets ({nsg_count/vnet_count:.1f}:1 ratio) — likely orphaned or over-segmented")
    if vm_count > 0 and disk_count / vm_count > 3:
        architecture_smells.append(f"Disk sprawl: {disk_count} disks for {vm_count} VMs ({disk_count/vm_count:.1f}:1 ratio) — check for orphaned data disks")
    if vm_count > 0 and nic_count / vm_count > 2:
        architecture_smells.append(f"NIC sprawl: {nic_count} NICs for {vm_count} VMs ({nic_count/vm_count:.1f}:1 ratio) — possible orphaned NICs from deleted VMs")
    if pe_count > 0 and pe_count > vnet_count * 3:
        architecture_smells.append(f"Private endpoint density: {pe_count} PEs across {vnet_count} VNets — verify subnet capacity planning")

    # Blast radius — map VNet dependencies
    vnet_resources = query_resource_graph(
        "Resources | where type =~ 'Microsoft.Network/virtualNetworks' "
        "| project vnetName=name, resourceGroup, "
        "peerings=array_length(properties.virtualNetworkPeerings), "
        "subnets=array_length(properties.subnets)",
        subscription_ids=subs,
    )

    return {
        "idle_app_service_plans": idle_plans,
        "orphaned_nsgs": orphaned_nsgs,
        "empty_subnets": empty_subnets,
        "monitorable_resources": len(monitored_types),
        "recovery_vaults": recovery_vaults,
        "architecture_smells": architecture_smells,
        "type_counts": type_counts,
        "vnet_topology": vnet_resources,
    }


# ─── Azure Advisor (platform-verified recommendations) ──────────

def get_advisor_recommendations(subscription_id: str = None) -> list[dict]:
    """Pull Azure Advisor recommendations — these are platform-verified, not AI guesses."""
    from azure.mgmt.advisor import AdvisorManagementClient
    cred = _credential()
    sub = subscription_id or _subscription_id()
    client = AdvisorManagementClient(cred, sub)
    recs = []
    for r in client.recommendations.list():
        recs.append({
            "category": r.category,
            "impact": r.impact,
            "problem": r.short_description.problem if r.short_description else "",
            "solution": r.short_description.solution if r.short_description else "",
            "resource": r.resource_metadata.resource_id if r.resource_metadata else "",
        })
    return recs


# ─── Security Drift Detection (near-real-time via Resource Graph) ─

def detect_security_drift(subscription_id: str = None,
                         subscription_ids: list[str] = None) -> list[dict]:
    """Find open inbound NSG rules allowing traffic from any source (*) on dangerous ports."""
    results = query_resource_graph(
        "Resources | where type =~ 'Microsoft.Network/networkSecurityGroups' "
        "| mvexpand rules=properties.securityRules "
        "| where rules.properties.access == 'Allow' "
        "and rules.properties.direction == 'Inbound' "
        "and rules.properties.sourceAddressPrefix == '*' "
        "and rules.properties.destinationPortRange in ('22', '3389', '445', '1433', '3306', '5432', '*') "
        "| project nsgName=name, ruleName=tostring(rules.name), "
        "port=tostring(rules.properties.destinationPortRange), "
        "priority=toint(rules.properties.priority), resourceGroup, subscriptionId",
        subscription_id, subscription_ids,
    )
    return results


def detect_insecure_storage(subscription_id: str = None,
                           subscription_ids: list[str] = None) -> list[dict]:
    """Find storage accounts with public blob access enabled."""
    return query_resource_graph(
        "Resources | where type =~ 'Microsoft.Storage/storageAccounts' "
        "| where properties.allowBlobPublicAccess == true "
        "| project name, resourceGroup, location, publicAccess=properties.allowBlobPublicAccess, subscriptionId",
        subscription_id, subscription_ids,
    )


# ─── Azure Service Health & Resource Health ──────────────────────

def get_resource_health_statuses(subscription_id: str = None) -> list[dict]:
    """Get health status for all resources via Resource Health API."""
    import requests
    cred = _credential()
    sub = subscription_id or _subscription_id()
    token = cred.get_token("https://management.azure.com/.default").token
    url = f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.ResourceHealth/availabilityStatuses?api-version=2023-07-01-preview"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        return []
    data = resp.json().get("value", [])
    results = []
    for item in data:
        rid = item.get("id", "")
        props = item.get("properties", {})
        # Extract resource name from the ID
        parts = rid.split("/providers/")[0].split("/") if "/providers/" in rid else []
        rg = ""
        rname = ""
        rtype = ""
        if "/providers/" in rid:
            before_health = rid.split("/providers/Microsoft.ResourceHealth")[0]
            segments = before_health.split("/")
            for i, s in enumerate(segments):
                if s.lower() == "resourcegroups" and i + 1 < len(segments):
                    rg = segments[i + 1]
                if s.lower() == "providers" and i + 2 < len(segments):
                    rtype = f"{segments[i+1]}/{segments[i+2]}"
                    if i + 3 < len(segments):
                        rname = segments[i + 3]

        results.append({
            "name": rname,
            "resourceGroup": rg,
            "type": rtype,
            "status": props.get("availabilityState", "Unknown"),
            "summary": props.get("summary", ""),
            "title": props.get("title", ""),
            "location": item.get("location", ""),
        })
    return results


def get_service_health_events(subscription_id: str = None, days: int = 30) -> list[dict]:
    """Get Azure Service Health events affecting this subscription."""
    import requests
    from datetime import datetime, timezone, timedelta
    cred = _credential()
    sub = subscription_id or _subscription_id()
    token = cred.get_token("https://management.azure.com/.default").token
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    url = f"https://management.azure.com/subscriptions/{sub}/providers/Microsoft.ResourceHealth/events?api-version=2024-02-01&queryStartTime={start}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        return []
    events = []
    for item in resp.json().get("value", []):
        props = item.get("properties", {})
        impacted_services = []
        impacted_regions = []
        for impact in props.get("impact", []):
            impacted_services.append(impact.get("impactedService", ""))
            for region in impact.get("impactedRegions", []):
                impacted_regions.append(region.get("impactedRegion", ""))
        events.append({
            "title": props.get("title", ""),
            "status": props.get("status", ""),
            "level": props.get("level", ""),
            "eventType": props.get("eventType", ""),
            "impactStart": props.get("impactStartTime", ""),
            "services": list(set(impacted_services)),
            "regions": list(set(impacted_regions)),
            "summary": props.get("summary", "")[:300],
        })
    return events


# ─── Chaos / Demo Functions ─────────────────────────────────────

def create_chaos_nsg_rule(resource_group: str = "OGE_Envisioning",
                          nsg_name: str = "ogeops-nsg-pe") -> dict:
    """Create a deliberately bad NSG rule — SSH open to the world."""
    from azure.mgmt.network import NetworkManagementClient
    cred = _credential()
    sub = _subscription_id()
    client = NetworkManagementClient(cred, sub)
    rule = client.security_rules.begin_create_or_update(
        resource_group, nsg_name, "chaos-allow-ssh-from-anywhere",
        {
            "protocol": "Tcp",
            "source_address_prefix": "*",
            "source_port_range": "*",
            "destination_address_prefix": "*",
            "destination_port_range": "22",
            "access": "Allow",
            "direction": "Inbound",
            "priority": 100,
        }
    ).result()
    return {"rule_name": rule.name, "status": "created", "port": "22", "source": "*"}


def cleanup_chaos_nsg_rule(resource_group: str = "OGE_Envisioning",
                            nsg_name: str = "ogeops-nsg-pe") -> dict:
    """Remove the chaos NSG rule."""
    from azure.mgmt.network import NetworkManagementClient
    cred = _credential()
    sub = _subscription_id()
    client = NetworkManagementClient(cred, sub)
    try:
        client.security_rules.begin_delete(
            resource_group, nsg_name, "chaos-allow-ssh-from-anywhere"
        ).result()
        return {"status": "cleaned_up"}
    except Exception:
        return {"status": "already_clean"}


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


# ─── Azure Policy Compliance ────────────────────────────────────

def get_policy_compliance_summary(subscription_id: str = None) -> dict:
    """Get subscription-level policy compliance summary."""
    import requests

    sub = subscription_id or _subscription_id()
    cred = _credential()
    token = cred.get_token("https://management.azure.com/.default").token

    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/providers/Microsoft.PolicyInsights/policyStates/latest/summarize"
        f"?api-version=2019-10-01"
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    summaries = data.get("value", [])
    if not summaries:
        return {"total_policies": 0, "non_compliant_policies": 0, "non_compliant_resources": 0, "details": []}

    summary = summaries[0]
    results = summary.get("results", {})

    policy_details = []
    for pd in summary.get("policyAssignments", [])[:20]:
        pd_results = pd.get("results", {})
        if pd_results.get("nonCompliantResources", 0) > 0:
            policy_details.append({
                "assignmentId": pd.get("policyAssignmentId", ""),
                "assignmentName": pd.get("policyAssignmentId", "").split("/")[-1],
                "nonCompliantResources": pd_results.get("nonCompliantResources", 0),
                "nonCompliantPolicies": pd_results.get("nonCompliantPolicies", 0),
            })

    return {
        "total_policies": results.get("totalPoliciesCount", 0),
        "non_compliant_policies": results.get("nonCompliantPolicies", 0),
        "non_compliant_resources": results.get("nonCompliantResources", 0),
        "compliant_resources": results.get("totalResources", 0) - results.get("nonCompliantResources", 0),
        "total_resources": results.get("totalResources", 0),
        "compliance_pct": round(
            (1 - results.get("nonCompliantResources", 0) / max(results.get("totalResources", 1), 1)) * 100, 1
        ),
        "top_non_compliant_assignments": policy_details,
    }


def get_non_compliant_resources(subscription_id: str = None, top: int = 25) -> list[dict]:
    """Get specific non-compliant resources with policy details."""
    import requests

    sub = subscription_id or _subscription_id()
    cred = _credential()
    token = cred.get_token("https://management.azure.com/.default").token

    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/providers/Microsoft.PolicyInsights/policyStates/latest/queryResults"
        f"?api-version=2019-10-01&$top={top}"
        f"&$filter=complianceState eq 'NonCompliant'"
        f"&$orderby=timestamp desc"
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for record in data.get("value", []):
        results.append({
            "resourceId": record.get("resourceId", ""),
            "resourceName": record.get("resourceId", "").split("/")[-1],
            "resourceType": record.get("resourceType", ""),
            "resourceGroup": record.get("resourceGroup", ""),
            "policyAssignmentName": record.get("policyAssignmentName", ""),
            "policyDefinitionName": record.get("policyDefinitionName", ""),
            "policyDefinitionAction": record.get("policyDefinitionAction", ""),
            "complianceState": record.get("complianceState", ""),
            "timestamp": record.get("timestamp", ""),
        })

    return results
