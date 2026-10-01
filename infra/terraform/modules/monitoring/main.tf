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

# Monitoring for the ADSTA decision agent.
#
# Every metric and log-match alert here filters on jsonPayload.extra.event,
# emitted by agentic_dsta/core/telemetry.py. Event names and label keys are a
# contract between that module and this one: rename them together.

locals {
  # Base filter for application logs from the Cloud Run service.
  app_log_filter = <<-EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="${var.service_name}"
  EOT

  metric_prefix = "logging.googleapis.com/user"

  channels = concat(
    [for c in google_monitoring_notification_channel.email : c.id],
    var.additional_notification_channel_ids,
  )

  scheduler_job_filter = join(" OR ", [for j in var.scheduler_job_names : "\"${j}\""])

  common_labels = {
    app     = "adsta"
    service = var.service_name
  }

  # Links used in alert runbooks and outputs.
  logs_url      = "https://console.cloud.google.com/logs/query?project=${var.project_id}"
  dashboard_url = "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.adsta.id), length(split("/", google_monitoring_dashboard.adsta.id)) - 1)}?project=${var.project_id}"

  # Appended to every alert notification.
  runbook_footer = "**Investigate:** [ADSTA Operations dashboard](${local.dashboard_url}) | [Logs Explorer](${local.logs_url})"
}

# --- Notification channels ---------------------------------------------------

resource "google_monitoring_notification_channel" "email" {
  for_each     = toset(var.notification_emails)
  project      = var.project_id
  display_name = "${var.resource_prefix} alerts - ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }
  user_labels = local.common_labels
}

# --- Log-based metrics -------------------------------------------------------

resource "google_logging_metric" "runs" {
  project     = var.project_id
  name        = "adsta_runs"
  description = "One point per decision agent run (the heartbeat), labelled by outcome."
  filter      = <<-EOT
    ${local.app_log_filter}
    jsonPayload.extra.event="run_completed"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key         = "usecase"
      description = "GoogleAds or SA360"
    }
    labels {
      key         = "outcome"
      description = "success, partial, failed, aborted or noop"
    }
    labels {
      key         = "reason"
      description = "Why a run aborted, failed or did nothing"
    }
  }
  label_extractors = {
    usecase = "EXTRACT(jsonPayload.extra.usecase)"
    outcome = "EXTRACT(jsonPayload.extra.outcome)"
    reason  = "EXTRACT(jsonPayload.extra.reason)"
  }
}

resource "google_logging_metric" "run_duration" {
  project         = var.project_id
  name            = "adsta_run_duration_s"
  description     = "Wall-clock duration of each decision agent run, in seconds."
  filter          = google_logging_metric.runs.filter
  value_extractor = "EXTRACT(jsonPayload.extra.total_duration_s)"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "s"
    labels {
      key = "usecase"
    }
  }
  label_extractors = {
    usecase = "EXTRACT(jsonPayload.extra.usecase)"
  }
  bucket_options {
    # 1s .. ~3300s, enough to cover the 1800s scheduler deadline.
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 1.5
      scale              = 1
    }
  }
}

resource "google_logging_metric" "playbook_failures" {
  project     = var.project_id
  name        = "adsta_playbook_failures"
  description = "Playbook executions that raised, per campaign playbook."
  filter      = <<-EOT
    ${local.app_log_filter}
    jsonPayload.extra.event="playbook_failed"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key = "playbook_id"
    }
    labels {
      key = "error_class"
    }
  }
  label_extractors = {
    playbook_id = "EXTRACT(jsonPayload.extra.playbook_id)"
    error_class = "EXTRACT(jsonPayload.extra.error_class)"
  }
}

resource "google_logging_metric" "mutations" {
  project     = var.project_id
  name        = "adsta_mutations"
  description = "Advertising platform changes (applied, or suppressed by dry run)."
  filter      = <<-EOT
    ${local.app_log_filter}
    jsonPayload.extra.event="mutation_applied"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key         = "action"
      description = "budget, bidding, geo, campaign_status or asset_group"
    }
    labels {
      key         = "dry_run"
      description = "true if ADSTA_DRY_RUN suppressed the write"
    }
    labels {
      key = "usecase"
    }
  }
  label_extractors = {
    action  = "EXTRACT(jsonPayload.extra.action)"
    dry_run = "EXTRACT(jsonPayload.extra.dry_run)"
    usecase = "EXTRACT(jsonPayload.extra.usecase)"
  }
}

resource "google_logging_metric" "tool_errors" {
  project     = var.project_id
  name        = "adsta_tool_errors"
  description = "Failed tool calls and Gemini errors, per external dependency."
  filter      = <<-EOT
    ${local.app_log_filter}
    jsonPayload.extra.event=("tool_error" OR "model_error")
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    labels {
      key         = "dependency"
      description = "google_ads, sa360, weather, firestore, gemini or internal"
    }
    labels {
      key         = "error_class"
      description = "auth, quota, timeout, unavailable, not_found, invalid_argument or other"
    }
  }
  label_extractors = {
    dependency  = "EXTRACT(jsonPayload.extra.dependency)"
    error_class = "EXTRACT(jsonPayload.extra.error_class)"
  }
}

