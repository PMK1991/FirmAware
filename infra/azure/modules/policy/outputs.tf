output "assignment_ids" {
  description = "Policy assignment ids, for the compliance mapping in the README."
  value = merge(
    {
      deny_public_blob = azurerm_resource_group_policy_assignment.deny_public_blob.id
      audit_tls        = azurerm_resource_group_policy_assignment.audit_tls.id
      deny_acr_admin   = azurerm_resource_group_policy_assignment.deny_acr_admin.id
    },
    {
      for tag, assignment in azurerm_resource_group_policy_assignment.require_tag :
      "require_tag_${tag}" => assignment.id
    }
  )
}
