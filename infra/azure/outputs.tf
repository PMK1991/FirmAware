output "resource_group_name" {
  value = module.foundation.resource_group_name
}

output "container_registry_login_server" {
  value = module.foundation.container_registry_login_server
}

output "container_registry_name" {
  value = module.foundation.container_registry_name
}

output "storage_account_name" {
  value = module.storage.storage_account_name
}

output "machine_learning_workspace_name" {
  value = module.workspace.workspace_name
}

output "compute_cluster_name" {
  value = module.workspace.compute_cluster_name
}

output "online_endpoint_name" {
  value = module.endpoints.online_endpoint_name
}

output "key_vault_name" {
  value = module.foundation.key_vault_name
}

# The deploy workflows branch on this. Dev keeps the online endpoint off because
# managed endpoints cannot scale to zero, so the deploy/smoke/promote steps must
# be skipped rather than fail against an endpoint that was never created.
output "online_endpoint_enabled" {
  value = tostring(var.online_endpoint_enabled)
}

output "online_instance_type" {
  description = "Read by deploy_endpoint.sh so deployment sizing is env-scoped, not hardcoded in YAML."
  value       = var.online_instance_type
}

output "online_min_instances" {
  description = "Read by deploy_endpoint.sh. Exported so prod cannot silently inherit the shell default of 0."
  value       = tostring(var.online_min_instances)
}

output "batch_endpoint_name" {
  value = module.endpoints.batch_endpoint_name
}

output "endpoint_identity_resource_id" {
  value = module.identity.endpoint_identity_id
}

output "workspace_identity_client_id" {
  value = module.identity.workspace_identity_client_id
}

output "cicd_identity_client_id" {
  description = "Set as AZURE_CLIENT_ID in GitHub; there is no secret to pair it with."
  value       = module.identity.cicd_identity_client_id
}

output "cicd_identity_principal_id" {
  description = <<-EOT
    Read by security_check.sh control 4. The CI identity lives in the shared
    state resource group, so an `az identity list -g <env-rg>` would not find it
    and the most privileged principal would go unchecked. Sourced from Terraform
    rather than Microsoft Graph because the deploy identity cannot read Graph.
  EOT
  value       = module.identity.cicd_identity_principal_id
}

# The URIs the application is configured with. Emitting them here means the
# deploy workflow never hand-assembles a path that could drift from the storage
# module's naming.
output "data_uri" {
  value = module.storage.data_uri
}

output "artifacts_uri" {
  value = module.storage.artifacts_uri
}

output "scores_uri" {
  value = module.storage.scores_uri
}
