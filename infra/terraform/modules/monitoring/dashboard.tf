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

# "ADSTA Operations" dashboard. Built with jsonencode so names and thresholds
# stay in sync with the metrics defined in main.tf.

locals {
  run_resource = "resource.type=\"cloud_run_revision\""
  run_service  = "resource.label.\"service_name\"=\"${var.service_name}\""

  # Log-based counters shown as stacked bars, summed per hour.
  counter_chart_specs = {
    runs = {
      title  = "Runs by outcome"
      metric = google_logging_metric.runs.name
      group  = ["metric.label.\"usecase\"", "metric.label.\"outcome\""]
    }
    mutations = {
      title  = "Ad changes by action (dry_run=true means suppressed)"
      metric = google_logging_metric.mutations.name
      group  = ["metric.label.\"action\"", "metric.label.\"dry_run\""]
    }
    tool_errors = {
      title  = "Tool / model errors by dependency"
      metric = google_logging_metric.tool_errors.name
      group  = ["metric.label.\"dependency\"", "metric.label.\"error_class\""]
    }
    playbook_failures = {
      title  = "Playbook failures"
      metric = google_logging_metric.playbook_failures.name
      group  = ["metric.label.\"playbook_id\"", "metric.label.\"error_class\""]
    }
  }

  counter_chart = {
    for k, v in local.counter_chart_specs : k => {
      title = v.title
      xyChart = {
        dataSets = [{
          plotType   = "STACKED_BAR"
          targetAxis = "Y1"
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"${local.metric_prefix}/${v.metric}\" ${local.run_resource}"
              aggregation = {
                alignmentPeriod    = "3600s"
                perSeriesAligner   = "ALIGN_SUM"
                crossSeriesReducer = "REDUCE_SUM"
                groupByFields      = v.group
              }
            }
          }
        }]
        yAxis = { scale = "LINEAR" }
      }
    }
  }

  # Cloud Run built-in metrics shown as lines.
  run_chart_specs = {
    requests = {
      title   = "Cloud Run requests by response class"
      metric  = "run.googleapis.com/request_count"
      aligner = "ALIGN_SUM"
      reducer = "REDUCE_SUM"
      group   = ["metric.label.\"response_code_class\""]
    }
    latency = {
      title   = "Cloud Run request latency p95 (ms)"
      metric  = "run.googleapis.com/request_latencies"
      aligner = "ALIGN_PERCENTILE_95"
      reducer = "REDUCE_MAX"
      group   = []
    }
    instances = {
      title   = "Cloud Run instances"
      metric  = "run.googleapis.com/container/instance_count"
      aligner = "ALIGN_MAX"
      reducer = "REDUCE_SUM"
      group   = ["metric.label.\"state\""]
    }
    memory = {
      title   = "Cloud Run memory utilization p99"
      metric  = "run.googleapis.com/container/memory/utilizations"
      aligner = "ALIGN_PERCENTILE_99"
      reducer = "REDUCE_MAX"
      group   = []
    }
  }

  run_chart = {
    for k, v in local.run_chart_specs : k => {
      title = v.title
      xyChart = {
        dataSets = [{
          plotType   = "LINE"
          targetAxis = "Y1"
          timeSeriesQuery = {
            timeSeriesFilter = {
              filter = "metric.type=\"${v.metric}\" ${local.run_resource} ${local.run_service}"
              aggregation = {
                alignmentPeriod    = "300s"
                perSeriesAligner   = v.aligner
                crossSeriesReducer = v.reducer
                groupByFields      = v.group
              }
            }
          }
        }]
        yAxis = { scale = "LINEAR" }
      }
    }
  }

  duration_chart = {
    title = "Run duration p50 / p95 (s)"
    xyChart = {
      dataSets = [for p in ["50", "95"] : {
        plotType       = "LINE"
        targetAxis     = "Y1"
        legendTemplate = "p${p} $${metric.labels.usecase}"
        timeSeriesQuery = {
          timeSeriesFilter = {
            filter = "metric.type=\"${local.metric_prefix}/${google_logging_metric.run_duration.name}\" ${local.run_resource}"
            aggregation = {
              alignmentPeriod    = "3600s"
              perSeriesAligner   = "ALIGN_PERCENTILE_${p}"
              crossSeriesReducer = "REDUCE_MAX"
              groupByFields      = ["metric.label.\"usecase\""]
            }
          }
        }
      }]
      # XyChart thresholds cannot set color or direction (scorecard-only fields).
      thresholds = [{
        label      = "warning"
        value      = var.run_duration_warning_seconds
        targetAxis = "Y1"
      }]
      yAxis = { scale = "LINEAR" }
    }
  }

  live_changes_scorecard = {
    title = "Live ad changes (last 24h)"
    scorecard = {
      timeSeriesQuery = {
        timeSeriesFilter = {
          filter = "metric.type=\"${local.metric_prefix}/${google_logging_metric.mutations.name}\" ${local.run_resource} metric.label.\"dry_run\"=\"false\""
          aggregation = {
            alignmentPeriod    = "86400s"
            perSeriesAligner   = "ALIGN_SUM"
            crossSeriesReducer = "REDUCE_SUM"
          }
        }
      }
    }
  }

  failed_runs_scorecard = {
    title = "Failed / aborted runs (last 24h)"
    scorecard = {
      timeSeriesQuery = {
        timeSeriesFilter = {
          filter = "metric.type=\"${local.metric_prefix}/${google_logging_metric.runs.name}\" ${local.run_resource} metric.label.\"outcome\"=monitoring.regex.full_match(\"failed|aborted\")"
          aggregation = {
            alignmentPeriod    = "86400s"
            perSeriesAligner   = "ALIGN_SUM"
            crossSeriesReducer = "REDUCE_SUM"
          }
        }
      }
      thresholds = [{
        value     = 0
        color     = "RED"
        direction = "ABOVE"
      }]
    }
  }

  dashboard = {
    displayName = "${var.resource_prefix} - ADSTA Operations"
    labels      = { adsta = "" }
    mosaicLayout = {
      columns = 48
      tiles = [
        # Row 1: run health
        { xPos = 0, yPos = 0, width = 8, height = 8, widget = local.failed_runs_scorecard },
        { xPos = 0, yPos = 8, width = 8, height = 8, widget = local.live_changes_scorecard },
        { xPos = 8, yPos = 0, width = 20, height = 16, widget = local.counter_chart.runs },
        { xPos = 28, yPos = 0, width = 20, height = 16, widget = local.duration_chart },
        # Row 2: what the agent did and what broke
        { xPos = 0, yPos = 16, width = 16, height = 16, widget = local.counter_chart.mutations },
        { xPos = 16, yPos = 16, width = 16, height = 16, widget = local.counter_chart.tool_errors },
        { xPos = 32, yPos = 16, width = 16, height = 16, widget = local.counter_chart.playbook_failures },
        # Row 3: platform
        { xPos = 0, yPos = 32, width = 12, height = 14, widget = local.run_chart.requests },
        { xPos = 12, yPos = 32, width = 12, height = 14, widget = local.run_chart.latency },
        { xPos = 24, yPos = 32, width = 12, height = 14, widget = local.run_chart.instances },
        { xPos = 36, yPos = 32, width = 12, height = 14, widget = local.run_chart.memory },
        # Row 4: logs
        {
          xPos   = 0
          yPos   = 46
          width  = 24
          height = 20
          widget = {
            title = "Audit trail (mutation_applied)"
            logsPanel = {
              filter        = "${trimspace(local.app_log_filter)}\njsonPayload.extra.event=\"mutation_applied\""
              resourceNames = ["projects/${var.project_id}"]
            }
          }
        },
        {
          xPos   = 24
          yPos   = 46
          width  = 24
          height = 20
          widget = {
            title = "Warnings and errors"
            logsPanel = {
              filter        = "${trimspace(local.app_log_filter)}\nseverity>=WARNING"
              resourceNames = ["projects/${var.project_id}"]
            }
          }
        },
      ]
    }
  }
}

resource "google_monitoring_dashboard" "adsta" {
  project        = var.project_id
  dashboard_json = jsonencode(local.dashboard)
}
