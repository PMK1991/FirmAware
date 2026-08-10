locals {
  services = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "firmaware" {
  project       = var.project_id
  location      = var.region
  repository_id = "firmaware"
  description   = "Immutable FirmAware batch job images"
  format        = "DOCKER"
  labels        = var.labels

  docker_config {
    immutable_tags = true
  }

  depends_on = [google_project_service.required]
}
