output "artifact_registry_repository" {
  value = google_artifact_registry_repository.firmaware.name
}

output "artifact_registry_host" {
  value = "${var.region}-docker.pkg.dev"
}
