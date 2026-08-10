output "storage_account_id" {
  value = azurerm_storage_account.this.id
}

# The workspace's system storage, kept separate because AML refuses an HNS
# account as its default. Nothing in the pipeline addresses it directly.
output "workspace_storage_account_id" {
  value = azurerm_storage_account.workspace.id
}

output "workspace_storage_account_name" {
  value = azurerm_storage_account.workspace.name
}

output "storage_account_name" {
  value = azurerm_storage_account.this.name
}

output "container_ids" {
  description = "Container resource ids, keyed by name, for scoped role assignments."
  value = {
    for name, container in azurerm_storage_container.this : name => container.id
  }
}

# abfss:// URIs assembled here rather than in the deploy workflow, so the
# application is always configured with a path this module actually created.
output "data_uri" {
  value = local.container_uris["data"]
}

output "artifacts_uri" {
  value = local.container_uris["artifacts"]
}

output "scores_uri" {
  value = local.container_uris["scores"]
}

output "collected_uri" {
  value = local.container_uris["collected"]
}
