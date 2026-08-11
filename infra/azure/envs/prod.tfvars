# Production — every control from the implementation spec at its default value.
#
# This file is short because the defaults in variables.tf are the secure ones.
# What is written here is the environment's identity and the values that have no
# safe default, not a list of things being switched on.

subscription_id     = "a4b87216-b285-44f7-a7f3-54506c8ffcfb"
env                 = "prod"
location            = "eastus"
unique_suffix       = "fa7k"
owner               = "data4v"
cost_center         = "firmaware-prod"
data_classification = "confidential"

# network_isolation, registry_sku, key_vault_purge_protection,
# scores_immutability_locked and scores_retention_days are all left at their
# defaults: private endpoints on, Premium registry, purge protection on, and a
# locked seven-year immutability policy on the scores container.

compute_vm_size   = "Standard_DS3_v2"
compute_max_nodes = 4

# min_instances is 1 in prod, so there is no cold start on the first call of the
# day. That is the cost floor the architecture document warns about.
online_endpoint_enabled = true
online_instance_type    = "Standard_DS3_v2"

# One warm instance at all times. Stated explicitly rather than inherited: the
# deploy script's own fallback is 0, and a prod endpoint that scales to nothing
# turns the first request after a quiet period into a multi-minute cold start.
online_min_instances = 1

# No github_* settings here. bootstrap.sh creates the federated credential and
# defaults prod's OIDC subject to `environment:production`. Federating on the
# GitHub environment *instead of* the branch is what makes the required-reviewer
# gate load-bearing: a branch credential alongside it would be a second door into
# this identity that opens on any push to main, with no approval. bootstrap.sh
# creates one credential, and it is the environment one.

alerts_email   = "data4v@gmail.com"
monthly_budget = 300
