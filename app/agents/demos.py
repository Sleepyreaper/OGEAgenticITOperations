"""Demo scenarios — pre-built data and queries to showcase the agent council.

These provide realistic Azure telemetry data for demo purposes while
the real data integrations are being connected. In production, this
data comes from azure_data.py queries.
"""

import json

# ─── Scenario 1: "Why is this VM so big?" ────────────────────────

VM_SIZING_DATA = json.dumps({
    "resource": {
        "name": "oge-sap-batch-vm01",
        "type": "Microsoft.Compute/virtualMachines",
        "resourceGroup": "SAP-Production-RG",
        "location": "eastus2",
        "sku": "Standard_D16s_v5",
        "tags": {
            "support-owner": "sap-ops-team@OGE.com",
            "environment": "production",
            "application": "SAP-BatchProcessing",
            "cost-center": "CC-4820"
        }
    },
    "metrics_7day": {
        "cpu_avg_pct": 11.8,
        "cpu_max_pct": 94.2,
        "cpu_p95_pct": 18.5,
        "memory_avg_pct": 34.2,
        "memory_max_pct": 88.1,
        "disk_iops_avg": 120,
        "disk_iops_max": 4800,
        "network_out_avg_mbps": 2.1,
        "network_out_max_mbps": 180.5
    },
    "peak_schedule": "Last Friday of each month, 18:00-00:00 UTC (SAP month-end batch)",
    "cost_current_monthly": 985.60,
    "comparable_skus": [
        {"sku": "Standard_D4s_v5", "monthly_cost": 246.40, "vcpus": 4, "memory_gb": 16},
        {"sku": "Standard_D8s_v5", "monthly_cost": 492.80, "vcpus": 8, "memory_gb": 32},
        {"sku": "Standard_B16ms", "monthly_cost": 483.84, "vcpus": 16, "memory_gb": 64, "note": "Burstable"},
        {"sku": "Standard_D16s_v5", "monthly_cost": 985.60, "vcpus": 16, "memory_gb": 64}
    ]
}, indent=2)

VM_SIZING_QUESTION = "Why is oge-sap-batch-vm01 running on a D16s_v5? It seems oversized. Can we save money here?"


# ─── Scenario 2: "My deployment failed" ──────────────────────────

DEPLOYMENT_FAILURE_DATA = json.dumps({
    "deployment": {
        "name": "terraform-apply-20260311-1430",
        "resourceGroup": "WebApp-Test-RG",
        "status": "Failed",
        "timestamp": "2026-03-11T14:32:18Z",
        "initiatedBy": "svc-terraform-pipeline@OGE.com"
    },
    "activity_log_entries": [
        {
            "timestamp": "2026-03-11T14:30:05Z",
            "operation": "Microsoft.Resources/deployments/write",
            "status": "Started",
            "resourceGroup": "WebApp-Test-RG"
        },
        {
            "timestamp": "2026-03-11T14:31:12Z",
            "operation": "Microsoft.Network/networkSecurityGroups/write",
            "status": "Succeeded",
            "resource": "webapp-test-nsg"
        },
        {
            "timestamp": "2026-03-11T14:31:45Z",
            "operation": "Microsoft.Network/virtualNetworks/subnets/write",
            "status": "Failed",
            "resource": "webapp-test-vnet/default",
            "error": {
                "code": "NetcfgInvalidSubnet",
                "message": "Subnet 'default' is in use by /subscriptions/.../providers/Microsoft.Network/virtualNetworks/hub-vnet/virtualNetworkPeerings/hub-to-webapp-test and cannot be modified."
            }
        },
        {
            "timestamp": "2026-03-11T14:32:18Z",
            "operation": "Microsoft.Resources/deployments/write",
            "status": "Failed",
            "resource": "terraform-apply-20260311-1430",
            "error": {
                "code": "DeploymentFailed",
                "message": "At least one resource deployment operation failed."
            }
        }
    ],
    "resource_health": {
        "hub-vnet": "Available",
        "webapp-test-vnet": "Available",
        "webapp-test-nsg": "Available"
    },
    "vnet_peering": {
        "name": "hub-to-webapp-test",
        "status": "Connected",
        "remoteVnet": "hub-vnet",
        "allowForwardedTraffic": True,
        "useRemoteGateways": True
    }
}, indent=2)

