data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

# Created before anything else: every resource below ships its diagnostics here,
# so the log sink cannot be missing at the moment a resource starts emitting.
resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${var.name_prefix}"
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = var.tags
}

# Lives here rather than beside the workspace that consumes it, because the
# workspace identity must hold Contributor on it *before* the workspace is
# created. Declaring it in the workspace module made that grant unexpressible:
# the identity module would have had to depend on the workspace it is a
# prerequisite for.
resource "azurerm_application_insights" "this" {
  name                = "appi-${var.name_prefix}"
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.this.id
  tags                = var.tags
}

resource "azurerm_container_registry" "this" {
  name                = "cr${replace(var.name_prefix, "-", "")}${var.unique_suffix}"
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = var.registry_sku

  # The registry is pulled from by managed identity only. An admin user would be
  # a username and password pair, which is exactly the key material this design
  # is built to not have.
  admin_enabled                 = false
  anonymous_pull_enabled        = false
  public_network_access_enabled = !var.network_isolation

  # Quarantine holds a pushed image unscanned-and-unusable until it passes, and
  # retention reclaims untagged manifests. Both are Premium-only, so on a Basic
  # dev registry the equivalent gate is the blocking Trivy scan in CI.
  retention_policy_in_days  = var.registry_sku == "Premium" ? 30 : null
  quarantine_policy_enabled = var.registry_sku == "Premium"

  tags = var.tags
}

resource "azurerm_key_vault" "this" {
  name                = "kv-${substr(replace(var.name_prefix, "firmaware", "fa"), 0, 14)}-${var.unique_suffix}"
  location            = var.location
  resource_group_name = azurerm_resource_group.this.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  # RBAC rather than access policies: role assignments are auditable in the same
  # place as every other grant, and there is no second permission model to read.
  rbac_authorization_enabled = true

  purge_protection_enabled      = var.purge_protection
  soft_delete_retention_days    = 90
  public_network_access_enabled = !var.network_isolation

  network_acls {
    # Azure ML reaches the vault over the trusted-services path even when the
    # firewall denies by default, which is why this can deny and still work.
    default_action = var.network_isolation ? "Deny" : "Allow"
    bypass         = "AzureServices"
  }

  tags = var.tags
}

# --- diagnostics --------------------------------------------------------------
# One setting per resource. allLogs plus AllMetrics rather than a hand-listed
# category set, so a new category added by Azure is captured without a code change.

resource "azurerm_monitor_diagnostic_setting" "registry" {
  name                       = "diag-acr"
  target_resource_id         = azurerm_container_registry.this.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category_group = "allLogs"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}

resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  name                       = "diag-kv"
  target_resource_id         = azurerm_key_vault.this.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category_group = "allLogs"
  }

  enabled_metric {
    category = "AllMetrics"
  }
}
