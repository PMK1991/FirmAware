variable "env" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "workspace_id" { type = string }
variable "endpoint_identity_id" { type = string }
variable "online_endpoint_enabled" { type = bool }
variable "network_isolation" { type = bool }