# --- Alert policies: critical ------------------------------------------------

# 1. Missed runs. PromQL is used instead of a metric-absence condition because
# absence conditions max out at 23.5h, which is shorter than the daily SA360
# schedule. Note: fires after first deploy until the first run completes.
resource "google_monitoring_alert_policy" "missed_runs" {
  for_each     = var.heartbeat_windows
  project      = var.project_id
  display_name = "${var.resource_prefix} - CRITICAL - Missed runs (${each.key})"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "No ${each.key} run_completed event in ${each.value}"
    condition_prometheus_query_language {
      query               = "absent_over_time(logging_googleapis_com:user_adsta_runs{monitored_resource=\"cloud_run_revision\",service_name=\"${var.service_name}\",usecase=\"${each.key}\"}[${each.value}])"
      duration            = "0s"
      evaluation_interval = "300s"
    }
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **No ${each.key} decision agent run has completed in ${each.value}.** Weather-driven changes are not being applied.

      1. Cloud Scheduler: confirm the job is ENABLED and check its last attempt status.
      2. Check the scheduler-failure alert, which usually fires first with the cause.
      3. [Logs](${local.logs_url}): `jsonPayload.extra.event="run_started"` to see if runs start but never finish (crash/OOM/timeout).
      4. Right after a fresh deploy, this fires until the first scheduled run completes.

      ${local.runbook_footer}
    EOT
  }
  depends_on = [google_logging_metric.runs]
}

# 2. Scheduler attempt failed: HTTP 5xx (failed/aborted run), timeout or auth.
resource "google_monitoring_alert_policy" "scheduler_failed" {
  project      = var.project_id
  display_name = "${var.resource_prefix} - CRITICAL - Scheduled run failed"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "Cloud Scheduler job attempt failed"
    condition_matched_log {
      filter = <<-EOT
        resource.type="cloud_scheduler_job"
        resource.labels.job_id=(${local.scheduler_job_filter})
        severity>=ERROR
      EOT
      label_extractors = {
        job_id = "EXTRACT(resource.labels.job_id)"
        status = "EXTRACT(jsonPayload.status)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = var.log_alert_rate_limit
    }
    auto_close = var.auto_close
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **Scheduler job `$${log.extracted_label.job_id}` failed** (status: $${log.extracted_label.status}).

      The service returns HTTP 500 when every playbook failed or the run aborted (missing CustomerInstructions/config).
      A DEADLINE_EXCEEDED status means the run exceeded the attempt deadline.

      1. [Logs](${local.logs_url}): `jsonPayload.extra.event="run_completed"` and look at `outcome` / `reason`.
      2. `reason=missing_instructions|missing_config` means a Firestore document is missing or the customer_id is wrong.
      3. `reason=all_playbooks_failed`: filter `jsonPayload.extra.event="playbook_failed"` for the `error_class`.
      4. Scheduler retries are disabled, so the next scheduled run is the retry.

      ${local.runbook_footer}
    EOT
  }
}

# 3. Change volume guard (spec 5.5): possible bad weather feed.
resource "google_monitoring_alert_policy" "change_guard" {
  project      = var.project_id
  display_name = "${var.resource_prefix} - CRITICAL - Change volume guard exceeded"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "change_guard_exceeded event"
    condition_matched_log {
      filter = <<-EOT
        ${local.app_log_filter}
        jsonPayload.extra.event="change_guard_exceeded"
      EOT
      label_extractors = {
        run_id = "EXTRACT(jsonPayload.extra.run_id)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = var.log_alert_rate_limit
    }
    auto_close = var.auto_close
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **Run `$${log.extracted_label.run_id}` changed more budgets than the configured cap.** This may mean a bad weather data feed.

      The guard only detects after the fact; changes have already been applied.
      1. Review Firestore `ChangeLog` rows where `runId == $${log.extracted_label.run_id}`.
      2. [Logs](${local.logs_url}): `jsonPayload.extra.event="mutation_applied" jsonPayload.extra.run_id="$${log.extracted_label.run_id}"`.
      3. If the changes are wrong, revert them in Google Ads and consider setting `ADSTA_DRY_RUN=true` until the cause is fixed.

      ${local.runbook_footer}
    EOT
  }
}

