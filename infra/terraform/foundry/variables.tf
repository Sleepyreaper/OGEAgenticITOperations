variable "subscription_id" {
  description = "Subscription that holds (or will hold) the Foundry account."
  type        = string
}

variable "mode" {
  description = "\"existing\" grants RBAC on an existing Foundry account; \"new\" creates account + project + model deployments + RBAC."
  type        = string
  default     = "existing"
  validation {
    condition     = contains(["existing", "new"], var.mode)
    error_message = "mode must be \"existing\" or \"new\"."
  }
}

variable "resource_group_name" {
  description = "Resource group of the Foundry account (must already exist)."
  type        = string
}

variable "account_name" {
  description = "AIServices account name. For mode = \"new\" it must be globally unique (used as the custom subdomain)."
  type        = string
}

variable "project_name" {
  description = "Foundry project name (created for \"new\"; used to build the endpoint for both modes)."
  type        = string
  default     = "oge-ops"
}

variable "location" {
  description = "Region for a new Foundry account. Pick one with quota for model_deployments."
  type        = string
  default     = "eastus2"
}

variable "public_network_access" {
  description = "Public network access for a new Foundry account."
  type        = string
  default     = "Enabled"
}

variable "model_deployments" {
  description = "Model deployments for mode = \"new\". Map agents onto them with AGENT_<KEY>_DEPLOYMENT app settings."
  type = list(object({
    name     = string
    model    = string
    version  = string
    sku      = string
    capacity = number
  }))
  default = [
    { name = "gpt-5.6-sol", model = "gpt-5.6-sol", version = "2026-07-09", sku = "GlobalStandard", capacity = 100 },
    { name = "gpt-5.6-terra", model = "gpt-5.6-terra", version = "2026-07-09", sku = "GlobalStandard", capacity = 50 },
    { name = "gpt-5.6-luna", model = "gpt-5.6-luna", version = "2026-07-09", sku = "GlobalStandard", capacity = 50 },
  ]
}

variable "app_principal_ids" {
  description = "Principal (object) IDs that need Foundry User on the account — typically the web app's user-assigned managed identity."
  type        = list(string)
  default     = []
}

variable "log_analytics_workspace_id" {
  description = "Optional Log Analytics workspace resource ID for Foundry diagnostic logs (mode = \"new\")."
  type        = string
  default     = ""
}
