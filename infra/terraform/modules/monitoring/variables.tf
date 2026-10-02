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

variable "project_id" {
  description = "The GCP project ID."
  type        = string
}

variable "service_name" {
  description = "Name of the Cloud Run service whose logs feed the metrics."
  type        = string
}

variable "scheduler_job_names" {
  description = "Cloud Scheduler job IDs that trigger decision agent runs."
  type        = list(string)
}

variable "resource_prefix" {
  description = "Prefix used in alert policy and dashboard display names."
  type        = string
}

variable "notification_emails" {
  description = "Email addresses that receive every alert. Empty = alerts are created but only visible in the console."
  type        = list(string)
  default     = []
}

variable "additional_notification_channel_ids" {
  description = <<-EOT
    Existing notification channel IDs (projects/<p>/notificationChannels/<id>)
    to attach to every alert, e.g. Slack, PagerDuty or Google Chat channels
    created in the console.
  EOT
  type        = list(string)
  default     = []
}

variable "heartbeat_windows" {
  description = <<-EOT
    Map of usecase ("GoogleAds" / "SA360") to the PromQL range after which a
    missing run_completed event raises the missed-run alert, e.g. "13h".
    Set it a little above the gap between scheduled runs. Remove a usecase
    to disable its heartbeat (for example if SA360 is not in use). The
    GoogleAds entry is ignored when googleads_customer_heartbeats is set.
  EOT
  type        = map(string)
  default = {
    GoogleAds = "13h"
    SA360     = "25h"
  }
}

variable "googleads_customer_heartbeats" {
  description = <<-EOT
    Map of Google Ads customer ID to its missed-run window, e.g.
    { "5341114500" = "13h" }. One alert is created per account (filtered on
    the customer_id label of adsta_runs), replacing the usecase-level
    GoogleAds heartbeat, so one account that stops running is detected while
    the others keep running. Empty = the usecase-level heartbeat is used.
  EOT
  type        = map(string)
  default     = {}
}

variable "run_duration_warning_seconds" {
  description = "Warn when a single run takes longer than this (should be below the scheduler attempt deadline)."
  type        = number
  default     = 1440
}

variable "tool_error_threshold_per_hour" {
  description = "Warn when tool + model errors for one dependency exceed this count within an hour."
  type        = number
  default     = 10
}

variable "log_alert_rate_limit" {
  description = "Minimum interval between notifications from the same log-match alert."
  type        = string
  default     = "3600s"
}

variable "auto_close" {
  description = "How long an incident stays open without new matches before it auto-closes."
  type        = string
  default     = "86400s"
}