# 4. Auth failure: expired/revoked refresh token, missing developer token, IAM.
resource "google_monitoring_alert_policy" "auth_failure" {
  project      = var.project_id
  display_name = "${var.resource_prefix} - CRITICAL - Authentication failure"
  combiner     = "OR"
  severity     = "CRITICAL"
  conditions {
    display_name = "error_class=auth"
    condition_matched_log {
      filter = <<-EOT
        ${local.app_log_filter}
        jsonPayload.extra.event=("tool_error" OR "playbook_failed" OR "model_error")
        jsonPayload.extra.error_class="auth"
      EOT
      label_extractors = {
        dependency = "EXTRACT(jsonPayload.extra.dependency)"
        tool       = "EXTRACT(jsonPayload.extra.tool)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = var.log_alert_rate_limit
    }
    auto_close = var.auto_close
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **Authentication failed calling `$${log.extracted_label.dependency}`** (tool: `$${log.extracted_label.tool}`). Every run fails until this is fixed.

      - `invalid_grant` / `OAUTH_TOKEN_*`: the OAuth refresh token expired or was revoked. Regenerate it with `auth/` and update the secret in Secret Manager.
      - `DEVELOPER_TOKEN_*`: check the `GOOGLE_ADS_DEVELOPER_TOKEN` secret.
      - `USER_PERMISSION_DENIED` / 403: the user or service account lost access to the account or MCC.

      ${local.runbook_footer}
    EOT
  }
}

# --- Alert policies: warning -------------------------------------------------

# 5. Degraded run: some playbooks failed, or nothing was runnable (config).
resource "google_monitoring_alert_policy" "degraded_run" {
  project      = var.project_id
  display_name = "${var.resource_prefix} - WARNING - Run partially failed"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "run_completed with outcome=partial or no runnable playbooks"
    condition_matched_log {
      filter = <<-EOT
        ${local.app_log_filter}
        jsonPayload.extra.event="run_completed"
        (jsonPayload.extra.outcome="partial" OR jsonPayload.extra.reason="no_runnable_playbooks")
      EOT
      label_extractors = {
        run_id  = "EXTRACT(jsonPayload.extra.run_id)"
        outcome = "EXTRACT(jsonPayload.extra.outcome)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = var.log_alert_rate_limit
    }
    auto_close = var.auto_close
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **Run `$${log.extracted_label.run_id}` did not fully succeed** (outcome: $${log.extracted_label.outcome}).

      - partial: [Logs](${local.logs_url}) `jsonPayload.extra.event="playbook_failed" jsonPayload.extra.run_id="$${log.extracted_label.run_id}"`.
      - noop / no_runnable_playbooks: no campaign resolved to a playbook. Check `assetGroupTokens`, playbook ids and campaign params in Firestore.

      ${local.runbook_footer}
    EOT
  }
}

# 6. Error spike for one dependency (weather API, Gemini quota, Firestore, ...).
resource "google_monitoring_alert_policy" "dependency_errors" {
  project      = var.project_id
  display_name = "${var.resource_prefix} - WARNING - Dependency error spike"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "Tool/model errors > ${var.tool_error_threshold_per_hour}/h for a dependency"
    condition_threshold {
      filter          = "metric.type=\"${local.metric_prefix}/${google_logging_metric.tool_errors.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.tool_error_threshold_per_hour
      duration        = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.dependency"]
      }
      trigger {
        count = 1
      }
    }
  }
  alert_strategy {
    auto_close = var.auto_close
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **One external dependency is returning many errors.** The `dependency` label on the incident names it.

      [Logs](${local.logs_url}): `jsonPayload.extra.event=("tool_error" OR "model_error")` and group by `error_class`.
      - gemini + quota: Vertex AI 429s. Consider `gemini_location = "global"` or a quota increase.
      - weather: Weather API key/quota or outage. Decisions will fall back or be skipped.
      - invalid_argument: usually the model calling a tool badly, so review the playbook instructions.

      ${local.runbook_footer}
    EOT
  }
  depends_on = [google_logging_metric.tool_errors]
}

# 7. Run approaching the scheduler attempt deadline.
resource "google_monitoring_alert_policy" "slow_run" {
  project      = var.project_id
  display_name = "${var.resource_prefix} - WARNING - Run near timeout"
  combiner     = "OR"
  severity     = "WARNING"
  conditions {
    display_name = "Run took longer than ${var.run_duration_warning_seconds}s"
    condition_matched_log {
      filter = <<-EOT
        ${local.app_log_filter}
        jsonPayload.extra.event="run_completed"
        jsonPayload.extra.total_duration_s > ${var.run_duration_warning_seconds}
      EOT
      label_extractors = {
        run_id = "EXTRACT(jsonPayload.extra.run_id)"
      }
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = var.log_alert_rate_limit
    }
    auto_close = var.auto_close
  }
  notification_channels = local.channels
  user_labels           = local.common_labels
  documentation {
    mime_type = "text/markdown"
    content   = <<-EOT
      **Run `$${log.extracted_label.run_id}` took more than ${var.run_duration_warning_seconds}s.** Campaigns run one after another in a single request, so adding campaigns will soon hit the scheduler deadline (DEADLINE_EXCEEDED).

      Options: cut the number of campaigns per account, raise `sa_run_sse_scheduler_job_attempt_deadline` (max 1800s), or split accounts across jobs.

      ${local.runbook_footer}
    EOT
  }
}
