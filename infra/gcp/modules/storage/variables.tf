variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

variable "labels" {
  type = map(string)
}

variable "smoke_fixture_path" {
  type = string
}
