data "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = var.wif_pool_id
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = data.google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub ${var.github_owner}/${var.github_repo}"
  description                        = "Repository-scoped GitHub Actions OIDC provider."

  attribute_mapping = {
    "google.subject"        = "assertion.sub"
    "attribute.actor"       = "assertion.actor"
    "attribute.environment" = "assertion.environment"
    "attribute.repository"  = "assertion.repository"
    "attribute.ref"         = "assertion.ref"
  }
  attribute_condition = (
    var.env == "prod"
    ? "assertion.repository == \"${var.github_owner}/${var.github_repo}\" && assertion.environment == \"production\" && assertion.ref.startsWith(\"refs/tags/v\")"
    : "assertion.repository == \"${var.github_owner}/${var.github_repo}\" && ((assertion.environment == \"development\" && assertion.ref == \"refs/heads/main\") || (assertion.environment == \"production\" && assertion.ref.startsWith(\"refs/tags/v\")))"
  )

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "jobs" {
  project      = var.project_id
  account_id   = "sa-firmaware-jobs"
  display_name = "FirmAware ${var.env} Cloud Run Jobs"
  description  = "Runtime identity for FirmAware validate, train, and predict jobs."
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "sa-firmaware-deployer"
  display_name = "FirmAware ${var.env} GitHub deployer"
  description  = "Keyless GitHub Actions deployment identity."
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "sa-firmaware-scheduler"
  display_name = "FirmAware ${var.env} scheduler"
  description  = "Invoker identity restricted to the predict job."
}

resource "google_project_iam_member" "jobs_project_roles" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_storage_bucket_iam_member" "jobs_data_reader" {
  bucket = var.data_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_storage_bucket_iam_member" "jobs_artifacts_admin" {
  bucket = var.artifacts_bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.jobs.email}"
}

# Creator + viewer permits immutable writes and smoke-test listing, but not delete.
resource "google_storage_bucket_iam_member" "jobs_scores_roles" {
  for_each = toset([
    "roles/storage.objectCreator",
    "roles/storage.objectViewer",
  ])

  bucket = var.scores_bucket_name
  role   = each.value
  member = "serviceAccount:${google_service_account.jobs.email}"
}

resource "google_project_iam_member" "deployer_project_roles" {
  for_each = toset([
    "roles/artifactregistry.admin",
    "roles/cloudscheduler.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/logging.admin",
    "roles/monitoring.editor",
    "roles/resourcemanager.projectIamAdmin",
    "roles/run.admin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_uses_jobs_sa" {
  service_account_id = google_service_account.jobs.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_uses_scheduler_sa" {
  service_account_id = google_service_account.scheduler.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_storage_bucket_iam_member" "deployer_state_admin" {
  bucket = var.state_bucket_name
  role   = "roles/storage.admin"
  member = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "github_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${data.google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_owner}/${var.github_repo}"

  depends_on = [google_iam_workload_identity_pool_provider.github]
}
