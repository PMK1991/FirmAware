variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "unique_suffix" { type = string }
variable "tags" { type = map(string) }

variable "app_principal_id" {
  type        = string
  description = "Principal id of the page's managed identity: the only identity this module grants anything to."
}

variable "search_sku" {
  type        = string
  description = "free in dev (one per subscription, 50 MB, no SLA); basic or above where an SLA matters."

  validation {
    condition     = contains(["free", "basic", "standard"], var.search_sku)
    error_message = "search_sku must be free, basic or standard."
  }
}

variable "models" {
  description = <<-EOT
    Foundry deployments, keyed by model name (which is also the deployment
    name the page is configured with). The writer, the embedder, and in dev the
    eval judge. Every model here must have Global Standard quota in the
    subscription: a model with quota 0 fails the apply with InsufficientQuota.
  EOT
  type = map(object({
    format   = string
    version  = string
    capacity = number
  }))
}
