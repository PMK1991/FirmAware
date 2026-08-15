locals {
  # The six tags the spec makes mandatory. Policy audits for exactly these, so
  # the map is defined once and passed to every module rather than restated.
  tags = {
    app                 = "firmaware"
    env                 = var.env
    owner               = var.owner
    cost_center         = var.cost_center
    data_classification = var.data_classification
    managed_by          = "terraform"
  }

  name_prefix = "firmaware-${var.env}"

  # Every resource this root creates lands in one group. Defaults to the
  # conventional rg-<prefix> name; dev overrides it to "firmAware" so the whole
  # environment is one obvious thing in the portal.
  #
  # Two environments cannot share a group name in one subscription, so this is
  # deliberately per-environment rather than a constant.
  resource_group_name = coalesce(var.resource_group_name, "rg-${local.name_prefix}")

  # Falls back to the name bootstrap.sh generates, so a normal setup needs no
  # tfvars entry and the two cannot drift apart by a typo.
  cicd_identity_name = coalesce(var.cicd_identity_name, "id-firmaware-bootstrap-${var.env}")
}

# Guard the one relaxation that would be silently ineffective rather than merely
# cheaper: private endpoints need a Premium registry, so asking for isolation on
# a Basic SKU has to fail at plan time, not at apply.
resource "terraform_data" "cost_profile_is_coherent" {
  lifecycle {
    precondition {
      condition     = !var.network_isolation || var.registry_sku == "Premium"
      error_message = "network_isolation requires registry_sku = \"Premium\": Basic and Standard registries have no private link support."
    }
  }
}

module "foundation" {
  source = "./modules/foundation"

  name_prefix         = local.name_prefix
  resource_group_name = local.resource_group_name
  location            = var.location
  unique_suffix       = var.unique_suffix
  tags                = local.tags
  registry_sku        = var.registry_sku
  network_isolation   = var.network_isolation
  purge_protection    = var.key_vault_purge_protection
  log_retention_days  = var.log_retention_days
}

module "network" {
  source = "./modules/network"
  count  = var.network_isolation ? 1 : 0

  name_prefix         = local.name_prefix
  location            = var.location
  resource_group_name = module.foundation.resource_group_name
  tags                = local.tags

  private_endpoint_targets = {
    blob      = { resource_id = module.storage.storage_account_id, subresource = "blob", dns_zone = "privatelink.blob.core.windows.net" }
    dfs       = { resource_id = module.storage.storage_account_id, subresource = "dfs", dns_zone = "privatelink.dfs.core.windows.net" }
    registry  = { resource_id = module.foundation.container_registry_id, subresource = "registry", dns_zone = "privatelink.azurecr.io" }
    vault     = { resource_id = module.foundation.key_vault_id, subresource = "vault", dns_zone = "privatelink.vaultcore.azure.net" }
    workspace = { resource_id = module.workspace.workspace_id, subresource = "amlworkspace", dns_zone = "privatelink.api.azureml.ms" }
  }
}

module "storage" {
  source = "./modules/storage"

  name_prefix                = local.name_prefix
  env                        = var.env
  location                   = var.location
  resource_group_name        = module.foundation.resource_group_name
  unique_suffix              = var.unique_suffix
  tags                       = local.tags
  network_isolation          = var.network_isolation
  log_analytics_workspace_id = module.foundation.log_analytics_workspace_id
  scores_retention_days      = var.scores_retention_days
  scores_immutability_locked = var.scores_immutability_locked

  storage_network_default_action = var.storage_network_default_action
  operator_ip_rules              = var.operator_ip_rules
}

module "identity" {
  source = "./modules/identity"

  name_prefix                  = local.name_prefix
  location                     = var.location
  resource_group_name          = module.foundation.resource_group_name
  resource_group_id            = module.foundation.resource_group_id
  tags                         = local.tags
  container_ids                = module.storage.container_ids
  workspace_storage_account_id = module.storage.workspace_storage_account_id
  container_registry_id        = module.foundation.container_registry_id
  key_vault_id                 = module.foundation.key_vault_id
  application_insights_id      = module.foundation.application_insights_id

  # Created by bootstrap.sh, not by this root. It has to pre-exist: it is the
  # principal CI authenticates as to run this very apply.
  cicd_identity_name                = local.cicd_identity_name
  cicd_identity_resource_group_name = var.cicd_identity_resource_group_name
}

