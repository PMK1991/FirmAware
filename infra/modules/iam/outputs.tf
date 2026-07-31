output "jobs_service_account_email" {
  value = google_service_account.jobs.email
}

output "deployer_service_account_email" {
  value = google_service_account.deployer.email
}

output "scheduler_service_account_email" {
  value = google_service_account.scheduler.email
}
