# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# Enable required APIs for the project
resource "google_project_service" "apis" {
  for_each           = toset(var.gcp_apis)
  service            = each.key
  disable_on_destroy = false
}


locals {
  service_name                       = "${var.resource_prefix}-app"
  run_sa_account_id                  = "${var.resource_prefix}-runner"
  firestore_database_name            = "${var.resource_prefix}-firestore"
  artifact_repository_id             = "${var.resource_prefix}-repo"

  sa_combined_scheduler_job_name     = "${var.resource_prefix}-sa-combined-job"
  google_ads_combined_scheduler_job_name = "${var.resource_prefix}-ga-combined-job"

  googleads_primary_customer_id = replace(var.googleads_customer_id, "-", "")
  googleads_additional_ids      = [for id in keys(var.googleads_additional_customers) : replace(id, "-", "")]

  # One Google Ads scheduler job per account. "primary" is the original
  # single-account job (googleads_customer_id, <prefix>-ga-combined-job). Its
  # key is a literal so the moved block below keeps existing deployments' job.
  # The primary is never duplicated from the additional map; a precondition on
  # the job reports that mistake instead.
  google_ads_jobs = merge(
    {
      primary = {
        name             = local.google_ads_combined_scheduler_job_name
        description      = "Combined job to init session and run agent for Google Ads"
        customer_id      = local.googleads_primary_customer_id
        schedule         = var.googleads_scheduler_schedule
        time_zone        = var.sa_run_sse_scheduler_job_timezone
        heartbeat_window = tostring(null)
      }
    },
    {
      for id, cfg in var.googleads_additional_customers : replace(id, "-", "") => {
        name             = "${var.resource_prefix}-ga-${replace(id, "-", "")}-job"
        description      = "Combined job to init session and run agent for Google Ads customer ${replace(id, "-", "")}"
        customer_id      = replace(id, "-", "")
        schedule         = coalesce(try(cfg.schedule, null), var.googleads_scheduler_schedule)
        time_zone        = coalesce(try(cfg.time_zone, null), var.sa_run_sse_scheduler_job_timezone)
        heartbeat_window = try(cfg.heartbeat_window, null)
      } if replace(id, "-", "") != local.googleads_primary_customer_id
    },
  )
}

# Service Account for Cloud Run
resource "google_service_account" "run_sa" {
  project      = var.project_id
  account_id   = local.run_sa_account_id
  display_name = var.run_sa_display_name
}

# IAM roles for Service Account
resource "google_project_iam_member" "run_sa_roles" {
  for_each = toset(var.run_sa_roles)
  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.run_sa.email}"
}

#--- Modules ---

module "firestore" {
  source        = "./modules/firestore"
  project_id    = var.project_id
  location_id   = var.region
  database_name = local.firestore_database_name
  database_type = var.firestore_database_type

  depends_on = [google_project_service.apis]
}

module "secret_manager" {
  source                = "./modules/secret_manager"
  project_id            = var.project_id
  service_account_email = google_service_account.run_sa.email
  additional_secrets    = var.additional_secrets
  secret_values         = var.secret_values

  depends_on = [google_project_service.apis]
}

# NOTE: The API Hub module was removed. External signals are now fetched by
# calling weather.googleapis.com directly from WeatherSignalsToolset, so the
# dynamic API discovery layer is no longer needed.



module "artifact_registry" {
  source        = "./modules/artifact_registry"
  project_id    = var.project_id
  location      = var.region
  repository_id = local.artifact_repository_id
  description   = var.artifact_repository_description
  format        = var.artifact_repository_format

  depends_on = [google_project_service.apis]
}

module "cloud_run_service" {
  source                = "./modules/cloud_run_service"
  service_name          = local.service_name
  project_id            = var.project_id
  location              = var.location != "" ? var.location : var.region
  image_url             = var.image_url != "" ? var.image_url : var.run_service_default_image_url
  container_port        = var.container_port
  allow_unauthenticated = var.allow_unauthenticated
  service_account_email = google_service_account.run_sa.email
  request_timeout       = var.cloud_run_request_timeout
  cpu                   = var.cloud_run_cpu
  memory                = var.cloud_run_memory

  env_vars = merge(var.run_service_env_vars, {
    GOOGLE_CLOUD_PROJECT          = var.project_id
    GOOGLE_CLOUD_LOCATION         = var.region
    # Kept separate from var.region on purpose: Cloud Run runs in us-central1,
    # but no Gemini 3.x model is served from a single US region, so model calls
    # must target the `us` multi-region (or `global`) endpoint.
    GEMINI_LOCATION               = var.gemini_location
    GEMINI_MODEL                  = var.gemini_model
    FIRESTORE_DB                  = local.firestore_database_name
    GOOGLE_ADS_FORCE_USER_CREDS = var.google_ads_force_user_creds
    SA360_FORCE_USER_CREDS      = var.sa360_force_user_creds
    GOOGLE_ADS_LOGIN_CUSTOMER_ID = var.google_ads_login_customer_id
    PYTHONUNBUFFERED              = "1"
    LOG_LEVEL                     = var.log_level
    ADSTA_MAX_CONCURRENT_CAMPAIGNS = tostring(var.max_concurrent_campaigns)
    ADSTA_PLAYBOOK_TIMEOUT_SECONDS = tostring(var.playbook_timeout_seconds)
  })
  secret_env_vars = { for secret in module.secret_manager.secret_ids : secret => { name = secret, version = "latest" } }
  depends_on = [google_project_service.apis, module.secret_manager]
}


