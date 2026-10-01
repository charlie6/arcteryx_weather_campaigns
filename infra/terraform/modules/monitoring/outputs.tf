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

output "dashboard_url" {
  description = "Console URL of the ADSTA Operations dashboard."
  value       = local.dashboard_url
}

output "alert_policy_ids" {
  description = "IDs of all alert policies created by this module."
  value = concat(
    [for p in google_monitoring_alert_policy.missed_runs : p.id],
    [
      google_monitoring_alert_policy.scheduler_failed.id,
      google_monitoring_alert_policy.change_guard.id,
      google_monitoring_alert_policy.auth_failure.id,
      google_monitoring_alert_policy.degraded_run.id,
      google_monitoring_alert_policy.dependency_errors.id,
      google_monitoring_alert_policy.slow_run.id,
      google_monitoring_alert_policy.config_sync.id,
    ],
  )
}

output "notification_channel_ids" {
  description = "Notification channels attached to every alert."
  value       = local.channels
}
