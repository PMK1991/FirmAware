output "app_name" {
  value = azurerm_container_app.this.name
}

output "app_id" {
  value = azurerm_container_app.this.id
}

output "environment_id" {
  value = azurerm_container_app_environment.this.id
}

output "fqdn" {
  description = "The page's stable hostname. Revision-specific hostnames are derived from it by deploy_app.sh, which smoke tests a revision before it takes traffic."
  value       = azurerm_container_app.this.ingress[0].fqdn
}

output "url" {
  value = "https://${azurerm_container_app.this.ingress[0].fqdn}"
}
