output "account_id" {
  description = "Foundry (AIServices) account resource ID."
  value       = local.account_id
}

output "project_endpoint" {
  description = "Set as FOUNDRY_PROJECT_ENDPOINT on the web app (with AGENT_BACKEND=foundry)."
  value       = local.project_endpoint
}

output "app_settings" {
  description = "App settings to apply to the web app to switch the runtime squad onto Foundry."
  value = {
    AGENT_BACKEND            = "foundry"
    FOUNDRY_PROJECT_ENDPOINT = local.project_endpoint
  }
}
