terraform {
  # Partial configuration: bootstrap.sh creates the state account and writes
  # the matching -backend-config, so no subscription-specific name is committed.
  #
  #   terraform init -backend-config=envs/dev.backend.hcl
  #
  # Locking is the blob lease the azurerm backend takes automatically; there is
  # no separate lock table to provision or to leak.
  backend "azurerm" {
    use_azuread_auth = true
  }
}
