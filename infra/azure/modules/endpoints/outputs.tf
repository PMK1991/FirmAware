output "online_endpoint_name" {
  value = var.online_endpoint_enabled ? azapi_resource.online[0].name : ""
}

output "online_endpoint_id" {
  value = var.online_endpoint_enabled ? azapi_resource.online[0].id : ""
}

output "batch_endpoint_name" {
  value = azapi_resource.batch.name
}

output "batch_endpoint_id" {
  value = azapi_resource.batch.id
}
