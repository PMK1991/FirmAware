variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

variable "image_digest" {
  type = string
}

variable "labels" {
  type = map(string)
}

variable "data_bucket_name" {
  type = string
}

variable "artifacts_bucket_name" {
  type = string
}

variable "scores_bucket_name" {
  type = string
}

variable "jobs_service_account_email" {
  type = string
}

variable "scheduler_service_account_email" {
  type = string
}

variable "schedule" {
  type = string
}

variable "schedule_time_zone" {
  type = string
}

variable "scheduler_enabled" {
  type = bool
}

variable "alerts_email" {
  type = string
}
