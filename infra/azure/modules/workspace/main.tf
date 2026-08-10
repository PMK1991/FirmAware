resource "azurerm_machine_learning_workspace" "this" {
  name                = "mlw-${var.name_prefix}"
  location            = var.location
  resource_group_name = var.resource_group_name

  application_insights_id = var.application_insights_id
  key_vault_id            = var.key_vault_id
  storage_account_id      = var.storage_account_id
  container_registry_id   = var.container_registry_id

  # User-assigned rather than system-assigned so the role assignments in the
  # identity module can exist before the workspace does, instead of a chicken
  # and egg between "create workspace" and "grant workspace access to storage".
  identity {
    type         = "UserAssigned"
    identity_ids = [var.workspace_identity_id]
  }

  primary_user_assigned_identity = var.workspace_identity_id

  # Suppresses Azure's own diagnostic capture of data that may contain the
  # payload. On in prod, off in dev where the ability to read a failure trace is
  # worth more than the suppression on synthetic data.
  high_business_impact = var.env == "prod"

  public_network_access_enabled = !var.network_isolation

  # Managed VNet isolation with enumerated outbound rules, rather than blanket
  # egress. Only meaningful alongside private endpoints, so it follows the same
  # switch.
  managed_network {
    isolation_mode = var.network_isolation ? "AllowOnlyApprovedOutbound" : "Disabled"
  }

  tags = var.tags
}

# Scale-to-zero is the reason training costs nothing between runs: min_node_count
# is 0 and the cluster releases its nodes after five idle minutes.
resource "azurerm_machine_learning_compute_cluster" "this" {
  name                          = "firmaware-cluster"
  location                      = var.location
  machine_learning_workspace_id = azurerm_machine_learning_workspace.this.id
  vm_priority                   = "Dedicated"
  vm_size                       = var.compute_vm_size
  subnet_resource_id            = var.compute_subnet_id

  scale_settings {
    min_node_count                       = 0
    max_node_count                       = var.compute_max_nodes
    scale_down_nodes_after_idle_duration = "PT5M"
  }

  # No ssh_public_access_enabled block and no admin credentials: nothing logs
  # into a training node interactively.
  identity {
    type         = "UserAssigned"
    identity_ids = [var.workspace_identity_id]
  }

  tags = var.tags
}

# Datastores pointing at the containers this deployment created, authenticating
# with the workspace identity. Registering them here means a job refers to
# azureml://datastores/... and never to an account key or a raw URL.
resource "azurerm_machine_learning_datastore_datalake_gen2" "this" {
  for_each = var.container_ids

  name                 = "firmaware_${each.key}"
  workspace_id         = azurerm_machine_learning_workspace.this.id
  storage_container_id = each.value
}

resource "azurerm_monitor_diagnostic_setting" "workspace" {
  name                       = "diag-mlw"
  target_resource_id         = azurerm_machine_learning_workspace.this.id
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category_group = "allLogs"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
