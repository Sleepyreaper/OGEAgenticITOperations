"""Demo scenarios — pre-built data and queries to showcase the agent council.

These provide realistic Azure telemetry data for demo purposes while
the real data integrations are being connected. In production, this
data comes from azure_data.py queries.
"""

import json

# ─── Scenario 1: "Why is this VM so big?" ────────────────────────

VM_SIZING_DATA = json.dumps({
    "resource": {
        "name": "dte-scada-batch-vm01",
        "type": "Microsoft.Compute/virtualMachines",
        "resourceGroup": "SAP-Production-RG",
        "location": "eastus2",
        "sku": "Standard_D16s_v5",
        "tags": {
            "support-owner": "sap-ops-team@dtenergy.com",
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

VM_SIZING_QUESTION = "Why is dte-scada-batch-vm01 running on a D16s_v5? It seems oversized. Can we save money here?"


# ─── Scenario 2: "My deployment failed" ──────────────────────────

DEPLOYMENT_FAILURE_DATA = json.dumps({
    "deployment": {
        "name": "terraform-apply-20260311-1430",
        "resourceGroup": "WebApp-Test-RG",
        "status": "Failed",
        "timestamp": "2026-03-11T14:32:18Z",
        "initiatedBy": "svc-terraform-pipeline@dtenergy.com"
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
        {"name": "migration-temp-disk01", "resourceGroup": "Legacy-Migration-RG", "sizeGB": 512, "sku": "Premium_LRS", "monthly_cost": 73.22, "unattached_days": 45, "tags": {"support-owner": "cloud-ops@dtenergy.com"}},
        {"name": "dev-backup-osdisk", "resourceGroup": "Dev-Sandbox-RG", "sizeGB": 256, "sku": "Premium_LRS", "monthly_cost": 38.11, "unattached_days": 90, "tags": {"support-owner": "dev-team-alpha@dtenergy.com"}},
        {"name": "old-sql-data-disk", "resourceGroup": "Data-Prod-RG", "sizeGB": 1024, "sku": "Premium_LRS", "monthly_cost": 135.17, "unattached_days": 30, "tags": {"support-owner": "data-platform@dtenergy.com"}}
    ],
    "underutilized_app_service_plans": [
        {"name": "api-staging-plan", "resourceGroup": "API-Staging-RG", "sku": "P2v3", "monthly_cost": 292.00, "cpu_avg_7d": 3.2, "memory_avg_7d": 18.5, "apps_hosted": 1, "tags": {"support-owner": "api-team@dtenergy.com"}},
        {"name": "internal-tools-plan", "resourceGroup": "InternalTools-RG", "sku": "P1v3", "monthly_cost": 146.00, "cpu_avg_7d": 1.1, "memory_avg_7d": 8.3, "apps_hosted": 2, "tags": {"support-owner": "platform-eng@dtenergy.com"}}
    ],
    "idle_application_gateway": {
        "name": "dr-appgw-eastus2", "resourceGroup": "DR-Infrastructure-RG", "sku": "WAF_v2", "monthly_cost": 350.00, "requests_7d": 0, "backend_health": "Healthy",
        "tags": {"support-owner": "cloud-ops@dtenergy.com", "purpose": "disaster-recovery", "dr-tier": "tier-1"}
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
        "name": "{prefix}-log",
        "type": "Microsoft.OperationalInsights/workspaces",
        "resourceGroup": "{PREFIX}_RG",
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
        "support-owner": "cloud-ops@dtenergy.com",
        "environment": "production"
    }
}, indent=2)

SCOUT_ALERT_QUESTION = "Run a proactive environment scan. What issues should we know about right now?"


# ─── Scenario 5: Environment overview ────────────────────────────

ENVIRONMENT_OVERVIEW_DATA = json.dumps({
    "subscription": {
        "name": "CloudOps-Production",
        "id": "00000000-0000-0000-0000-000000000000",
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


# ─── Scenario 6: Continuous compliance ────────────────────────────

COMPLIANCE_DATA = json.dumps({
    "policy_compliance_summary": {
        "total_policies": 47,
        "non_compliant_policies": 8,
        "non_compliant_resources": 14,
        "compliant_resources": 833,
        "total_resources": 847,
        "compliance_pct": 98.3,
        "scan_timestamp": "2026-03-17T06:00:00Z"
    },
    "non_compliant_resources": [
        {
            "resourceName": "stlegacyexport01",
            "resourceType": "Microsoft.Storage/storageAccounts",
            "resourceGroup": "Legacy-Migration-RG",
            "policyDefinitionName": "Secure transfer to storage accounts should be enabled",
            "policyDefinitionAction": "Audit",
            "complianceState": "NonCompliant",
            "tags": {"support-owner": "data-platform@dtenergy.com", "environment": "production", "migration-phase": "3"},
            "detail": "HTTPS-only transfer is disabled. Account uses HTTP for legacy ETL pipeline from on-prem SFTP gateway.",
            "created": "2024-06-15",
            "last_policy_eval": "2026-03-17T05:42:00Z"
        },
        {
            "resourceName": "kv-sandbox-dev",
            "resourceType": "Microsoft.KeyVault/vaults",
            "resourceGroup": "Dev-Sandbox-RG",
            "policyDefinitionName": "Key Vault should use a virtual network service endpoint",
            "policyDefinitionAction": "Audit",
            "complianceState": "NonCompliant",
            "tags": {"support-owner": "dev-team-alpha@dtenergy.com", "environment": "development"},
            "detail": "Key Vault has no VNet service endpoint. Dev teams use public endpoint for local development.",
            "created": "2025-11-02",
            "last_policy_eval": "2026-03-17T05:42:00Z"
        },
        {
            "resourceName": "sql-analytics-prod",
            "resourceType": "Microsoft.Sql/servers",
            "resourceGroup": "Data-Prod-RG",
            "policyDefinitionName": "Azure SQL Database should have Azure Active Directory Only Authentication",
            "policyDefinitionAction": "Audit",
            "complianceState": "NonCompliant",
            "tags": {"support-owner": "data-platform@dtenergy.com", "environment": "production", "application": "analytics-dwh"},
            "detail": "SQL Auth is enabled alongside AAD. Legacy analytics ETL uses SQL auth with a service account password rotated quarterly via Key Vault.",
            "created": "2023-09-10",
            "last_policy_eval": "2026-03-17T05:42:00Z"
        },
        {
            "resourceName": "aks-microservices-01",
            "resourceType": "Microsoft.ContainerService/managedClusters",
            "resourceGroup": "Microservices-Prod-RG",
            "policyDefinitionName": "Azure Kubernetes Service Clusters should have local authentication methods disabled",
            "policyDefinitionAction": "Audit",
            "complianceState": "NonCompliant",
            "tags": {"support-owner": "platform-eng@dtenergy.com", "environment": "production", "application": "microservices"},
            "detail": "Local accounts still enabled. Team filed exemption request 6 months ago citing CI/CD pipeline dependency. Exemption expired last month.",
            "created": "2024-03-20",
            "last_policy_eval": "2026-03-17T05:42:00Z"
        },
        {
            "resourceName": "func-etl-legacy",
            "resourceType": "Microsoft.Web/sites",
            "resourceGroup": "Legacy-Migration-RG",
            "policyDefinitionName": "Function apps should use the latest TLS version",
            "policyDefinitionAction": "Audit",
            "complianceState": "NonCompliant",
            "tags": {"support-owner": "cloud-ops@dtenergy.com", "environment": "production"},
            "detail": "Running TLS 1.0. Function app is stopped (runtime deprecated) but still exists as a resource. Migration to new function app was completed Q4 2025 but old resource never deleted.",
            "created": "2022-08-01",
            "last_policy_eval": "2026-03-17T05:42:00Z"
        }
    ],
    "policy_definitions_with_issues": [
        {
            "policy_name": "Secure transfer to storage accounts should be enabled",
            "policy_type": "BuiltIn",
            "known_gap": "Does not account for storage accounts acting as SFTP gateway endpoints for on-prem integration. The org has 3 storage accounts in this pattern.",
            "suggested_fix": "Create custom policy that exempts storage accounts tagged 'sftp-gateway: true' OR create a policy exemption for the specific resources."
        },
        {
            "policy_name": "Key Vault should use a virtual network service endpoint",
            "policy_type": "BuiltIn",
            "known_gap": "Policy is correct for production. For development Key Vaults used by developers on corporate VPN, enforcing VNet endpoints breaks local dev workflow. The policy definition doesn't distinguish by environment tag.",
            "suggested_fix": "Create custom policy: 'Key Vaults tagged environment=production MUST have VNet service endpoint. Key Vaults tagged environment=development SHOULD have VNet endpoint but are exempt if tagged dev-access-pattern=local.'"
        }
    ],
    "workaround_patterns": [
        {
            "pattern": "AKS local auth exemption expired",
            "resource": "aks-microservices-01",
            "risk": "High — local auth on a production AKS cluster is a credential theft vector. The CI/CD pipeline should have been migrated to workload identity when the exemption was granted.",
            "recommendation": "PBI: Migrate CI/CD to workload identity federation. Priority: High. Sprint: current+1."
        },
        {
            "pattern": "SQL Auth enabled alongside AAD on production database",
            "resource": "sql-analytics-prod",
            "risk": "Medium — SQL auth is a weaker auth pattern but the password rotation via KV mitigates immediate risk. However, this is a documented workaround that's been in place for 2.5 years.",
            "recommendation": "PBI: Migrate analytics ETL to managed identity auth. Priority: Medium. Timeline: 90 days."
        }
    ],
    "exemption_status": {
        "total_active": 3,
        "recently_expired": 1,
        "expired_detail": {
            "resource": "aks-microservices-01",
            "exemption_name": "AKS-local-auth-ci-cd-pipeline",
            "granted": "2025-09-17",
            "expired": "2026-02-17",
            "reason": "CI/CD pipeline dependency on kubeconfig with local credentials. Migration to workload identity planned for Q1 2026.",
            "status": "EXPIRED — migration not completed"
        }
    }
}, indent=2)

COMPLIANCE_QUESTION = "Run a compliance scan. We need to know what's non-compliant, whether our policies are right, and if anyone is abusing exemptions."


# ─── Registry ────────────────────────────────────────────────────

DEMO_SCENARIOS = {
    "vm_sizing": {
        "title": "Why is this VM so big?",
        "subtitle": "⚡ Meter Reader vs 🔌 The Lineman — the grid team debates sizing",
        "icon": "server",
        "question": VM_SIZING_QUESTION,
        "data": VM_SIZING_DATA,
        "agents": ["cost_sentinel", "standards_architect"],
    },
    "deployment_failure": {
        "title": "My deployment failed",
        "subtitle": "🌑 Blackout diagnoses failures without elevated access",
        "icon": "alert-triangle",
        "question": DEPLOYMENT_FAILURE_QUESTION,
        "data": DEPLOYMENT_FAILURE_DATA,
        "agents": ["diagnostics_sre"],
    },
    "waste_analysis": {
        "title": "Where are we wasting money?",
        "subtitle": "⚡ Meter Reader finds waste, 🔌 The Lineman defends spending",
        "icon": "dollar-sign",
        "question": WASTE_QUESTION,
        "data": WASTE_ANALYSIS_DATA,
        "agents": ["cost_sentinel", "standards_architect"],
    },
    "scout_alert": {
        "title": "⚠️ Light the Arc Flash",
        "subtitle": "Arc Flash scans for issues before they become incidents",
        "icon": "radar",
        "question": SCOUT_ALERT_QUESTION,
        "data": SCOUT_ALERT_DATA,
        "agents": ["scout"],
    },
    "environment_overview": {
        "title": "Full environment health check",
        "subtitle": "All crew members analyze health, cost, compliance, and risks",
        "icon": "activity",
        "question": OVERVIEW_QUESTION,
        "data": ENVIRONMENT_OVERVIEW_DATA,
        "agents": ["cost_sentinel", "standards_architect", "diagnostics_sre", "scout"],
    },
    "continuous_compliance": {
        "title": "📊 Compliance inspection",
        "subtitle": "The Regulator classifies violations — policy bugs vs workaround abuse",
        "icon": "clipboard-check",
        "question": COMPLIANCE_QUESTION,
        "data": COMPLIANCE_DATA,
        "agents": ["compliance_inspector", "standards_architect", "cost_sentinel"],
    },
}
