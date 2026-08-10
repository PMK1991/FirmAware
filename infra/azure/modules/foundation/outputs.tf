output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "resource_group_id" {
  value = azurerm_resource_group.this.id
}

output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.this.id
}

output "container_registry_id" {
  value = azurerm_container_registry.this.id
}

output "container_registry_name" {
  value = azurerm_container_registry.this.name
}

output "container_registry_login_server" {
  value = azurerm_container_registry.this.login_server
}

output "key_vault_id" {
  value = azurerm_key_vault.this.id
}

output "key_vault_name" {
  description = "security_check.sh needs the name, not the id, to query soft-delete and purge protection."
  value       = azurerm_key_vault.this.name
}

output "application_insights_id" {
  value = azurerm_application_insights.this.id
}
