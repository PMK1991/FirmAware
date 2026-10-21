# The evidence layer's two services: a Foundry account serving the models, and
# Azure AI Search holding the corpus index.
#
# Both are keyless. local_auth_enabled / local_authentication_enabled = false
# means no key exists that a leak could expose: the page reaches both with its
# managed identity and operators with `az login`, the same posture as the
# storage account (shared keys disabled) and the registry (admin disabled).
#
# The page calls Foundry directly rather than through a separate explain
# endpoint. Scores are produced by the batch pipeline and read from the lake, so
# a model outage can only stop the "Explain" button, never a score; the
# isolation a second endpoint would buy is already there.

resource "azurerm_cognitive_account" "foundry" {
  # checkov:skip=CKV_AZURE_134:public endpoint, but keyless: with local auth off it accepts only Entra tokens from identities holding Cognitive Services User. No private endpoint is built for this module yet, so network_isolation must add one before prod turns this off
  # checkov:skip=CKV2_AZURE_22:Microsoft-managed keys; the account stores no customer data at rest beyond model deployments, and prompts carry only a synthetic corpus and scores
  name                  = "ais-${var.name_prefix}-${var.unique_suffix}"
  location              = var.location
  resource_group_name   = var.resource_group_name
  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = "ais-${var.name_prefix}-${var.unique_suffix}"
  local_auth_enabled    = false
  tags                  = var.tags

  # A Foundry resource (AIServices with projects), which is what the Azure CLI
  # and portal create today. The flag forces replacement when it changes, so
  # leaving it at the provider's false would destroy and recreate the account,
  # and every model deployment with it, on the first apply that adopted it.
  project_management_enabled = true

  # Required by project management, and what the account already carries. It
  # is the account's own identity and is granted nothing here.
  identity {
    type = "SystemAssigned"
  }
}

# One deployment per model. Global Standard is pay per token with no idle cost;
# capacity is a rate limit in thousands of tokens per minute, not a reservation.
resource "azurerm_cognitive_deployment" "model" {
  for_each = var.models

  name                 = each.key
  cognitive_account_id = azurerm_cognitive_account.foundry.id

  model {
    format  = each.value.format
    name    = each.key
    version = each.value.version
  }

  sku {
    name     = "GlobalStandard"
    capacity = each.value.capacity
  }
}

resource "azurerm_search_service" "this" {
  # checkov:skip=CKV_AZURE_124:public endpoint, but keyless: RBAC only, the page holds Search Index Data Reader. Same missing private endpoint as the Foundry account above
  # checkov:skip=CKV_AZURE_207:the service reaches no other Azure resource: documents are pushed by `rag index`, there is no indexer or skillset for an identity to authenticate
  # checkov:skip=CKV_AZURE_208:the update SLA needs 3 replicas; dev runs the Free tier, which has one, and a rebuild is a new index behind an alias, so a failed update never reaches readers
  # checkov:skip=CKV_AZURE_209:the query SLA needs 2 replicas; dev runs one, and the only reader is an optional Explain button whose failure leaves every score intact
  name                = "srch-${var.name_prefix}-${var.unique_suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = var.search_sku
  tags                = var.tags

  # RBAC only: with local authentication off there is no admin or query key.
  local_authentication_enabled = false
}

# --- the page: read the index, call the models; nothing else -----------------
#
# Data-plane roles only, scoped to the one resource each. Search Index Data
# Reader cannot write a document or change an index; Cognitive Services User
# runs inference, and with local auth disabled the keys it could list unlock
# nothing. smoke_test_app.sh asserts the page holds exactly these on every
# deploy.

resource "azurerm_role_assignment" "app_search_reader" {
  scope                = azurerm_search_service.this.id
  role_definition_name = "Search Index Data Reader"
  principal_id         = var.app_principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_model_user" {
  scope                = azurerm_cognitive_account.foundry.id
  role_definition_name = "Cognitive Services User"
  principal_id         = var.app_principal_id
  principal_type       = "ServicePrincipal"
}
