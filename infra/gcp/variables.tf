variable "project_id" {
  description = "GCP project that owns this environment."
  type        = string
}

variable "region" {
  description = "Regional location for Artifact Registry and Cloud Run Jobs."
  type        = string
  default     = "us-central1"
}

variable "env" {
  description = "Deployment environment."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.env)
    error_message = "env must be dev or prod."
  }
}

variable "image_digest" {
  description = "Full immutable Artifact Registry image reference ending in @sha256:..."
  type        = string

  validation {
    condition     = can(regex("^.+@sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be a full image reference pinned by sha256 digest."
  }
}

variable "alerts_email" {
  description = "Monitoring notification email; empty disables email delivery."
  type        = string
  default     = ""
}

variable "state_bucket_name" {
  description = "Manually bootstrapped GCS Terraform state bucket."
  type        = string
}

variable "github_owner" {
  description = "GitHub repository owner used by WIF."
  type        = string
  default     = "PMK1991"
}

variable "github_repo" {
  description = "GitHub repository name used by WIF."
  type        = string
  default     = "FirmAware"
}

variable "wif_pool_id" {
  description = "Bootstrapped Workload Identity Pool ID."
  type        = string
  default     = "github-actions"
}

variable "schedule" {
  description = "Cloud Scheduler cron expression for prediction."
  type        = string
  default     = "0 2 * * *"
}

variable "schedule_time_zone" {
  description = "IANA time zone for the nightly prediction schedule."
  type        = string
  default     = "Etc/UTC"
}

variable "scheduler_enabled" {
  description = "Whether the nightly predictor schedule is active."
  type        = bool
  default     = true
}
