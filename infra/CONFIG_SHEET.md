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
| Geo, Campaign name contains | Optional. The name check stops ADSTA from acting if the campaign has been renamed. |
| Asset groups / Severe budget | Which playbooks to run. Blank means Y / N. |
| Normal daily budget | In account currency, for example `25.00`. Blank means the sheet does not manage this budget. See [Budgets](#budgets). |
| Budget bump % | Required when Severe budget is Y. |
| Max daily budget | Optional cap on the severe-weather increase. Must be at least the normal budget. |

**Cities**

| Column | Meaning | Allowed |
|---|---|---|
| Cold below C | The Cold asset group turns on when the forecast minimum temperature is below this | -30 to 30 |
| Rain rate mm/h | The Rain asset group turns on when the peak rain rate reaches this | 0.01 to 20 |
| Sunny min hours | The Sunny asset group turns on when the forecast has at least this many clear daylight hours | 0 to 24 |
| `(ref)` columns | Coordinates and severe-weather thresholds, for reference only. These are computed from climate history. The sync ignores edits to them. | |

A blank threshold means the playbook default applies (9.0 °C, 0.2 mm/h, 3 h). Removing a city's row leaves its stored thresholds unchanged. You can't add new cities in the sheet: add them to `infra/config/baselines/cities.json` and rebuild the baselines (see AGENTS.md).

## How a sync behaves

- **Validation:** if any row has an error, **nothing is written**. Runs keep using the last good configuration, and the "Config sheet needs attention" alert fires. The log and `SyncLog` name the tab, row and column of each error.
- **Scope:** a scheduled run syncs its own account's `Accounts` and `Campaigns` rows, plus all `Cities` rows.
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

`ADSTA_DRY_RUN` applies here as well. In dry-run mode the sync reports budget pushes but doesn't make them.
