output "workspace_id" {
  value = azurerm_machine_learning_workspace.this.id
}

output "workspace_name" {
  value = azurerm_machine_learning_workspace.this.name
}

output "compute_cluster_name" {
  value = azurerm_machine_learning_compute_cluster.this.name
}

output "application_insights_id" {
  description = "Passed through so consumers of this module keep a single source for the workspace's telemetry target, even though foundation now owns the resource."
  value       = var.application_insights_id
}
