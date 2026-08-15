variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "tags" { type = map(string) }

variable "log_analytics_workspace_id" {
  type        = string
  description = "The environment streams console and system logs here. Same workspace as everything else, so one query spans the pipeline and the page."
}

variable "container_registry_login_server" { type = string }

variable "app_identity_id" {
  type        = string
  description = "Resource id of the user-assigned identity. Used both to run the app and to pull its image, so no registry credential exists."
}

variable "app_identity_client_id" {
  type        = string
  description = "Client id of the same identity. DefaultAzureCredential cannot choose between user-assigned identities without it, and fails at the first abfss:// read."
}

variable "image" {
  type        = string
  description = "Digest-pinned image reference, from deploy/azure/build.sh. Never a tag."
}

variable "scores_uri" { type = string }
variable "artifacts_uri" { type = string }
variable "data_uri" { type = string }

variable "infrastructure_subnet_id" {
  type        = string
  default     = null
  description = <<-EOT
    Delegated subnet for the environment, or null.

    Null is not "unconfigured": it selects a Consumption-only environment on
    platform-managed networking, which is what dev runs because dev has no VNet
    at all (network_isolation = false). Non-null selects a workload-profiles
    environment integrated into that subnet, which is what prod runs. The
    subnet's delegation follows from the same choice -- workload profiles require
    it, Consumption-only rejects it -- so the two cannot be mixed.
  EOT
}

variable "min_replicas" {
  type        = number
  default     = 0
  description = <<-EOT
    0 in dev, 1 in prod.

    Scale to zero costs a cold start of roughly half a minute and drops any
    in-session state, because the replica holding a browser's websocket is gone.
    For a read-only page in dev, whose only regular visitor is a smoke test, that
    is worth an idle cost of nothing. In prod a visitor should never pay it.
  EOT
}

variable "max_replicas" {
  type    = number
  default = 3
}

variable "zone_redundant" {
  type        = bool
  default     = false
  description = "Prod only, and only meaningful with a subnet: the platform cannot spread replicas across zones on platform-managed networking."
}