# Azure RBAC is eventually consistent, and workspace creation is the first thing
# in this root that depends on roles granted moments earlier. Without a pause the
# first apply of a new environment can fail with
#
#   User assigned identity doesn't have enough permissions ...
#   Microsoft.KeyVault/vaults/read ... If access was recently granted, please
#   refresh your credentials.
#
# Two things make that failure worse than a normal race, and both were observed
# while bringing up dev. It reports a single missing action even when several
# grants are absent, so it is not a checklist. And Azure ML caches the negative
# result against the *workspace name*: once creation has failed, retrying the
# same name keeps failing for tens of minutes after the permissions are correct,
# while an otherwise identical request under a new name succeeds immediately.
# That combination will send a reader hunting for a permissions bug that is
# already fixed, so the wait is here to keep the first attempt from failing at
# all rather than to make a retry work.
resource "time_sleep" "role_propagation" {
  depends_on      = [module.identity]
  create_duration = "180s"
}

module "workspace" {
  source = "./modules/workspace"

  name_prefix         = local.name_prefix
  env                 = var.env
  location            = var.location
  resource_group_name = module.foundation.resource_group_name
  tags                = local.tags
  network_isolation   = var.network_isolation
  # The workspace's system storage, not the lake: AML rejects an HNS account as
  # its default. Pipeline data is reached through datastores over the lake's
  # container-scoped RBAC, not through this account.
  storage_account_id         = module.storage.workspace_storage_account_id
  container_ids              = module.storage.container_ids
  key_vault_id               = module.foundation.key_vault_id
  container_registry_id      = module.foundation.container_registry_id
  log_analytics_workspace_id = module.foundation.log_analytics_workspace_id
  application_insights_id    = module.foundation.application_insights_id
  workspace_identity_id      = module.identity.workspace_identity_id
  compute_vm_size            = var.compute_vm_size
  compute_max_nodes          = var.compute_max_nodes
  compute_subnet_id          = var.network_isolation ? module.network[0].compute_subnet_id : null

  # The workspace's own identity needs its storage and registry grants to exist
  # before it will provision, and Terraform cannot see that through data flow.
  # The sleep, not the module, is the dependency: the grants must also have
  # propagated, not merely been created.
  depends_on = [time_sleep.role_propagation]
}

module "endpoints" {
  source = "./modules/endpoints"

  env                     = var.env
  location                = var.location
  tags                    = local.tags
  workspace_id            = module.workspace.workspace_id
  endpoint_identity_id    = module.identity.endpoint_identity_id
  online_endpoint_enabled = var.online_endpoint_enabled
  network_isolation       = var.network_isolation
}

module "monitoring" {
  source = "./modules/monitoring"

  name_prefix                = local.name_prefix
  env                        = var.env
  resource_group_name        = module.foundation.resource_group_name
  resource_group_id          = module.foundation.resource_group_id
  location                   = var.location
  tags                       = local.tags
  alerts_email               = var.alerts_email
  monthly_budget             = var.monthly_budget
  log_analytics_workspace_id = module.foundation.log_analytics_workspace_id
}

# The page. Last, because it consumes the storage URIs, the registry and the
# identity that the modules above create, and because nothing else depends on it
# -- the pipeline runs whether or not anyone is looking at it.
module "app" {
  source = "./modules/app"

  name_prefix                     = local.name_prefix
  location                        = var.location
  resource_group_name             = module.foundation.resource_group_name
  tags                            = local.tags
  log_analytics_workspace_id      = module.foundation.log_analytics_workspace_id
  container_registry_login_server = module.foundation.container_registry_login_server

  app_identity_id        = module.identity.app_identity_id
  app_identity_client_id = module.identity.app_identity_client_id
  image                  = var.app_image

  # From the storage module rather than assembled here, so the page cannot be
  # pointed at a path this deployment never created.
  scores_uri    = module.storage.scores_uri
  artifacts_uri = module.storage.artifacts_uri
  data_uri      = module.storage.data_uri

  # Null in dev, which has no VNet at all. Same conditional as the workspace's
  # compute subnet, and the same relaxation behind it.
  infrastructure_subnet_id = var.network_isolation ? module.network[0].apps_subnet_id : null

  min_replicas   = var.app_min_replicas
  max_replicas   = var.app_max_replicas
  zone_redundant = var.env == "prod" && var.network_isolation

  # The identity's read grants must exist before a replica starts, or the first
  # page load fails on authorization and the revision looks broken. Same
  # propagation problem the workspace has, and the same sleep answers it.
  depends_on = [time_sleep.role_propagation]
}

module "policy" {
  source = "./modules/policy"

  name_prefix       = local.name_prefix
  resource_group_id = module.foundation.resource_group_id
  location          = var.location
  required_tags     = keys(local.tags)
}
