# Endpoints are infrastructure; deployments are releases.
#
# The endpoint is a stable name and a traffic map that outlives any model, so
# Terraform owns it. The blue and green deployments underneath it change with
# every release and are created by deploy/azure/deploy_endpoint.sh through
# `az ml`, which is also what performs the traffic shift. Splitting them this
# way is what makes rollback a traffic update rather than a terraform apply.
#
# azurerm has no resource for either endpoint kind, so azapi calls the same ARM
# API directly. That keeps them in the plan and in state instead of being
# conjured by a script nobody reviews.

resource "azapi_resource" "online" {
  count = var.online_endpoint_enabled ? 1 : 0

  type      = "Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2024-04-01"
  name      = "firmaware-score-${var.env}"
  parent_id = var.workspace_id
  location  = var.location
  tags      = var.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [var.endpoint_identity_id]
  }

  body = {
    properties = {
      description = "Real-time firmware deployment risk scoring."

      # AAD tokens rather than a shared key. A key would be one more secret to
      # store, rotate and leak; a token is minted per caller and expires.
      authMode = "AADToken"

      # Under isolation the endpoint is reachable only through its private
      # endpoint, so there is no public ingress at all. Without isolation it is
      # the single deliberate public ingress in this design, and even then it is
      # behind AAD auth rather than an anonymous or key-based path.
      publicNetworkAccess = var.network_isolation ? "Disabled" : "Enabled"
    }
  }

  # Traffic is a release concern, moved by promote_traffic.sh between deploys.
  # Terraform would otherwise revert a promotion on the next apply.
  lifecycle {
    ignore_changes = [body]
  }

  # The endpoint provisions before either deployment exists, so it starts with
  # an empty traffic map. That is expected, not an error.
  schema_validation_enabled = false
}

resource "azapi_resource" "batch" {
  type      = "Microsoft.MachineLearningServices/workspaces/batchEndpoints@2024-04-01"
  name      = "firmaware-batch-${var.env}"
  parent_id = var.workspace_id
  location  = var.location
  tags      = var.tags

  # SystemAssigned, unlike the online endpoint above, because batch endpoints
  # reject a user-assigned identity outright: "does not support creation of
  # 'UserAssigned' resource identity. The supported types are 'SystemAssigned'".
  #
  # This costs nothing here. A batch endpoint is only a routing name -- the work
  # runs in a batch deployment on the training cluster, which carries the
  # workspace identity and its container-scoped grants. So the append-only
  # guarantee on scores is still enforced by the identity that does the writing.
  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      description = "Scheduled bulk scoring. Writes one append-only object per run."
      authMode    = "AADToken"
    }
  }

  lifecycle {
    ignore_changes = [body]
  }

  schema_validation_enabled = false
}
