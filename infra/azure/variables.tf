variable "subscription_id" {
  type        = string
  description = "Subscription that owns every resource in this root."
}

variable "env" {
  type        = string
  description = "Environment name; part of every resource name and the env tag."

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod."
  }
}

variable "location" {
  type        = string
  description = "Azure region."
  default     = "eastus"
}

variable "unique_suffix" {
  type        = string
  description = <<-EOT
    Short suffix that makes globally-unique names (storage, ACR, Key Vault)
    unique without a random value. Explicit rather than random so a destroy and
    re-apply reproduces the same names instead of orphaning the old ones.
  EOT

  validation {
    condition     = can(regex("^[a-z0-9]{2,6}$", var.unique_suffix))
    error_message = "unique_suffix must be 2-6 lowercase alphanumeric characters."
  }
}

# --- mandatory tags -----------------------------------------------------------
# Six tags on every resource, enforced by policy as well as by this variable set.

variable "owner" {
  type        = string
  description = "Team or person accountable for the environment."
}

variable "cost_center" {
  type        = string
  description = "Chargeback code carried by every resource."
}

variable "data_classification" {
  type        = string
  description = "Sensitivity of the data the environment holds."

  validation {
    condition     = contains(["public", "internal", "confidential"], var.data_classification)
    error_message = "data_classification must be public, internal or confidential."
  }
}

# --- cost profile -------------------------------------------------------------
# The one place dev and prod deliberately differ. Every relaxation here is
# env-scoped, defaulted to the secure value, and justified in infra/azure/README.md.

variable "network_isolation" {
  type        = bool
  description = <<-EOT
    Private endpoints, private DNS and public access disabled on every PaaS
    resource. Default true because the spec makes it a non-negotiable; dev sets
    it false because five private endpoints plus the Premium registry they
    require cost more per month than the rest of dev put together. When false,
    the compensating controls are AAD-only auth, shared keys disabled and the
    storage firewall, all of which stay on in both environments.
  EOT
  default     = true
}

variable "storage_network_default_action" {
  type        = string
  description = <<-EOT
    Storage firewall posture when there are no private endpoints to carry the
    traffic. Ignored entirely when network_isolation is on, where the firewall is
    forced to Deny. Without isolation the compute cluster and CI reach the
    account over its public path, so Deny with no operator_ip_rules would lock
    the environment out of its own data -- that is the specific cost of relaxing
    isolation, and it is stated here rather than discovered at apply time.
    Authentication does not relax with it: shared keys stay disabled, so an open
    network path still demands an AAD identity.
  EOT
  default     = "Deny"

  validation {
    condition     = contains(["Deny", "Allow"], var.storage_network_default_action)
    error_message = "storage_network_default_action must be Deny or Allow."
  }
}

variable "operator_ip_rules" {
  type        = list(string)
  description = <<-EOT
    Public IPs allowed through the storage firewall when it denies by default.
    Preferred over flipping the default action to Allow where the set of callers
    is known and stable.
  EOT
  default     = []
}

variable "registry_sku" {
  type        = string
  description = <<-EOT
    Premium is required for private endpoints, immutable tags and quarantine, so
    it is the default and the only valid choice when network_isolation is on.
  EOT
  default     = "Premium"

  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.registry_sku)
    error_message = "registry_sku must be Basic, Standard or Premium."
  }
}

variable "key_vault_purge_protection" {
  type        = bool
  description = <<-EOT
    Blocks permanent deletion for the soft-delete window. True in prod. False in
    dev only because a purge-protected vault reserves its name for 90 days,
    which makes a teardown and re-apply of a throwaway environment impossible.
  EOT
  default     = true
}

variable "log_retention_days" {
  type        = number
  description = "Log Analytics retention. The spec's floor is 90 days."
  default     = 90

  validation {
    condition     = var.log_retention_days >= 90
    error_message = "log_retention_days must be at least 90."
  }
}

# --- compute ------------------------------------------------------------------

variable "compute_vm_size" {
  type        = string
  description = "Training cluster node size."
  default     = "Standard_DS3_v2"
}

