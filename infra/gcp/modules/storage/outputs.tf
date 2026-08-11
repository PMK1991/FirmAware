output "data_bucket_name" {
  value = google_storage_bucket.firmaware["data"].name
}

output "artifacts_bucket_name" {
  value = google_storage_bucket.firmaware["artifacts"].name
}

output "scores_bucket_name" {
  value = google_storage_bucket.firmaware["scores"].name
}
