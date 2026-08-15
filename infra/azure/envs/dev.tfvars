# Development environment — cost-reduced, deliberately and visibly.
#
# Three controls from the implementation spec are relaxed here. Each one is a
# named variable rather than a silent default, each is scoped to this file, and
# prod.tfvars turns all three back on. The reasoning is in infra/azure/README.md
# under "Where dev deviates".
#
# What does NOT relax, in either environment:
#   shared_access_key_enabled = false      no account key exists to leak
#   admin_enabled             = false      no registry password exists to leak
#   append-only RBAC on scores             the runtime cannot delete evidence
#   immutability policy on scores          nor can anyone else, for the window
#   no service-principal passwords         CI federates, it does not authenticate
#   OIDC subject scoped to one env         a dev token cannot address prod
#   diagnostics on every resource          90-day retention
#   the six mandatory tags                 enforced by policy

subscription_id = "a4b87216-b285-44f7-a7f3-54506c8ffcfb"
env             = "dev"
location        = "eastus"

# One group for the whole environment, so everything is visible in one place.
# The Terraform state account and CI identity stay in rg-firmaware-tfstate: if
# they shared this group, destroying dev would delete the state that describes
# it, and Terraform would have to adopt a group it did not create.
resource_group_name = "firmAware"
unique_suffix       = "fa7k"
owner               = "data4v"
cost_center         = "firmaware-rnd"
data_classification = "internal"

# RELAXED 1 — no private endpoints, no managed VNet isolation.
# Five private endpoints at ~$7.30 each plus the Premium registry they require
# cost more per month than everything else in dev combined. Authentication is
# unaffected: AAD is still the only way in, because shared keys are disabled.
network_isolation = false

# The direct consequence of RELAXED 1, spelled out rather than implied: with no
# private endpoints the compute cluster and CI reach storage over its public
# path, so the firewall cannot deny by default without locking dev out of its
# own data. Prod never reaches this line -- isolation forces Deny regardless.
storage_network_default_action = "Allow"

# RELAXED 2 — Basic registry.
# Private link, immutable tags, quarantine and retention policies are all
# Premium-only. In dev the compensating gate is the blocking Trivy scan in CI,
# which refuses to promote an image with a HIGH or CRITICAL finding.
registry_sku = "Basic"

# RELAXED 3 — no purge protection on the vault.
# A purge-protected vault reserves its name for 90 days after deletion, which
# makes a throwaway environment impossible to tear down and rebuild.
key_vault_purge_protection = false

# Immutability itself stays on; only the lock comes off, so dev can still be
# destroyed. The append-only role assignment is identical to prod either way.
scores_retention_days      = 1
scores_immutability_locked = false

# Training scales to zero between runs, so the cluster is free when idle.
compute_vm_size   = "Standard_DS3_v2"
compute_max_nodes = 2

# Managed online endpoints cannot scale to zero: one instance bills continuously
# at roughly $70/month. Left off by default and turned on for a smoke test.
online_endpoint_enabled = false
online_instance_type    = "Standard_DS2_v2"

# RELAXATION (cost). Prod holds one warm instance so no user pays a multi-minute
# cold start. Dev has no users: a cold start on the occasional smoke test is
# free to tolerate, and 0 means an idle dev endpoint costs nothing even when
# online_endpoint_enabled is flipped on for a test.
online_min_instances = 0

# No github_* settings here. The federated credential is created by
# infra/azure/bootstrap.sh, which defaults dev's OIDC subject to
# `environment:dev` -- matching the `environment:` the deploy workflow declares.
# Dev's GitHub environment carries no required reviewers; it exists for the
# subject claim and to scope the AZURE_* variables, not to gate anything.

alerts_email   = "data4v@gmail.com"
monthly_budget = 40

# The hosted page scales to zero. It is a read-only view whose only regular
# visitor is the deploy smoke test, so an idle dev page costs nothing and a
# visitor pays a cold start of roughly half a minute. app_image is not set here:
# it is a digest, so it comes from the build rather than from a file.
app_min_replicas = 0