DEPLOYMENT_FAILURE_QUESTION = "Our Terraform deployment to WebApp-Test-RG failed. The team says they can't see the logs because they only have Reader access. What happened and how do they fix it?"


# ─── Scenario 3: "Where are we wasting money?" ───────────────────

WASTE_ANALYSIS_DATA = json.dumps({
    "subscription_summary": {
        "total_resources": 847,
        "resource_groups": 64,
        "current_monthly_spend": 42850.00,
        "previous_month_spend": 41200.00,
        "trend": "+4.0%"
    },
    "orphaned_disks": [
        {"name": "migration-temp-disk01", "resourceGroup": "Legacy-Migration-RG", "sizeGB": 512, "sku": "Premium_LRS", "monthly_cost": 73.22, "unattached_days": 45, "tags": {"support-owner": "cloud-ops@OGE.com"}},
        {"name": "dev-backup-osdisk", "resourceGroup": "Dev-Sandbox-RG", "sizeGB": 256, "sku": "Premium_LRS", "monthly_cost": 38.11, "unattached_days": 90, "tags": {"support-owner": "dev-team-alpha@OGE.com"}},
        {"name": "old-sql-data-disk", "resourceGroup": "Data-Prod-RG", "sizeGB": 1024, "sku": "Premium_LRS", "monthly_cost": 135.17, "unattached_days": 30, "tags": {"support-owner": "data-platform@OGE.com"}}
    ],
    "underutilized_app_service_plans": [
        {"name": "api-staging-plan", "resourceGroup": "API-Staging-RG", "sku": "P2v3", "monthly_cost": 292.00, "cpu_avg_7d": 3.2, "memory_avg_7d": 18.5, "apps_hosted": 1, "tags": {"support-owner": "api-team@OGE.com"}},
        {"name": "internal-tools-plan", "resourceGroup": "InternalTools-RG", "sku": "P1v3", "monthly_cost": 146.00, "cpu_avg_7d": 1.1, "memory_avg_7d": 8.3, "apps_hosted": 2, "tags": {"support-owner": "platform-eng@OGE.com"}}
    ],
    "idle_application_gateway": {
        "name": "dr-appgw-eastus2", "resourceGroup": "DR-Infrastructure-RG", "sku": "WAF_v2", "monthly_cost": 350.00, "requests_7d": 0, "backend_health": "Healthy",
        "tags": {"support-owner": "cloud-ops@OGE.com", "purpose": "disaster-recovery", "dr-tier": "tier-1"}
    },
    "unused_public_ips": [
        {"name": "legacy-api-pip", "resourceGroup": "Legacy-Migration-RG", "allocation": "Static", "monthly_cost": 3.65, "associated": None},
        {"name": "test-lb-pip", "resourceGroup": "LoadTest-RG", "allocation": "Static", "monthly_cost": 3.65, "associated": None}
    ]
}, indent=2)

WASTE_QUESTION = "Give me a full waste analysis of our Azure subscription. Where are we bleeding money?"


# ─── Scenario 4: Scout proactive alert ───────────────────────────

SCOUT_ALERT_DATA = json.dumps({
    "alert_type": "quota_pressure",
    "resource": {
        "name": "ogeops-log",
        "type": "Microsoft.OperationalInsights/workspaces",
        "resourceGroup": "OGE_Envisioning",
        "location": "westus2"
    },
    "details": {
        "daily_cap_gb": 5.0,
        "ingested_today_gb": 4.1,
        "hours_remaining": 6,
        "projected_total_gb": 6.8,
        "top_tables": [
            {"table": "AzureActivity", "gb": 1.8},
            {"table": "Perf", "gb": 1.2},
            {"table": "ContainerLog", "gb": 0.7},
            {"table": "SecurityEvent", "gb": 0.4}
        ]
    },
    "resource_group_tags": {
        "support-owner": "cloud-ops@OGE.com",
        "environment": "production"
    }
}, indent=2)

SCOUT_ALERT_QUESTION = "Run a proactive environment scan. What issues should we know about right now?"


# ─── Scenario 5: Environment overview ────────────────────────────

