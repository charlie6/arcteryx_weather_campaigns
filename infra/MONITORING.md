# ADSTA Monitoring

Alerts and the dashboard are created by
[`terraform/modules/monitoring`](terraform/modules/monitoring) when `./deploy.sh` runs
(`enable_monitoring: true`, the default). They are built on structured log events
emitted by [`agentic_dsta/core/telemetry.py`](../agentic_dsta/core/telemetry.py).

## Setup

In `config/app/config.yaml`:

```yaml
alert_notification_emails:
  - "adsta-oncall@example.com"
enable_sa360: false             # Google Ads only: no SA360 job, heartbeat or job alert
monitoring_heartbeat_windows:   # a bit longer than the gap between scheduled runs
  GoogleAds: "13h"              # cron "0 6,18 * * *"
  SA360: "25h"                  # ignored while enable_sa360 is false
```

Slack, PagerDuty and Google Chat channels can be created in the console and attached via
`alert_additional_notification_channel_ids`. After deploy, Terraform prints
`monitoring_dashboard_url`.

## Events (`jsonPayload.extra.event`)

| Event | Emitted when | Key labels |
|---|---|---|
| `run_started` | A run begins | `customer_id`, `usecase` |
| `run_completed` | Once per run, always (heartbeat) | `outcome`, `reason`, `run_id`, `total_duration_s` |
| `playbook_failed` | A playbook execution raised | `campaign_id`, `playbook_id`, `error_class` |
| `mutation_applied` | Google Ads / SA360 write (or dry-run suppression) | `action`, `dry_run`, `campaign_id`, `tool_args` |
| `tool_error` | A tool returned an error or raised | `dependency`, `error_class`, `tool`, `mutating` |
| `model_error` | A Gemini call failed | `error_class` |
| `change_guard_exceeded` | Budget changes in a run exceeded the cap | `run_id`, `budget_changes`, `cap` |

`outcome` is one of `success`, `partial`, `failed`, `aborted`, `noop`.
`error_class` is one of `auth`, `quota`, `timeout`, `unavailable`, `not_found`,
`invalid_argument`, `other`.

The scheduler endpoint returns **HTTP 500** for `failed` and `aborted` runs, so Cloud Scheduler
records the attempt as failed. Scheduler retries are disabled, so this never re-applies changes.

## Alerts

| Severity | Alert | Fires when |
|---|---|---|
| Critical | Missed runs (per usecase) | No `run_completed` within the heartbeat window |
| Critical | Scheduled run failed | Cloud Scheduler attempt ends in error (5xx, timeout) |
| Critical | Change volume guard exceeded | `change_guard_exceeded` |
| Critical | Authentication failure | Any event with `error_class="auth"` (for example, an expired refresh token) |
| Warning | Run partially failed | `outcome="partial"` or `reason="no_runnable_playbooks"` |
| Warning | Dependency error spike | More than 10 tool/model errors per hour for one dependency |
| Warning | Run near timeout | Run longer than 80% of the scheduler attempt deadline |

Each alert includes a runbook in its notification. The missed-run alert fires after a fresh
deploy until the first scheduled run completes.

## Useful log queries

```
jsonPayload.extra.event="run_completed"                          # run history + outcomes
jsonPayload.extra.event="mutation_applied" jsonPayload.extra.dry_run="false"   # live audit trail
jsonPayload.extra.run_id="<RUN_ID>"                              # everything from one run
jsonPayload.extra.event=("tool_error" OR "model_error")          # dependency failures
```

## Testing alerts

Against a test project, with `ADSTA_DRY_RUN=true`:

- **Scheduled run failed**: point `googleads_customer_id` at an ID with no Firestore config, then force-run the job.
- **Authentication failure**: temporarily set an invalid refresh token secret, then force-run.
- **Missed runs**: pause the scheduler job for longer than the heartbeat window.
