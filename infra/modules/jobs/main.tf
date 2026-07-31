locals {
  job_config = {
    validate = {
      args        = ["validate", "--mode", "training"]
      cpu         = "1"
      memory      = "2Gi"
      timeout     = "600s"
      max_retries = 1
    }
    train = {
      args        = ["train"]
      cpu         = "4"
      memory      = "8Gi"
      timeout     = "1800s"
      max_retries = 0
    }
    predict = {
      args        = ["predict"]
      cpu         = "1"
      memory      = "2Gi"
      timeout     = "600s"
      max_retries = 1
    }
  }

  common_env = {
    FIRMAWARE_DATA_URI             = "gs://${var.data_bucket_name}"
    FIRMAWARE_ARTIFACTS_URI        = "gs://${var.artifacts_bucket_name}"
    FIRMAWARE_SCORES_URI           = "gs://${var.scores_bucket_name}/scores"
    FIRMAWARE_PROMOTED_BY          = "cloud-run-job:${var.env}"
    MLFLOW_TRACKING_URI            = "sqlite:////tmp/firmaware-mlflow.db"
    FIRMAWARE_MLFLOW_ARTIFACT_ROOT = "/tmp/firmaware-mlruns"
  }
}

resource "google_cloud_run_v2_job" "firmaware" {
  for_each = local.job_config

  project             = var.project_id
  name                = "firmaware-${var.env}-${each.key}"
  location            = var.region
  labels              = var.labels
  deletion_protection = var.env == "prod"

  template {
    labels = var.labels

    template {
      service_account = var.jobs_service_account_email
      timeout         = each.value.timeout
      max_retries     = each.value.max_retries

      containers {
        image = var.image_digest
        args  = each.value.args

        resources {
          limits = {
            cpu    = each.value.cpu
            memory = each.value.memory
          }
        }

        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }

    }
  }

  lifecycle {
    ignore_changes = [launch_stage]
  }
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_predict_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.firmaware["predict"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_service_account_email}"
}

resource "google_cloud_scheduler_job" "predict" {
  project          = var.project_id
  region           = var.region
  name             = "firmaware-${var.env}-nightly-predict"
  description      = "Nightly FirmAware ${var.env} batch prediction"
  schedule         = var.schedule
  time_zone        = var.schedule_time_zone
  paused           = !var.scheduler_enabled
  attempt_deadline = "600s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.firmaware["predict"].name}:run"

    oauth_token {
      service_account_email = var.scheduler_service_account_email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_predict_invoker]
}

resource "google_logging_metric" "job_failure" {
  project = var.project_id
  name    = "firmaware_${var.env}_job_failure"
  filter = join(" ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=~\"firmaware-${var.env}-(validate|train|predict)\"",
    "severity>=ERROR",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "contract_violation" {
  project = var.project_id
  name    = "firmaware_${var.env}_contract_violation"
  filter = join(" ", [
    "resource.type=\"cloud_run_job\"",
    "resource.labels.job_name=~\"firmaware-${var.env}-(validate|train|predict)\"",
    "(textPayload:\"[error]\" OR jsonPayload.exitCode=1)",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_logging_metric" "smoke_contract_violation" {
  project = var.project_id
  name    = "firmaware_${var.env}_smoke_contract_violation"
  filter = join(" ", [
    "log_id(\"firmaware_smoke_test\")",
    "jsonPayload.environment=\"${var.env}\"",
    "jsonPayload.exitCode=1",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_notification_channel" "email" {
  count = var.alerts_email == "" ? 0 : 1

  project      = var.project_id
  display_name = "FirmAware ${var.env} deployment alerts"
  type         = "email"
  labels = {
    email_address = var.alerts_email
  }
}

resource "google_monitoring_alert_policy" "job_failures" {
  project      = var.project_id
  display_name = "FirmAware ${var.env} job or contract failure"
  combiner     = "OR"
  enabled      = true

  notification_channels = (
    var.alerts_email == ""
    ? []
    : [google_monitoring_notification_channel.email[0].name]
  )

  conditions {
    display_name = "Cloud Run completed execution reports failure"

    condition_threshold {
      filter          = "metric.type=\"run.googleapis.com/job/completed_execution_count\" AND resource.type=\"cloud_run_job\" AND metric.label.result=\"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  conditions {
    display_name = "Cloud Run Job execution failure"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.job_failure.name}\" AND resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  conditions {
    display_name = "Deployment smoke contract violation"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.smoke_contract_violation.name}\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  conditions {
    display_name = "FirmAware contract violation"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.contract_violation.name}\" AND resource.type=\"cloud_run_job\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  alert_strategy {
    auto_close = "1800s"
  }
}
