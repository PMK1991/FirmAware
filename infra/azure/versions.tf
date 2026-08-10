terraform {
  required_version = ">= 1.7"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.14"
    }
    # Managed online and batch endpoints have no azurerm resource. azapi calls
    # the same ARM API Terraform would, so the endpoints stay in state and in
    # the plan instead of being created out of band by a script.
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    # Used for one thing only: waiting out Azure RBAC propagation between
    # granting the workspace identity its roles and creating the workspace that
    # exercises them. See time_sleep.role_propagation in main.tf.
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  # The storage account has shared_access_key_enabled = false, so the provider
  # cannot reach its data plane the default way: it falls back to a shared key
  # that does not exist and fails with KeyBasedAuthenticationNotPermitted while
  # merely waiting for the account to come up. This makes it use the same AAD
  # identity it already authenticates the control plane with.
  storage_use_azuread = true

  features {
    key_vault {
      # Soft delete is the point of purge protection; letting Terraform purge on
      # destroy would defeat it. Recovery of a soft-deleted vault is deliberate.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}

provider "azapi" {
  subscription_id = var.subscription_id
}
