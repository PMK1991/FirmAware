output "foundry_endpoint" {
  description = "Base URL for the OpenAI-compatible routes (/openai/v1/chat/completions, /openai/v1/embeddings)."
  value       = "https://${azurerm_cognitive_account.foundry.custom_subdomain_name}.openai.azure.com"
}

output "search_endpoint" {
  value = "https://${azurerm_search_service.this.name}.search.windows.net"
}

output "foundry_account_id" {
  value = azurerm_cognitive_account.foundry.id
}

output "search_service_id" {
  value = azurerm_search_service.this.id
}
