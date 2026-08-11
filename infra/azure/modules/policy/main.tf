# Compliance as code, assigned at resource-group scope.
#
# These are the controls that survive a mistake in the Terraform above. Every
# storage setting in modules/storage can be reverted by an edit; a Deny policy
# refuses the request no matter who makes it or how. The mapping from each
# assignment to the control it satisfies is in infra/azure/README.md.
#
# Built-in definitions are referenced by their well-known GUIDs. They are stable
# ids, not names, so a rename by Microsoft cannot silently detach an assignment.

locals {
  builtin = {
    # CIS Azure 3.6 / ISO 27001 A.9.4 - anonymous read on blob containers.
    deny_public_blob = "/providers/Microsoft.Authorization/policyDefinitions/4fa4b6c0-31ca-4c0d-b10d-24b96f62a751"
    # CIS Azure 3.1 / ISO 27001 A.10.1 - transport encryption floor.
    audit_tls = "/providers/Microsoft.Authorization/policyDefinitions/fe83a0eb-a853-422d-aac2-1bffd182c5d0"
    # CIS Azure 5.x - registry admin user is a shared credential.
    deny_acr_admin = "/providers/Microsoft.Authorization/policyDefinitions/dc921057-6b28-4fbe-9b83-f7bec05db6c2"
    # Attribution: a resource without these cannot be costed or owned.
    require_tag = "/providers/Microsoft.Authorization/policyDefinitions/871b6d14-10aa-478d-b590-94f262ecfa99"
  }
}

resource "azurerm_resource_group_policy_assignment" "deny_public_blob" {
  name                 = substr("${var.name_prefix}-no-public-blob", 0, 24)
  display_name         = "Deny public blob access (${var.name_prefix})"
  description          = "Storage accounts must not allow anonymous container access."
  resource_group_id    = var.resource_group_id
  policy_definition_id = local.builtin.deny_public_blob
  location             = var.location

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_resource_group_policy_assignment" "audit_tls" {
  name                 = substr("${var.name_prefix}-tls12", 0, 24)
  display_name         = "Audit TLS below 1.2 (${var.name_prefix})"
  description          = "Storage accounts must require TLS 1.2 or higher."
  resource_group_id    = var.resource_group_id
  policy_definition_id = local.builtin.audit_tls
  location             = var.location

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_resource_group_policy_assignment" "deny_acr_admin" {
  name                 = substr("${var.name_prefix}-no-acr-admin", 0, 24)
  display_name         = "Deny registry admin user (${var.name_prefix})"
  description          = "The registry admin account is a shared username and password; identity-based pull replaces it."
  resource_group_id    = var.resource_group_id
  policy_definition_id = local.builtin.deny_acr_admin
  location             = var.location

  identity {
    type = "SystemAssigned"
  }
}

# One assignment per mandatory tag: the built-in definition takes a single tag
# name, and six separate results say which tag is missing rather than only that
# something is.
resource "azurerm_resource_group_policy_assignment" "require_tag" {
  for_each = toset(var.required_tags)

  name                 = substr("${var.name_prefix}-tag-${each.key}", 0, 24)
  display_name         = "Require tag ${each.key} (${var.name_prefix})"
  description          = "Every resource carries the six mandatory tags."
  resource_group_id    = var.resource_group_id
  policy_definition_id = local.builtin.require_tag
  location             = var.location

  parameters = jsonencode({
    tagName = { value = each.key }
  })

  identity {
    type = "SystemAssigned"
  }
}
