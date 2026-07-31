locals {
  labels = {
    app        = "firmaware"
    env        = var.env
    managed_by = "terraform"
  }
}

module "foundation" {
  source = "./modules/foundation"

  project_id = var.project_id
  region     = var.region
  labels     = local.labels
}

module "storage" {
  source = "./modules/storage"

  project_id         = var.project_id
  region             = var.region
  env                = var.env
  labels             = local.labels
  smoke_fixture_path = "${path.module}/../deploy/fixtures/upcoming_smoke.csv"

  depends_on = [module.foundation]
}

module "iam" {
  source = "./modules/iam"

  project_id            = var.project_id
  env                   = var.env
  data_bucket_name      = module.storage.data_bucket_name
  artifacts_bucket_name = module.storage.artifacts_bucket_name
  scores_bucket_name    = module.storage.scores_bucket_name
  state_bucket_name     = var.state_bucket_name
  github_owner          = var.github_owner
  github_repo           = var.github_repo
  wif_pool_id           = var.wif_pool_id

  depends_on = [module.foundation, module.storage]
}

module "jobs" {
  source = "./modules/jobs"

  project_id                      = var.project_id
  region                          = var.region
  env                             = var.env
  image_digest                    = var.image_digest
  labels                          = local.labels
  data_bucket_name                = module.storage.data_bucket_name
  artifacts_bucket_name           = module.storage.artifacts_bucket_name
  scores_bucket_name              = module.storage.scores_bucket_name
  jobs_service_account_email      = module.iam.jobs_service_account_email
  scheduler_service_account_email = module.iam.scheduler_service_account_email
  schedule                        = var.schedule
  schedule_time_zone              = var.schedule_time_zone
  scheduler_enabled               = var.scheduler_enabled
  alerts_email                    = var.alerts_email

  depends_on = [module.foundation, module.storage, module.iam]
}

output "artifact_registry_repository" {
  value = module.foundation.artifact_registry_repository
}

output "job_names" {
  value = module.jobs.job_names
}

output "bucket_names" {
  value = {
    data      = module.storage.data_bucket_name
    artifacts = module.storage.artifacts_bucket_name
    scores    = module.storage.scores_bucket_name
  }
}

output "deployer_service_account_email" {
  value = module.iam.deployer_service_account_email
}