# Combined Scheduler Job for Decision Agent
resource "google_cloud_scheduler_job" "sa_combined_job" {
  # Optional: with enable_sa360 = false no SA360 run is scheduled. Otherwise a
  # job with a placeholder customer ID would fail every day and page the
  # scheduler-failure alert.
  count            = var.enable_sa360 ? 1 : 0
  project          = var.project_id
  region           = var.region
  name             = local.sa_combined_scheduler_job_name
  description      = "Combined job to init session and run agent for SA360"
  schedule         = var.sa360_scheduler_schedule
  time_zone        = var.sa_run_sse_scheduler_job_timezone
  attempt_deadline = var.sa_run_sse_scheduler_job_attempt_deadline

  retry_config {
    retry_count          = 0
    max_retry_duration   = "0s"
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
  }

  http_target {
    http_method = "POST"
    uri         = "${module.cloud_run_service.service_url}/scheduler/init_and_run"
    body = base64encode(jsonencode({
      app_name = "decision_agent"
      user_id  = google_service_account.run_sa.email
      customer_id = var.sa360_customer_id
      usecase = "SA360"
    }))
    headers = {
      "Content-Type" = "application/json"
      "User-Agent"   = "Google-Cloud-Scheduler"
    }

    oidc_token {
      service_account_email = google_service_account.run_sa.email
      audience              = "${module.cloud_run_service.service_url}/scheduler/init_and_run"
    }
  }
}


# Combined Scheduler Job for Decision Agent: one per Google Ads account.
resource "google_cloud_scheduler_job" "google_ads_combined_job" {
  for_each         = local.google_ads_jobs
  project          = var.project_id
  region           = var.region
  name             = each.value.name
  description      = each.value.description
  schedule         = each.value.schedule
  time_zone        = each.value.time_zone
  attempt_deadline = var.sa_run_sse_scheduler_job_attempt_deadline

  retry_config {
    retry_count          = 0
    max_retry_duration   = "0s"
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
  }

  http_target {
    http_method = "POST"
    uri         = "${module.cloud_run_service.service_url}/scheduler/init_and_run"
    body = base64encode(jsonencode({
      app_name = "decision_agent"
      user_id  = google_service_account.run_sa.email
      customer_id = each.value.customer_id
      usecase = "GoogleAds"
    }))
    headers = {
      "Content-Type" = "application/json"
      "User-Agent"   = "Google-Cloud-Scheduler"
    }

    oidc_token {
      service_account_email = google_service_account.run_sa.email
      audience              = "${module.cloud_run_service.service_url}/scheduler/init_and_run"
    }
  }

  lifecycle {
    precondition {
      condition     = !contains(local.googleads_additional_ids, local.googleads_primary_customer_id)
      error_message = "googleads_customer_id ${var.googleads_customer_id} is also listed in googleads_additional_customers. List each account once."
    }
  }
}

# The Google Ads job became one job per account; keep the existing job as the
# "primary" entry instead of destroying and recreating it.
moved {
  from = google_cloud_scheduler_job.google_ads_combined_job
  to   = google_cloud_scheduler_job.google_ads_combined_job["primary"]
}

# The SA360 job gained `count`; keep existing SA360 deployments in place.
moved {
  from = google_cloud_scheduler_job.sa_combined_job
  to   = google_cloud_scheduler_job.sa_combined_job[0]
}

# --- Monitoring: log-based metrics, alerts and dashboard ---
module "monitoring" {
  source          = "./modules/monitoring"
  count           = var.enable_monitoring ? 1 : 0
  project_id      = var.project_id
  resource_prefix = var.resource_prefix
  service_name    = module.cloud_run_service.name
  scheduler_job_names = concat(
    [for job in google_cloud_scheduler_job.google_ads_combined_job : job.name],
    [for job in google_cloud_scheduler_job.sa_combined_job : job.name],
  )
  notification_emails                 = var.alert_notification_emails
  additional_notification_channel_ids = var.alert_additional_notification_channel_ids
  # No SA360 heartbeat when SA360 is disabled, whatever the windows map says.
  heartbeat_windows = {
    for usecase, window in var.monitoring_heartbeat_windows : usecase => window
    if usecase != "SA360" || var.enable_sa360
  }
  # One missed-run heartbeat per Google Ads account, so one account that stops
  # running is caught while the others keep running. Removing the GoogleAds key
  # from monitoring_heartbeat_windows disables them all.
  googleads_customer_heartbeats = {
    for key, job in local.google_ads_jobs :
    job.customer_id => coalesce(job.heartbeat_window, lookup(var.monitoring_heartbeat_windows, "GoogleAds", "13h"))
    if job.customer_id != "" && contains(keys(var.monitoring_heartbeat_windows), "GoogleAds")
  }
  # Warn at 80% of the scheduler attempt deadline.
  run_duration_warning_seconds = floor(tonumber(trimsuffix(var.sa_run_sse_scheduler_job_attempt_deadline, "s")) * 0.8)

  depends_on = [google_project_service.apis]
}

output "monitoring_dashboard_url" {
  description = "ADSTA Operations dashboard"
  value       = var.enable_monitoring ? module.monitoring[0].dashboard_url : null
}

# --- Scheduler Verification Outputs ---
output "google_ads_scheduler_jobs" {
  description = "Google Ads scheduler job per account: customer ID => job name, schedule and time zone."
  value = {
    for key, job in google_cloud_scheduler_job.google_ads_combined_job :
    local.google_ads_jobs[key].customer_id => "${job.name} (${job.schedule}, ${job.time_zone})"
  }
}

output "scheduler_target_uri" {
  description = "The target URI being used for scheduler jobs"
  value       = "${module.cloud_run_service.service_url}/scheduler/init_and_run"
}

output "scheduler_oidc_service_account" {
  description = "The service account email being used for scheduler OIDC tokens"
  value       = google_service_account.run_sa.email
}
