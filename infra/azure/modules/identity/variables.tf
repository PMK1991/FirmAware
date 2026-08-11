variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "resource_group_id" { type = string }
variable "tags" { type = map(string) }
variable "container_ids" { type = map(string) }
variable "workspace_storage_account_id" { type = string }
variable "container_registry_id" { type = string }
variable "key_vault_id" { type = string }
variable "application_insights_id" { type = string }

variable "state_container_id" {
  type        = string
  description = <<-EOT
    Resource id of the tfstate container, so CI can read and write its own
    state. Left empty by the root module on purpose: bootstrap.sh already grants
    this, because the identity needs state access before Terraform has ever run.
    Kept as a variable so a caller that manages state inside this root can grant
    it here instead of relying on bootstrap.
  EOT
  default     = ""
}

variable "cicd_identity_name" {
  type        = string
  description = <<-EOT
    Name of the user-assigned identity bootstrap.sh created for CI. Read as a
    data source rather than created here: it must exist before the first apply,
    since it is the principal that runs that apply.
  EOT
}

variable "cicd_identity_resource_group_name" {
  type        = string
  description = <<-EOT
    Resource group holding the CI identity. This is the shared state resource
    group, not the environment's own group -- the identity outlives any single
    environment and must survive a `terraform destroy` of it, or the next apply
    would have no principal to authenticate as.
  EOT
}
