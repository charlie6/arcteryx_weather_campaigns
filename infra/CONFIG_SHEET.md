# ADSTA Configuration Sheet

You maintain ADSTA's day-to-day settings in one Google Sheet instead of editing Firestore directly. When `CONFIG_SHEET_ID` is set, every scheduled run syncs the sheet into Firestore first. You can also preview or apply a sync yourself with `infra/scripts/config/sync_config_sheet.py`.

| Tab | One row per | Firestore target |
|---|---|---|
| `Accounts` | Google Ads customer | `GoogleAdsConfig/{customerId}` account fields |
| `Campaigns` | campaign | `GoogleAdsConfig/{customerId}.campaigns[]`, plus the normal daily budget in `CampaignBudgetState` and Google Ads |
| `Cities` | weather location | `ClimateBaselines/{city}.activation` |
| `SyncLog` | applied sync (written by ADSTA) | nothing; `ConfigChangeLog/{syncId}` is the authoritative record |

Columns are matched by their header text, so you can reorder them or add your own (for example `Notes`).

## Setup

1. Create an empty Google Sheet. Copy its id: the long string between `/d/` and `/edit` in the URL.
2. Share the sheet as **Editor** with:
   - the Cloud Run service account (Terraform output `scheduler_oidc_service_account`, `<prefix>-runner@<project>.iam.gserviceaccount.com`). Editor access lets it write `SyncLog`.
   - every person who will edit it.
3. Fill the sheet from the live configuration:
   ```bash
   cd agentic_dsta
   gcloud auth application-default login \
     --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform
   python3 infra/scripts/config/sync_config_sheet.py export \
     --sheet_id SHEET_ID --project_id PROJECT --database <prefix>-firestore
   ```
   `export` will not overwrite a tab that already has data unless you pass `--force`. Use `--tabs Cities` to export one tab only, for example to refresh the reference columns.
4. Check that nothing would change unexpectedly:
   ```bash
   python3 infra/scripts/config/sync_config_sheet.py plan --sheet_id SHEET_ID --project_id PROJECT --database <prefix>-firestore
   ```
   The first plan after an export is expected to write every city's thresholds. The export fills in the effective values, including playbook defaults, and the first sync stores them on each city.
5. Turn on automatic sync. Add the id to `run_service_env_vars` in `infra/config/app/config.yaml` and redeploy:
   ```yaml
   run_service_env_vars:
     GOOGLE_GENAI_USE_VERTEXAI: "True"
     CONFIG_SHEET_ID: "SHEET_ID"
   ```

## Columns

**Accounts**

| Column | Notes |
|---|---|
| Customer ID | 10 digits. Hyphens are fine. |
| Account label | Written to the change log, for example `Canada`. |
| Timezone | IANA name, for example `America/Vancouver`. |
| Runs per day / Look-ahead hours / Trailing window hours | Blank means 2 / 24 / 24. |
| Change guard enabled / max fraction | Y/N and 0.01–1. |
| Cold / Rain / Snow / Sunny token | Text that identifies each weather asset group by name, for example `_COLD`. Required, and must be unique. |

**Campaigns**