variable "compute_max_nodes" {
  type        = number
  description = "Training cluster ceiling. Minimum is always 0: it scales to zero."
  default     = 4
}

variable "online_endpoint_enabled" {
  type        = bool
  description = <<-EOT
    Managed online endpoints cannot scale to zero, so an idle one bills around
    the clock. Kept as a switch so a dev environment can exist without paying
    for a serving surface nobody is calling.
  EOT
  default     = true
}

variable "online_instance_type" {
  type        = string
  description = <<-EOT
    Instance type backing each online deployment. Terraform does not create the
    deployment -- `az ml` does -- but it owns the sizing decision and exports it
    so deploy_endpoint.sh cannot pick a different SKU per environment by hand.
  EOT
  default     = "Standard_DS3_v2"
}

variable "online_min_instances" {
  type        = number
  description = <<-EOT
    Floor on instances per online deployment, exported for the same reason as the
    instance type: the sizing decision belongs to the environment definition, not
    to a shell default.

    This must not be left to deploy_endpoint.sh's fallback of 0. A managed online
    deployment at zero instances has no warm capacity, so the first request after
    an idle period pays a cold start measured in minutes -- acceptable in dev,
    where nothing depends on it, and an outage in prod. Prod therefore sets 1.
  EOT
  default     = 1

  validation {
    condition     = var.online_min_instances >= 0 && floor(var.online_min_instances) == var.online_min_instances
    error_message = "online_min_instances must be a non-negative whole number."
  }
}

# --- immutability -------------------------------------------------------------

variable "scores_retention_days" {
  type        = number
  description = "Time-based immutability window on the scores container."
  default     = 2555 # seven years
}

variable "scores_immutability_locked" {
  type        = bool
  description = <<-EOT
    A locked policy is irreversible and blocks deleting the container or the
    account for the whole retention window. Locked in prod, unlocked in dev so a
    throwaway environment can still be torn down; the append-only RBAC that
    actually stops the runtime deleting evidence is identical in both.
  EOT
  default     = true
}

# --- release ------------------------------------------------------------------
#
# There is deliberately no image_digest variable here. Deployments -- the only
# things that reference an image -- are created by `az ml` as part of a release,
# not by terraform apply. Putting the digest in tfvars would imply an apply is
# needed to ship, which is exactly the coupling the endpoint/deployment split
# exists to avoid.

variable "resource_group_name" {
  type        = string
  default     = null
  description = <<-EOT
    Single resource group holding every resource this root creates. Null falls
    back to rg-<name_prefix>. The Terraform state account and the CI identity are
    deliberately NOT here: they live in their own group created by bootstrap.sh,
    so that destroying an environment cannot delete the state describing it.
  EOT
}

# --- CI/CD federation ---------------------------------------------------------
#
# There are no github_* variables here on purpose. The federated credential is
# created by infra/azure/bootstrap.sh, not by Terraform, because Terraform cannot
# create the credential that authenticates the apply that creates it. Declaring
# the values in both places invites drift, and drift here surfaces as a failed
# token exchange in CI rather than as a diff in a plan.

variable "cicd_identity_name" {
  type        = string
  description = <<-EOT
    The user-assigned identity bootstrap.sh created for this environment. Read
    as a data source, never created here: it is the principal that authenticates
    the apply, so it cannot also be a product of the apply. Defaults to the name
    bootstrap.sh uses, so envs need only override it if they renamed it.
  EOT
  default     = ""
}

variable "cicd_identity_resource_group_name" {
  type        = string
  description = <<-EOT
    Resource group holding the CI identity -- the shared tfstate group, not this
    environment's own. Keeping it outside means `terraform destroy` on an
    environment does not delete the credential needed to recreate it.
  EOT
  default     = "rg-firmaware-tfstate"
}

# --- monitoring ---------------------------------------------------------------

variable "alerts_email" {
  type        = string
  description = "Address the action group notifies."
}

variable "monthly_budget" {
  type        = number
  description = "Budget in USD; alerts fire at 50, 80 and 100 percent of it."
  default     = 50
}
