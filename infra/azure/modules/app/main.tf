# Somewhere to run app.py with an identity.
#
# The page already knows how to read abfss:// through firmaware.io; what it has
# never had on Azure is a host that carries a managed identity. Streamlit
# Community Cloud has none, so pointing the public demo at the lake would mean a
# client secret in Streamlit's secrets store -- the one thing the rest of this
# deployment is built to avoid. Container Apps supplies an identity, so the page
# can read real scores with no credential material anywhere.
#
# This is a second deployment of the same app.py, not a replacement: Community
# Cloud keeps serving the committed sample from `.[app]`.

# --- environment --------------------------------------------------------------

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.name_prefix}"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  log_analytics_workspace_id = var.log_analytics_workspace_id
  tags                       = var.tags

  # Null in dev. See the variable's own description: this single value decides
  # Consumption-only versus workload profiles, and the subnet delegation has to
  # agree with it.
  infrastructure_subnet_id = var.infrastructure_subnet_id

  # External. The page is deliberately reachable from the internet -- see "A
  # public page" in infra/azure/README.md, which records that decision and what
  # it rests on. Stated explicitly rather than left to the provider default,
  # because it is the most consequential line in this module.
  internal_load_balancer_enabled = false

  zone_redundancy_enabled = var.zone_redundant && var.infrastructure_subnet_id != null

  # Only when the environment is VNet-integrated. A Consumption-only environment
  # rejects workload profiles outright, and a workload-profiles environment is
  # the only kind that can use a delegated subnet -- so the two switch together.
  dynamic "workload_profile" {
    for_each = var.infrastructure_subnet_id == null ? [] : [1]
    content {
      name                  = "Consumption"
      workload_profile_type = "Consumption"
    }
  }

  lifecycle {
    precondition {
      condition     = !var.zone_redundant || var.infrastructure_subnet_id != null
      error_message = "zone_redundant requires infrastructure_subnet_id: the platform can only spread replicas across zones inside a VNet-integrated environment."
    }
  }
}

# --- the app ------------------------------------------------------------------

resource "azurerm_container_app" "this" {
  name                         = "ca-${var.name_prefix}"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = var.resource_group_name
  tags                         = var.tags

  # Multiple, not Single, and this is the whole rollback story.
  #
  # In Single mode a new image replaces the running revision and the old one is
  # gone -- rolling back means rebuilding and redeploying. In Multiple mode the
  # superseded revision stays provisioned at 0% traffic, so rollback_app.sh is
  # one control-plane call and no container start, exactly as the ML endpoint's
  # blue/green slots are.
  revision_mode = "Multiple"

  # No `secret` blocks anywhere in this resource, and that is an assertion rather
  # than an omission: smoke_test_app.sh fails the deploy if
  # properties.configuration.secrets is non-empty. Storage is reached with the
  # identity below, and so is the registry, so there is nothing left that a
  # secret could hold.
  identity {
    type         = "UserAssigned"
    identity_ids = [var.app_identity_id]
  }

  # Pull with the identity, not with admin credentials. The registry has
  # admin_enabled = false, so there is no registry password in existence to put
  # here even if someone wanted to.
  registry {
    server   = var.container_registry_login_server
    identity = var.app_identity_id
  }

  ingress {
    external_enabled = true
    target_port      = 8501

    # auto, so the platform negotiates HTTP/1.1 or HTTP/2 as the client asks.
    # Streamlit's session is a websocket, which `auto` upgrades correctly; `http2`
    # would not.
    transport = "auto"

    # No plaintext listener at all. The platform terminates TLS at 1.2 minimum
    # and there is no port 80 to downgrade to.
    allow_insecure_connections = false

    # The starting map, and the only time Terraform decides it. From the first
    # release onwards deploy_app.sh pins traffic to named revisions so a new one
    # can sit at 0% while it is smoke tested -- which is why traffic_weight is
    # ignored below.
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name  = "page"
      image = var.image

      # 1 vCPU / 2Gi. The combinations Container Apps accepts are fixed pairs,
      # and this is the smallest that starts pandas, plotly and the ADLS client
      # without the cold start running past the startup probe's budget.
      cpu    = 1.0
      memory = "2Gi"

      # Everything the page reads, sourced from the storage module's own outputs
      # so a container can never be configured with a path this deployment did
      # not create.
      #
      # The container root, with no sub-prefix. run_batch_scoring.sh publishes
      # each run as `scores_<timestamp>_<job>.csv` at the top level of the
      # container, so a prefixed URI would list nothing -- and app.py's fallback
      # would quietly render the committed demo fixture instead of failing.
      env {
        name  = "FIRMAWARE_SCORES_URI"
        value = var.scores_uri
      }

      env {
        name  = "FIRMAWARE_ARTIFACTS_URI"
        value = var.artifacts_uri
      }

      env {
        name  = "FIRMAWARE_DATA_URI"
        value = var.data_uri
      }

      env {
        name  = "FIRMAWARE_UPCOMING_URI"
        value = "${var.data_uri}/upcoming_deployments.csv"
      }

      # The one genuinely non-obvious setting here. With a user-assigned
      # identity, DefaultAzureCredential has no way to know which of the
      # subscription's identities it is meant to be, and fails at the first
      # abfss:// read with an error about the managed identity endpoint that
      # reads like the identity is missing rather than ambiguous.
      env {
        name  = "AZURE_CLIENT_ID"
        value = var.app_identity_client_id
      }

      # Streamlit's own health endpoint, not `/`.
      #
      # `/` returns Streamlit's bootstrap HTML before the app script has run, so
      # a probe against it reports healthy while the page is still failing to
      # reach storage. `/_stcore/health` is served by the same server and is the
      # narrowest thing that means "the server is up".
      startup_probe {
        transport               = "HTTP"
        port                    = 8501
        path                    = "/_stcore/health"
        interval_seconds        = 5
        failure_count_threshold = 30
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 8501
        path                    = "/_stcore/health"
        initial_delay           = 10
        interval_seconds        = 30
        failure_count_threshold = 3
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 8501
        path                    = "/_stcore/health"
        interval_seconds        = 10
        failure_count_threshold = 3
      }
    }
  }

  lifecycle {
    # The same split the endpoints module draws: the app is infrastructure, a
    # revision is a release.
    #
    # deploy_app.sh creates each new revision pinned to a digest and moves
    # traffic to it only after the smoke test passes. If Terraform owned these
    # two fields it would revert both on the next apply -- undoing a promotion,
    # or worse, silently rolling the image back to whatever the last apply saw.
    ignore_changes = [
      template[0].container[0].image,
      ingress[0].traffic_weight,
    ]

    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
      error_message = "image must be digest-pinned (<registry>/<repo>@sha256:...): a tag can be moved between the plan and the apply."
    }
  }
}
