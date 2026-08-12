# Three identities, one per trust boundary: what trains, what serves, and what
# deploys. Splitting them means a compromise of the serving surface cannot
# retrain a model, and a leaked CI token cannot read the data lake.
#
# Every role assignment below carries a comment naming why that principal needs
# that role at that scope. A reviewer should be able to audit the whole RBAC
# posture from this file without opening the portal.

resource "azurerm_user_assigned_identity" "workspace" {
  name                = "id-${var.name_prefix}-workspace"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "endpoint" {
  name                = "id-${var.name_prefix}-endpoint"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# The deploy identity is NOT created here. bootstrap.sh creates it, because it
# has to exist before the first `terraform apply` -- it is the identity that runs
# that apply from CI, and Terraform cannot create the credential it authenticates
# with. Creating a second one here would be worse than redundant: CI would
# authenticate as the bootstrap identity while every role below was granted to a
# different principal, and every deploy would fail on authorization.
#
# So this reads the bootstrap identity and grants it what it needs. bootstrap.sh
# gives it access to Terraform state and nothing else; everything else it can do
# is granted below, at resource-group scope, and is therefore visible in a plan.
data "azurerm_user_assigned_identity" "cicd" {
  name                = var.cicd_identity_name
  resource_group_name = var.cicd_identity_resource_group_name
}

# --- append-only role ---------------------------------------------------------
# Storage Blob Data Contributor is the closest built-in, and it grants delete.
# Scores are evidence: a run may add to them and read them back, and nothing in
# the runtime may remove them. That role does not exist, so it is defined here.
# The omission of .../blobs/delete is the entire point of this resource.

resource "azurerm_role_definition" "scores_appender" {
  name        = "Storage Blob Data Appender (${var.name_prefix})"
  scope       = var.resource_group_id
  description = "Read, write and add blobs. Deliberately cannot delete: the scores container is append-only evidence."

  permissions {
    actions = []
    data_actions = [
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action",
    ]
    not_actions      = []
    not_data_actions = []
  }

  assignable_scopes = [var.resource_group_id]
}

# --- workspace identity -------------------------------------------------------
# Runs training. Reads inputs, writes run outputs, appends scores, pulls the image.

# Training reads deployment history. Read, never write: a training run must not
# be able to alter the data it is judged against.
resource "azurerm_role_assignment" "workspace_data_reader" {
  scope                = var.container_ids["data"]
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# Training writes the model, preprocessor, feature list and metadata for the run.
resource "azurerm_role_assignment" "workspace_artifacts_contributor" {
  scope                = var.container_ids["artifacts"]
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# Batch scoring appends one object per run. Append-only, not Contributor, so a
# re-run cannot overwrite the previous run's evidence even by mistake.
resource "azurerm_role_assignment" "workspace_scores_appender" {
  scope              = var.container_ids["scores"]
  role_definition_id = azurerm_role_definition.scores_appender.role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.workspace.principal_id
}

# Drift jobs run on the workspace and read what the endpoint collected.
resource "azurerm_role_assignment" "workspace_collected_contributor" {
  scope                = var.container_ids["collected"]
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# The workspace's own system storage: run history, snapshots, its default
# datastore. Account-scoped rather than container-scoped because AML creates and
# names those containers itself, so there is nothing narrower to point at yet.
#
# This grant is what makes shared_access_key_enabled = false survivable on that
# account. Without it AML has neither a key nor an identity that can reach its
# own storage, and the workspace provisions into a broken state.
resource "azurerm_role_assignment" "workspace_system_storage_contributor" {
  scope                = var.workspace_storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# Table and Queue, and neither is redundant with the Blob grant above: each is a
# distinct data-plane surface with its own roles, and the Contributor assignment
# further down is control plane only -- its dataActions list is empty -- so it
# covers none of the three.
#
# Batch endpoints run on ParallelRunStep, whose driver uses all three surfaces on
# this account: Blob for the snapshot and output, Tables for job telemetry and
# heartbeats, and a Queue to hand mini-batches to the worker processes. With
# shared_access_key_enabled = false there is no key to fall back on, so a missing
# role is fatal rather than slow. The two failures are distinct and sequential --
# without Tables the driver dies at startup on TableNotFound, and with Tables but
# no Queue it gets as far as task creation and dies there -- so fixing one simply
# reveals the other.
#
# Neither shows up in training, which uses the blob datastore only. Nothing here
# is exercised until a batch job is actually invoked, and the job exits 42 with
# an empty user log, having never called the scoring script.
resource "azurerm_role_assignment" "workspace_system_storage_table_contributor" {
  scope                = var.workspace_storage_account_id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

resource "azurerm_role_assignment" "workspace_system_storage_queue_contributor" {
  scope                = var.workspace_storage_account_id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# --- what Azure ML itself requires of the workspace identity -------------------
# Microsoft documents an exact table for a workspace with a user-assigned
# identity, and workspace provisioning fails outright without it -- surfacing as
# a BadRequest naming a single missing action, which is far less informative
# than it sounds. The table is:
#
#   Storage         Contributor (control plane) + Storage Blob Data Contributor
#   Key Vault(RBAC) Contributor (control plane) + Key Vault Administrator
#   ACR             Contributor
#   App Insights    Contributor
#
# Each is granted at the individual resource, not inherited from the group. The
# read-only group-scope Reader below deliberately does not substitute for these:
# provisioning performs control-plane *writes* on all four dependencies.
#
# Source: learn.microsoft.com "Set up authentication between Azure Machine
# Learning and other services", user-assigned managed identity role table.

# Key Vault Administrator is data-plane only -- its `actions` list is empty --
# so on an RBAC-enabled vault it cannot satisfy Microsoft.KeyVault/vaults/read.
# Both halves are required, which is exactly what the table says.
resource "azurerm_role_assignment" "workspace_kv_admin" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Administrator"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

resource "azurerm_role_assignment" "workspace_kv_contributor" {
  scope                = var.key_vault_id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# AcrPull above covers a compute node pulling an image. The workspace resource
# itself also manages the registry, which pull alone does not permit.
resource "azurerm_role_assignment" "workspace_acr_contributor" {
  scope                = var.container_registry_id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# The control-plane half for the workspace's own system storage account. The
# data-plane half is workspace_system_storage_contributor above.
resource "azurerm_role_assignment" "workspace_system_storage_control" {
  scope                = var.workspace_storage_account_id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# The workspace links Application Insights and configures it during creation.
resource "azurerm_role_assignment" "workspace_app_insights_contributor" {
  scope                = var.application_insights_id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# Read-only, and the reason it is at group scope rather than per resource: the
# workspace issues control-plane GETs against each of its dependencies while
# provisioning, and Reader grants no ability to change any of them. The
# alternative -- Contributor on the resource group, as most examples do -- would
# hand the runtime identity write access to the whole environment.
resource "azurerm_role_assignment" "workspace_rg_reader" {
  scope                = var.resource_group_id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# Compute nodes pull the training image. Pull only: nothing that runs a job may
# push a new image and thereby change what a later job executes.
resource "azurerm_role_assignment" "workspace_acr_pull" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# The workspace stores its own connection material in the linked vault. Secrets
# User reads values; it cannot create, list-manage or delete them.
resource "azurerm_role_assignment" "workspace_kv_secrets_user" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.workspace.principal_id
}

# --- endpoint identity --------------------------------------------------------
# Serves. Reads the model it was pinned to, records what it was asked, nothing else.

# Loads the registered model artifact at init(). Read-only: a serving container
# must never be able to modify the artifact it is serving.
resource "azurerm_role_assignment" "endpoint_artifacts_reader" {
  scope                = var.container_ids["artifacts"]
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.endpoint.principal_id
}

# Inference data collection writes inputs and outputs here. This is the only
# container the serving surface can write to.
resource "azurerm_role_assignment" "endpoint_collected_contributor" {
  scope                = var.container_ids["collected"]
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.endpoint.principal_id
}

# Pulls the inference image.
resource "azurerm_role_assignment" "endpoint_acr_pull" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.endpoint.principal_id
}

# Note what is absent: the endpoint has no grant on data and none on scores. A
# compromised serving container cannot read the training corpus or touch the
# prediction record.

# --- CI/CD identity -----------------------------------------------------------
# Deploys. Federated to GitHub, so it holds no password at all.

# Creates and updates workspace assets: environments, models, endpoints,
# deployments. Scoped to the resource group, never the subscription.
resource "azurerm_role_assignment" "cicd_azureml_data_scientist" {
  scope                = var.resource_group_id
  role_definition_name = "AzureML Data Scientist"
  principal_id         = data.azurerm_user_assigned_identity.cicd.principal_id
}

# Pushes the image it just built. Push implies pull, so no separate AcrPull.
resource "azurerm_role_assignment" "cicd_acr_push" {
  scope                = var.container_registry_id
  role_definition_name = "AcrPush"
  principal_id         = data.azurerm_user_assigned_identity.cicd.principal_id
}

# Terraform's own reads and writes of the state blob.
resource "azurerm_role_assignment" "cicd_state_contributor" {
  count = var.state_container_id == "" ? 0 : 1

  scope                = var.state_container_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_user_assigned_identity.cicd.principal_id
}

# The batch smoke test reads the score object the run just published and then
# asserts a second write to it is refused. Contributor on the resource group is
# control plane only -- it lets CI manage the storage account and grants no
# access whatsoever to the data inside it -- so without this the smoke test
# fails on "You do not have the required permissions", never having read a byte.
#
# The appender role, not Storage Blob Data Contributor, and not Reader.
#
# Reader would be enough to read the object back, and would make the
# immutability assertion meaningless: the overwrite would be refused for lack of
# permission rather than by the policy, so the test would report the container
# is immutable without ever having tested it. The assertion is only worth
# anything if the identity making it could otherwise have succeeded.
#
# Contributor would grant delete, on the one container whose entire purpose is
# that nothing in the pipeline can remove evidence. CI is the identity most
# exposed to a leaked token, so it is the last one that should hold it.
resource "azurerm_role_assignment" "cicd_scores_appender" {
  scope              = var.container_ids["scores"]
  role_definition_id = azurerm_role_definition.scores_appender.role_definition_resource_id
  principal_id       = data.azurerm_user_assigned_identity.cicd.principal_id
}

# Terraform must be able to create the resources it plans. This is the widest
# grant in the file and the reason it is scoped to one resource group: a leaked
# GitHub token reaches this environment and stops at its boundary.
resource "azurerm_role_assignment" "cicd_rg_contributor" {
  scope                = var.resource_group_id
  role_definition_name = "Contributor"
  principal_id         = data.azurerm_user_assigned_identity.cicd.principal_id
}

# Contributor cannot grant roles, and Terraform creates role assignments, so
# this is required. User Access Administrator at subscription scope would be a
# privilege-escalation path; at resource-group scope it can only re-grant within
# an environment it already controls.
resource "azurerm_role_assignment" "cicd_rg_user_access_admin" {
  scope                = var.resource_group_id
  role_definition_name = "User Access Administrator"
  principal_id         = data.azurerm_user_assigned_identity.cicd.principal_id

  # Without this, the assignment lets the principal grant itself anything at
  # this scope. The condition pins it to the roles Terraform actually assigns.
  #
  # ForAllOfAllValues, not ForAnyOfAnyValues. The "Any/Any" form asks whether
  # any requested role differs from any listed role, which is trivially true the
  # moment the list holds two different GUIDs -- the condition would evaluate
  # true for every request, including a request to assign Owner, and the control
  # would be decorative. "All/All" is the documented deny-list form: every
  # requested role must differ from every listed role.
  condition_version = "2.0"
  condition         = <<-EOT
    (
      (
        !(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})
      )
      OR
      (
        @Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAllOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, 18d7d88d-d35e-4fb5-a5c3-7773c20a72d9}
      )
    )
  EOT
}

# --- GitHub federation is NOT here --------------------------------------------
# It is created by bootstrap.sh, and that is a security decision rather than a
# convenience one.
#
# For Terraform to manage the federated credential, the CI identity would need
# write access to its own federatedIdentityCredentials -- and an identity that
# can add credentials to itself can add a subject for any branch or environment,
# which is precisely the gate the credential exists to enforce. Terraform would
# be managing the lock while holding a key to it.
#
# bootstrap.sh is run by a human, so the subject that decides which workflow may
# become this identity is set by someone the directory already trusts. The
# script is in this repository and reviewed like anything else; what it is not
# is writable by the pipeline it authorises.
#
# CI needs only to READ this identity (Terraform's data source), which is why
# bootstrap grants Reader on the identity resource and nothing more.