ENVIRONMENT_OVERVIEW_DATA = json.dumps({
    "subscription": {
        "name": "OGE-CloudOps-Production",
        "id": "b1672fa6-8e52-45d0-bf79-ceccc352177d",
        "total_resources": 847,
        "resource_groups": 64
    },
    "health_summary": {
        "healthy": 812,
        "degraded": 8,
        "unavailable": 2,
        "unknown": 25
    },
    "degraded_resources": [
        {"name": "sql-analytics-prod", "type": "Microsoft.Sql/servers", "resourceGroup": "Data-Prod-RG", "status": "Degraded", "summary": "Intermittent connectivity issues detected"},
        {"name": "aks-microservices-01", "type": "Microsoft.ContainerService/managedClusters", "resourceGroup": "Microservices-Prod-RG", "status": "Degraded", "summary": "Node pool scaling limited - 90% node utilization"}
    ],
    "unavailable_resources": [
        {"name": "func-etl-legacy", "type": "Microsoft.Web/sites", "resourceGroup": "Legacy-Migration-RG", "status": "Unavailable", "summary": "Function app stopped - runtime version deprecated"},
        {"name": "redis-cache-staging", "type": "Microsoft.Cache/Redis", "resourceGroup": "API-Staging-RG", "status": "Unavailable", "summary": "Cache eviction rate 100% - maxmemory reached"}
    ],
    "tagging_compliance": {
        "total_resource_groups": 64,
        "with_support_owner": 58,
        "missing_support_owner": 6,
        "compliance_pct": 90.6,
        "non_compliant_rgs": ["Sandbox-Test-RG", "DevExperiment-RG", "TempMigration-RG", "Hackathon2026-RG", "POC-ML-RG", "InternProject-RG"]
    },
    "cost_snapshot": {
        "current_month_to_date": 28450.00,
        "projected_month_end": 42675.00,
        "budget": 45000.00,
        "budget_pct": 94.8,
        "top_cost_centers": [
            {"rg": "Data-Prod-RG", "cost": 8200.00},
            {"rg": "Microservices-Prod-RG", "cost": 5400.00},
            {"rg": "SAP-Production-RG", "cost": 4100.00},
            {"rg": "DR-Infrastructure-RG", "cost": 2800.00},
            {"rg": "API-Staging-RG", "cost": 2100.00}
        ]
    }
}, indent=2)

OVERVIEW_QUESTION = "Give me a full environment health overview. What's the status of our Azure subscription?"


# ─── Registry ────────────────────────────────────────────────────

DEMO_SCENARIOS = {
    "vm_sizing": {
        "title": "Why is this VM so big?",
        "subtitle": "Cost vs. operational requirements — the agents debate sizing decisions",
        "icon": "server",
        "question": VM_SIZING_QUESTION,
        "data": VM_SIZING_DATA,
        "agents": ["cost_sentinel", "standards_architect"],
    },
    "deployment_failure": {
        "title": "My deployment failed",
        "subtitle": "Diagnosing failures without elevated access",
        "icon": "alert-triangle",
        "question": DEPLOYMENT_FAILURE_QUESTION,
        "data": DEPLOYMENT_FAILURE_DATA,
        "agents": ["diagnostics_sre"],
    },
    "waste_analysis": {
        "title": "Where are we wasting money?",
        "subtitle": "Full subscription waste analysis with opposing perspectives",
        "icon": "dollar-sign",
        "question": WASTE_QUESTION,
        "data": WASTE_ANALYSIS_DATA,
        "agents": ["cost_sentinel", "standards_architect"],
    },
    "scout_alert": {
        "title": "Proactive environment scan",
        "subtitle": "Scout detects issues before they become incidents",
        "icon": "radar",
        "question": SCOUT_ALERT_QUESTION,
        "data": SCOUT_ALERT_DATA,
        "agents": ["scout"],
    },
    "environment_overview": {
        "title": "Environment health overview",
        "subtitle": "Full subscription status — health, cost, compliance",
        "icon": "activity",
        "question": OVERVIEW_QUESTION,
        "data": ENVIRONMENT_OVERVIEW_DATA,
        "agents": ["cost_sentinel", "standards_architect", "diagnostics_sre", "scout"],
    },
}
