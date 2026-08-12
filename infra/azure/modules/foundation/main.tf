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
  # Premium-only capabilities, and dev deliberately runs Basic. Prod sets
  # registry_sku = "Premium", where geo-replication, zone redundancy, dedicated
  # data endpoints and content trust all become available -- but checkov reads
  # the module source with variable defaults, so it cannot see that.
  #
  # checkov:skip=CKV_AZURE_165:geo-replication is Premium-only; prod is Premium, dev is Basic by design
  # checkov:skip=CKV_AZURE_233:zone redundancy is Premium-only; same split
  # checkov:skip=CKV_AZURE_237:dedicated data endpoints are Premium-only; same split
  # checkov:skip=CKV_AZURE_164:content trust is Premium-only, and images are already pinned by digest everywhere they are consumed
  # checkov:skip=CKV_AZURE_139:public access IS disabled, by public_network_access_enabled = !var.network_isolation, which checkov cannot evaluate
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
  # Both of these ARE implemented, behind var.network_isolation: prod sets it
  # true, which flips public_network_access_enabled to false and instantiates
  # the private endpoint in the network module. checkov evaluates the module
  # source with defaults and cannot resolve either the ternary or the count.
  #
  # checkov:skip=CKV_AZURE_189:public access is disabled by !var.network_isolation, which prod sets
  # checkov:skip=CKV2_AZURE_32:the private endpoint is created by module.network, count-gated on the same variable
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
