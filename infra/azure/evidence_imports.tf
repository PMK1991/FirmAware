# One-time adoption of the dev evidence resources.
#
# They were first created by hand with the Azure CLI on 2026-10-21, to prove
# the evidence layer end to end before it was codified (tagged managed_by =
# az-cli). These blocks bring exactly those resources under this root on the
# next apply instead of creating duplicates -- which for Search would fail
# outright, because a subscription may hold only one Free-tier service.
#
# Scoped to dev by for_each: prod has nothing to adopt and creates its own.
# Once applied the blocks are inert, and can be deleted after the first
# successful dev apply.

locals {
  evidence_adopt = var.env == "dev" ? {
    account = "/subscriptions/${var.subscription_id}/resourceGroups/${local.resource_group_name}/providers/Microsoft.CognitiveServices/accounts/ais-${local.name_prefix}-${var.unique_suffix}"
    search  = "/subscriptions/${var.subscription_id}/resourceGroups/${local.resource_group_name}/providers/Microsoft.Search/searchServices/srch-${local.name_prefix}-${var.unique_suffix}"
  } : {}
  evidence_adopt_models = var.env == "dev" ? {
    for model in ["Llama-3.3-70B-Instruct", "text-embedding-3-small", "gpt-oss-120b"] :
    model => "${local.evidence_adopt["account"]}/deployments/${model}"
  } : {}
}

import {
  for_each = { for k, v in local.evidence_adopt : k => v if k == "account" }
  to       = module.evidence.azurerm_cognitive_account.foundry
  id       = each.value
}

import {
  for_each = { for k, v in local.evidence_adopt : k => v if k == "search" }
  to       = module.evidence.azurerm_search_service.this
  id       = each.value
}

import {
  for_each = local.evidence_adopt_models
  to       = module.evidence.azurerm_cognitive_deployment.model[each.key]
  id       = each.value
}
