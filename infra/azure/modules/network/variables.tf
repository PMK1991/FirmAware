variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "tags" { type = map(string) }

variable "address_space" {
  type    = string
  default = "10.42.0.0/16"
}

variable "private_endpoint_targets" {
  type = map(object({
    resource_id = string
    subresource = string
    dns_zone    = string
  }))
  description = "One private endpoint per entry, each with its own Private DNS zone."
}
