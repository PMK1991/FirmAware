variable "name_prefix" { type = string }
variable "env" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "tags" { type = map(string) }
variable "network_isolation" { type = bool }
variable "storage_account_id" { type = string }
variable "container_ids" { type = map(string) }
variable "key_vault_id" { type = string }
variable "container_registry_id" { type = string }
variable "log_analytics_workspace_id" { type = string }
variable "application_insights_id" { type = string }
variable "workspace_identity_id" { type = string }
variable "compute_vm_size" { type = string }
variable "compute_max_nodes" { type = number }

variable "compute_subnet_id" {
  type    = string
  default = null
}
