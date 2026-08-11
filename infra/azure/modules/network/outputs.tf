output "vnet_id" {
  value = azurerm_virtual_network.this.id
}

output "compute_subnet_id" {
  value = azurerm_subnet.compute.id
}

output "private_endpoint_subnet_id" {
  value = azurerm_subnet.private_endpoints.id
}

output "scoring_subnet_id" {
  value = azurerm_subnet.scoring.id
}
