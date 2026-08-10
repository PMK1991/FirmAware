locals {
  buckets = {
    data = {
      versioning = true
    }
    artifacts = {
      versioning = true
    }
    scores = {
      versioning = false
    }
  }
}

resource "google_storage_bucket" "firmaware" {
  for_each = local.buckets

  project                     = var.project_id
  name                        = "firmaware-${var.env}-${each.key}"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  deletion_policy             = var.env == "prod" ? "PREVENT" : "DELETE"
  labels                      = var.labels

  versioning {
    enabled = each.value.versioning
  }

  dynamic "lifecycle_rule" {
    for_each = each.value.versioning ? [1] : []
    content {
      action {
        type = "Delete"
      }
      condition {
        days_since_noncurrent_time = 90
        with_state                 = "ARCHIVED"
      }
    }
  }
}

resource "google_storage_bucket_object" "smoke_fixture" {
  name         = "smoke/upcoming_smoke.csv"
  bucket       = google_storage_bucket.firmaware["data"].name
  source       = var.smoke_fixture_path
  content_type = "text/csv"
}
