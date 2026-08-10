output "workspace_identity_id" {
  value = azurerm_user_assigned_identity.workspace.id
}

output "workspace_identity_client_id" {
  value = azurerm_user_assigned_identity.workspace.client_id
}

output "workspace_identity_principal_id" {
  value = azurerm_user_assigned_identity.workspace.principal_id
}

output "endpoint_identity_id" {
  value = azurerm_user_assigned_identity.endpoint.id
}

output "endpoint_identity_client_id" {
  value = azurerm_user_assigned_identity.endpoint.client_id
}

output "cicd_identity_id" {
  value = data.azurerm_user_assigned_identity.cicd.id
}

output "cicd_identity_client_id" {
  value = data.azurerm_user_assigned_identity.cicd.client_id
}

output "cicd_identity_principal_id" {
  value = data.azurerm_user_assigned_identity.cicd.principal_id
}

output "scores_appender_role_id" {
  value = azurerm_role_definition.scores_appender.role_definition_resource_id
}
