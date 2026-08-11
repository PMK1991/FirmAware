variable "name_prefix" { type = string }
variable "env" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "unique_suffix" { type = string }
variable "tags" { type = map(string) }
variable "network_isolation" { type = bool }
variable "log_analytics_workspace_id" { type = string }
variable "scores_retention_days" { type = number }
variable "scores_immutability_locked" { type = bool }

variable "storage_network_default_action" {
  type        = string
  description = <<-EOT
    Firewall posture. Deny when private endpoints carry the traffic. Allow only
    in an environment without them, where it is the documented cost trade-off;
    authentication is unaffected because shared keys are disabled either way.
  EOT
  default     = "Deny"

  validation {
    condition     = contains(["Deny", "Allow"], var.storage_network_default_action)
    error_message = "storage_network_default_action must be Deny or Allow."
  }
}

variable "operator_ip_rules" {
  type        = list(string)
  description = "Public IPs allowed through the firewall when it denies by default."
  default     = []
}
