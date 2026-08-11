locals {
  containers = {
    data      = "Model inputs. Read-only to every runtime identity."
    artifacts = "Run outputs: model, preprocessor, feature list, metadata."
    scores    = "Append-only predictions. Immutability policy plus append-only RBAC."
    collected = "Inference data collection. Drift jobs only."
  }

  container_uris = {
    for name in keys(local.containers) :
    name => "abfss://${name}@${azurerm_storage_account.this.name}.dfs.core.windows.net"
  }
}

resource "azurerm_storage_account" "this" {
  name                     = "st${replace(var.name_prefix, "-", "")}${var.unique_suffix}"
  location                 = var.location
  resource_group_name      = var.resource_group_name
  account_tier             = "Standard"
  account_replication_type = var.env == "prod" ? "ZRS" : "LRS"
  account_kind             = "StorageV2"

  # Hierarchical namespace is what makes this ADLS Gen2 rather than plain blob,
  # and what makes abfss:// and directory-scoped RBAC work.
  is_hns_enabled = true

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false

  # The single most valuable control here: with shared keys disabled there is no
  # account key to leak, rotate, or accidentally paste into a config. Every
  # caller must present an AAD identity, which is why io.py uses
  # DefaultAzureCredential and never a connection string.
  shared_access_key_enabled = false

  # Encryption of the physical media underneath Azure's own at-rest encryption.
  # Set at creation and immutable afterwards.
  infrastructure_encryption_enabled = true

  blob_properties {
    # No versioning_enabled or change_feed_enabled here: both are unsupported on
    # an account with a hierarchical namespace, and HNS is not optional -- it is
    # what makes the abfss:// paths this pipeline reads and writes work at all.
    # Azure rejects the combination outright rather than ignoring it.
    #
    # The audit guarantee does not rest on versioning in any case. Scores are
    # append-only through two independent mechanisms that are unaffected by this:
    # an RBAC role with no blobs/delete action, and a container immutability
    # policy. Soft delete below still covers accidental deletion.
    delete_retention_policy {
      days = 30
    }

    container_delete_retention_policy {
      days = 30
    }
  }

  # Under isolation there is no public path at all -- blob and dfs are reachable
  # only through their private endpoints, which is why a prod release has to run
  # from a VNet-joined runner rather than a hosted one. Without isolation the
  # public path exists and the firewall below decides who may use it.
  public_network_access_enabled = !var.network_isolation

  # When isolation is on, the only way in is the private endpoint and this
  # denies everything else. When it is off, the firewall is the one control that
  # has to relax, so it is a named variable rather than a silent default -- and
  # authentication does not relax with it: shared keys stay disabled either way,
  # so an open network path still demands an AAD identity.
  network_rules {
    default_action = var.network_isolation ? "Deny" : var.storage_network_default_action
    bypass         = ["AzureServices", "Logging", "Metrics"]
    ip_rules       = var.operator_ip_rules
  }

  tags = var.tags
}

# The AML workspace's own system storage: run history, snapshots, its default
# datastore. It has to be a SECOND account, because Azure rejects a workspace
# whose default storage has a hierarchical namespace ("Cannot use storage with
# HNS enabled"), while the lake above must have one for abfss:// to work. One
# account cannot satisfy both, so each does the job it is allowed to do.
#
# It holds no pipeline data. Everything this project reads or writes -- inputs,
# artifacts, scores, collected inference -- lives in the lake, under the
# container-scoped RBAC in the identity module. That is why this account carries
# no containers, no immutability policy and no lifecycle rules.
resource "azurerm_storage_account" "workspace" {
  name                     = "stw${replace(var.name_prefix, "-", "")}${var.unique_suffix}"
  location                 = var.location
  resource_group_name      = var.resource_group_name
  account_tier             = "Standard"
  account_replication_type = var.env == "prod" ? "ZRS" : "LRS"
  account_kind             = "StorageV2"

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false

  # Same posture as the lake: no account key exists, so AML addresses this
  # account with the workspace's managed identity.
  shared_access_key_enabled         = false
  infrastructure_encryption_enabled = true

  public_network_access_enabled = !var.network_isolation

  network_rules {
    default_action = var.network_isolation ? "Deny" : var.storage_network_default_action
    bypass         = ["AzureServices", "Logging", "Metrics"]
    ip_rules       = var.operator_ip_rules
  }

  tags = var.tags
}

resource "azurerm_storage_container" "this" {
  for_each = local.containers

  name                  = each.key
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"

  metadata = {
    purpose = replace(lower(each.value), "/[^a-z0-9]+/", "_")
  }
}

# Evidence protection, enforced by the platform rather than by convention. The
# append-only RBAC in the identity module stops the runtime deleting a score;
# this stops anyone, including an operator with Contributor, doing so before the
# window expires.
resource "azurerm_storage_container_immutability_policy" "scores" {
  storage_container_resource_manager_id = azurerm_storage_container.this["scores"].id
  immutability_period_in_days           = var.scores_retention_days

  # A locked policy cannot be shortened or removed, and blocks deleting the
  # account for the whole window. Correct for prod; fatal for a dev environment
  # that has to be destroyable, hence the variable.
  locked = var.scores_immutability_locked

  # Terraform can create an unlocked policy and then be asked to delete it on
  # destroy; a locked one refuses, which is the intended behaviour in prod.
  protected_append_writes_all_enabled = true
}

resource "azurerm_storage_management_policy" "lifecycle" {
  storage_account_id = azurerm_storage_account.this.id

  rule {
    name    = "data-to-cool"
    enabled = true

    filters {
      prefix_match = ["data/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 90
      }

      version {
        # Old versions of an input are kept long enough to re-run a training
        # job against exactly what it saw, then tiered rather than deleted.
        change_tier_to_cool_after_days_since_creation = 90
      }
    }
  }
}

resource "azurerm_monitor_diagnostic_setting" "blob" {
  name                       = "diag-blob"
  target_resource_id         = "${azurerm_storage_account.this.id}/blobServices/default"
  log_analytics_workspace_id = var.log_analytics_workspace_id

  enabled_log {
    category_group = "audit"
  }

  enabled_metric {
    category = "Transaction"
  }
}
