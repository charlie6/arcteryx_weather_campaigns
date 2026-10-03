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
| `run_completed` | Once per run, always (heartbeat) | `customer_id`, `usecase`, `outcome`, `reason`, `run_id`, `total_duration_s`, `successful_campaigns` / `failed_campaigns` (a campaign fails if any of its playbooks failed), `successful_playbooks` / `failed_playbooks`, `eligible_campaigns`, `skipped_campaigns`, `dry_run_campaigns` |
| `playbook_failed` | A playbook execution raised | `campaign_id`, `playbook_id`, `error_class` |
| `mutation_applied` | Google Ads / SA360 write (or dry-run suppression) | `action`, `dry_run`, `dry_run_source` (`deployment` or `campaign`, dry runs only), `campaign_id`, `tool_args` |
| `tool_error` | A tool returned an error or raised | `dependency`, `error_class`, `tool`, `mutating` |
| `model_error` | A Gemini call failed | `error_class` |
| `dependency_retry` | A Google Ads read hit a transient error (gRPC `INTERNAL` or `UNAVAILABLE`) and is retried. WARNING; no alert. Reads are tried up to 4 times, about 1s, 2s and 4s apart; writes are never retried | `dependency`, `operation`, `customer_id`, `attempt`, `max_attempts`, `error_class` |
| `change_guard_exceeded` | Budget **increases** in a run exceeded the cap (reverts to normal are not counted) | `run_id`, `customer_id`, `budget_changes`, `log_only_changes` (increases by campaigns in dry run, included in `budget_changes`), `cap` |
| `campaign_name_mismatch` | A campaign was skipped because its live name doesn't contain `Campaign name contains`, or couldn't be read. Nothing ran for it | `campaign_id`, `reason` (`mismatch`, `not_found`, `lookup_failed`), `expected_name_contains`, `actual_name` |
| `config_sync` | Once per config sheet sync (start of each run when `CONFIG_SHEET_ID` is set, or the CLI) | `customer_id` (the run's account), `outcome` (`applied`, `noop`, `invalid`, `error`, `partial`), `changes`, `budget_pushes`, `budget_records` (normal budgets saved without changing Google Ads), `conflicts`, `error_count`, `other_account_error_count`, `needs_attention` |

`outcome` is one of `success`, `partial`, `failed`, `aborted`, `noop`. A run that skipped
campaigns with the name guard ends `partial` with `reason="campaigns_skipped"`.
`error_class` is one of `auth`, `quota`, `timeout`, `unavailable`, `not_found`,
`invalid_argument`, `other`.

The scheduler endpoint returns **HTTP 500** for `failed` and `aborted` runs, so Cloud Scheduler
records the attempt as failed. Scheduler retries are disabled, so this never re-applies changes.

## Alerts

| Severity | Alert | Fires when |
|---|---|---|
| Critical | Missed runs (one per Google Ads account, plus SA360) | No `run_completed` for that account within the heartbeat window |
| Critical | Scheduled run failed | Cloud Scheduler attempt ends in error (5xx, timeout) |
| Critical | Change volume guard exceeded | `change_guard_exceeded` |
| Critical | Authentication failure | Any event with `error_class="auth"` (for example, an expired refresh token) |
| Warning | Run partially failed | `outcome="partial"` (including `campaigns_skipped`) or `reason="no_runnable_playbooks"` |
| Warning | Dependency error spike | More than 10 tool/model errors per hour for one dependency |
| Warning | Run near timeout | Run longer than 80% of the scheduler attempt deadline |
| Warning | Config sheet needs attention | `config_sync` with `needs_attention=true`: sheet invalid or unreadable, another account's rows invalid, a budget push failed, or a budget conflict (see [CONFIG_SHEET.md](CONFIG_SHEET.md)) |

Each alert includes a runbook in its notification, and the account-specific ones name the
customer ID. A Google Ads account's missed-run alert fires after a fresh deploy, or after the
account is added (`googleads_additional_customers`), until that account's first run completes.
**Force run** its scheduler job to clear it.

Upgrading from a single-account deployment replaces "Missed runs (GoogleAds)" with a
per-account alert such as "Missed runs (GoogleAds 5341114500)" and adds a `customer_id` label
to the `adsta_runs` metric. Earlier points carry no `customer_id`, so the new alert fires once
after the deploy; force run the job right after deploying. The dashboard's runs chart groups
by account.

## Useful log queries

```
jsonPayload.extra.event="run_completed"                          # run history + outcomes
jsonPayload.extra.event="mutation_applied" jsonPayload.extra.dry_run="false"   # live audit trail
jsonPayload.extra.event="mutation_applied" jsonPayload.extra.dry_run_source="campaign"  # what campaigns in dry run would have done
jsonPayload.extra.run_id="<RUN_ID>"                              # everything from one run
jsonPayload.extra.event=("tool_error" OR "model_error")          # dependency failures
jsonPayload.extra.event="dependency_retry"                       # transient Google Ads errors that were retried
```

## Testing alerts

Against a test project, with `ADSTA_DRY_RUN=true`:

- **Scheduled run failed**: point `googleads_customer_id` at an ID with no Firestore config, then force-run the job.
- **Authentication failure**: temporarily set an invalid refresh token secret, then force-run.
- **Missed runs**: pause the scheduler job for longer than the heartbeat window.