| Column | Notes |
|---|---|
| Customer ID, Campaign ID | The customer must have a row in `Accounts`. |
| Active | `N` takes the campaign out of ADSTA (it is removed from `GoogleAdsConfig`). Blank means Y. |
| City | Must exactly match a city in the `Cities` tab. |
| Geo, Campaign name contains | Optional. Before running anything for a campaign, ADSTA reads its live name from Google Ads. If the name doesn't contain this text (case-insensitive), or can't be read, the campaign is skipped for that run. Nothing changes, a ChangeLog row is written, and the "Run partially failed" alert fires. Use a distinctive part of the name. |
| Asset groups / Severe budget | Which playbooks to run. Blank means Y / N. |
| Normal daily budget | In account currency, for example `25.00`. Blank means the sheet does not manage this budget. See [Budgets](#budgets). |
| Budget bump % | Required when Severe budget is Y. |
| Max daily budget | Optional cap on the severe-weather increase. Must be at least the normal budget. |
| Dry run | Optional. `Y` runs the campaign without changing it in Google Ads. Blank means N. See [Dry run for one campaign](#dry-run-for-one-campaign). |

**Cities**

| Column | Meaning | Allowed |
|---|---|---|
| Cold below C | The Cold asset group turns on when the forecast minimum temperature is below this | -30 to 30 |
| Rain rate mm/h | The Rain asset group turns on when the peak rain rate reaches this | 0.01 to 20 |
| Sunny min hours | The Sunny asset group turns on when the forecast has at least this many clear daylight hours | 0 to 24 |
| `(ref)` columns | Coordinates and severe-weather thresholds, for reference only. These are computed from climate history. The sync ignores edits to them. | |

A blank threshold means the playbook default applies (9.0 °C, 0.2 mm/h, 3 h). Removing a city's row leaves its stored thresholds unchanged. You can't add new cities in the sheet: add them to `infra/config/baselines/cities.json` and rebuild the baselines (see AGENTS.md).

## How a sync behaves

- **Validation:** a sync with an error that concerns it writes **nothing**, not even its valid rows. Runs keep using the last good configuration, and the "Config sheet needs attention" alert fires. The log and `SyncLog` name the tab, row and column of each error.
- **Errors are per account:** a scheduled run is blocked by errors in its own account's rows, in the `Cities` tab (shared by every account), and in rows whose Customer ID can't be read (they could belong to any account). Another account's errors don't block it. They are listed with "does not block this sync", the alert still fires, and that account's own syncs stay blocked until the rows are fixed. A campaign listed under two accounts blocks both. `sync_config_sheet.py` without `--customer_id` is blocked by any error.
- **Scope:** a scheduled run syncs its own account's `Accounts` and `Campaigns` rows, plus all `Cities` rows. The `SyncLog` Source column shows which account's run wrote the row, for example `scheduled_run (5341114500)`.
- **Settings the sheet doesn't show** stay as they are in `GoogleAdsConfig`, for example `params.hemisphere`.
- **Redeploys** keep the city thresholds. `upload_config.py` replaces `ClimateBaselines` but preserves `activation`. Only `FIRESTORE_SEED_MODE=overwrite` resets them.
- **Audit trail:** each sync that changes something writes `ConfigChangeLog/{syncId}`. Budget pushes also write a `ChangeLog` row and a `mutation_applied` event.

## Budgets

The normal daily budget is what ADSTA returns to after a severe-weather increase. When you change it in the sheet:

| Situation | What the sync does |
|---|---|
| No increase active, and Google Ads shows ADSTA's normal budget | Sets the new budget in Google Ads right away and records it as the normal budget. |
| A severe-weather increase is active | Records the new normal budget only. Google Ads keeps the higher budget until the increase ends, then drops to the new value. |
| The budget was changed outside the sheet (by hand in Google Ads, or adopted by ADSTA after a manual change) and the sheet still shows the old value | **Conflict.** Skips the campaign and reports it on every run until you update the cell. Enter the live value to accept the manual change, or a new value to override it. |
| Shared budget, or the budget can't be read | Skips the campaign and reports why. |

`ADSTA_DRY_RUN` applies here as well. In dry-run mode the sync reports budget pushes but doesn't make them. A campaign's own `Dry run` does the same for that campaign: its new budget is pushed on the first sync after you set it back to `N`.

## Dry run for one campaign

Set a campaign's `Dry run` to `Y` to try ADSTA on it, or to stop it changing the campaign, without affecting the other campaigns. The next run picks it up, because each run syncs the sheet first.

- **What still happens:** the campaign's playbooks run as usual. They read the forecast and Google Ads, decide, and write their ChangeLog rows. The model isn't told about the dry run, so it decides exactly as it would live.
- **What doesn't:** nothing changes in Google Ads for that campaign. ADSTA blocks every write in code: no asset group is enabled or paused, no budget changes, and the sheet's Normal daily budget isn't pushed.
- **Where to see it:** the campaign's ChangeLog rows have `mode` `log-only`. Its `mutation_applied` events have `dry_run="true"` and `dry_run_source="campaign"`. `run_completed` counts the campaigns in dry run in `dry_run_campaigns`.
- **The campaign stays as ADSTA last left it.** If a severe-weather increase or a weather asset group is active when you set `Y`, it stays on until you set `N`. Then the next run picks up from there and reverts what the weather no longer calls for. To start from a clean state, set `Y` while nothing is active.
- **Change volume guard:** budget increases a campaign in dry run would have made still count towards the cap, because a bad forecast shows up either way. The guard's log entry says how many of them were log-only.
- `ADSTA_DRY_RUN` overrides the column: when it is on, every campaign is in dry run.
- **A sheet without the column** leaves each campaign's setting as it is. To use it in an existing sheet, add a `Dry run` header to `Campaigns`. `export` puts it last.

## Adding a Google Ads account

One deployment can run several Google Ads accounts under the same manager (MCC) account. Each account has its own scheduler job and its own "Missed runs" alert. Its runs and syncs are independent of the other accounts.

1. Check that the account is linked under the MCC in `google_ads_login_customer_id`, and that the Google user behind ADSTA's refresh token can open it. No new credentials are needed.
2. Add the account's row to `Accounts` and its campaigns to `Campaigns`. Check them before anything runs:
   ```bash
   python3 infra/scripts/config/sync_config_sheet.py plan --customer_id NEW_ID \
     --sheet_id SHEET_ID --project_id PROJECT --database <prefix>-firestore
   ```
3. Give the account its run instructions by copying a working account's. This only creates the document; an existing one is never changed:
   ```bash
   python3 infra/scripts/config/sync_config_sheet.py seed-account \
     --customer_id NEW_ID --from_customer_id EXISTING_ID --project_id PROJECT --database <prefix>-firestore
   ```
   Without `CustomerInstructions/NEW_ID`, every run for the account aborts. The sync warns about it.
4. Add the account to `infra/config/app/config.yaml` and redeploy. Quote the IDs and the cron strings:
   ```yaml
   googleads_additional_customers:
     "1112223333": { schedule: "15 6,18 * * *" }
     "4445556666": { schedule: "30 6,18 * * *", time_zone: "America/Toronto" }
   ```
   - `googleads_customer_id` stays the first account, and its job keeps its name, `<prefix>-ga-combined-job`. Each additional account gets `<prefix>-ga-<id>-job`. The Terraform output `google_ads_scheduler_jobs` lists them all.
   - Every field is optional. `schedule` and `time_zone` default to `googleads_scheduler_schedule` and `sa_run_sse_scheduler_job_timezone`. `heartbeat_window`, the missed-run window, defaults to `monitoring_heartbeat_windows.GoogleAds`.
   - Stagger the schedules about 15 minutes apart. Runs that overlap share one Cloud Run instance and call Gemini at the same time, and `max_concurrent_campaigns` applies to each run separately.
   - Keep the account's `Runs per day` in step with its cron.
5. In Cloud Scheduler, use **Force run** on the new job and check its logs. The account's "Missed runs" alert fires until its first run completes.

`ADSTA_DRY_RUN` applies to every account in the deployment. To try a new account first, set its campaigns' `Dry run` to `Y`.

To remove an account, delete its entry from `googleads_additional_customers` and redeploy. This deletes its job and its alert. Then delete its rows or set its campaigns' `Active` to `N`. Its Firestore documents stay, unused.
