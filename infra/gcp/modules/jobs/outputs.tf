output "job_names" {
  value = {
    for name, job in google_cloud_run_v2_job.firmaware : name => job.name
  }
}
