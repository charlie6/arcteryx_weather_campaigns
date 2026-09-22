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
"""Generates the sample Firestore configs from readable playbook templates.

`upload_config.py` consumes JSON, but playbook templates are long instruction
prose. Maintaining them as escaped JSON string literals makes review and diffing
impractical, so this script is the source of truth for that prose and the JSON
files under `infra/config/samples/` are generated artifacts.

Edit the templates here, re-run, and commit both this file and the regenerated
JSON:

    cd agentic_dsta
    python3 infra/scripts/config/generate_sample_configs.py

`ClimateBaselines` documents are never regenerated here. They come from real
reanalysis data via `infra/scripts/baselines/build_climate_baselines.py` and are
carried over from whatever is already in the target file.
"""

import json
import pathlib

ASSET_GROUP_PLAYBOOK = """MODE: live writes are controlled in code by the ADSTA_DRY_RUN environment variable, not by
this prompt. Call the tools you judge correct. If a tool returns dry_run=true the write was
suppressed: treat it as 'would have applied', record mode 'log-only' in the change log, and do
NOT record the change as applied in state. Do not retry a suppressed write.

TASK: advance weather triggering (spec section 4) for campaign {{campaignId}}.
Account: {{account}}. Weather location: {{city}}. Geo token: {{geo}}.

RUN CONTEXT - already resolved, do not recompute:
  Run id                : {{runId}}
  Today (UTC)           : {{today}}
  Today (account local) : {{todayLocal}} [{{timezone}}]
  Timestamp (UTC)       : {{nowIsoUtc}}
  Timestamp (local)     : {{nowIsoLocal}}
  Look-ahead window     : {{lookAheadHours}} hours
  Trailing window       : {{trailingWindowHours}} hours = the last {{trailingWindowRuns}} run(s)

STEP 1 - Resolve coordinates.
Call get_document on collection 'ClimateBaselines', document_id '{{city}}'.
Read latitude and longitude from that document. Never assume or recall coordinates.
If the document does not exist, or either coordinate is missing, treat this as a weather
failure and go straight to STEP 6.

STEP 2 - Forecast.
Call get_24h_weather_signals with the latitude and longitude from STEP 1 and hours
{{lookAheadHours}}.
If success is false, or hours_received is less than hours_requested, go to STEP 6.

STEP 3 - Evaluate every condition INDEPENDENTLY.
Do NOT stop at the first match. More than one condition can be true at the same time, for
example Cold and Snow together during a cold snowstorm, and each has its own asset group.
{{conditionsTable}}
For each condition record whether it is active in this look-ahead, together with the observed
value and the threshold you compared it against.

STEP 4 - Apply the trailing window.
Call get_document on collection 'AssetGroupState', document_id '{{assetGroupStateDocId}}'.
Field conditionHistory maps each condition name to runsSinceLastSeen and currentStatus.
If the document does not exist, treat every condition as never seen.
For each condition:
  - Active in STEP 3              -> runsSinceLastSeen = 0, target status ENABLED
  - Not active, but seen before   -> runsSinceLastSeen = previous value + 1.
                                     Target ENABLED while runsSinceLastSeen is less than or
                                     equal to {{trailingWindowRuns}}, otherwise PAUSED.
  - Never seen                    -> target PAUSED
This is what keeps an asset group live for a further {{trailingWindowHours}} hours after the
condition leaves the look-ahead. It is only paused once neither this run nor the previous
{{trailingWindowRuns}} run(s) saw the condition.

STEP 5 - Apply to the account.
Call list_google_ads_asset_groups(customer_id '{{customerId}}', campaign_id '{{campaignId}}').
Match asset groups to conditions using the token listed in STEP 3, comparing upper-cased names.
  - If {{campaignNameContains}} is not null, verify that the returned campaign_name contains
    that text. If it does not, make NO changes and report a naming mismatch: the campaign may
    have been renamed, or the config may point at the wrong campaign.
  - If a condition matches no asset group, skip that condition and note it. Never invent a name.
  - If a condition matches more than one asset group, make NO change for that condition and
    report the ambiguity.
  - Never touch an asset group whose can_be_paused is false or whose is_always_on is true. The
    always-on group stays enabled before, during and after the event.
Call update_google_ads_asset_group_status_by_name only for groups whose current status differs
from the target status. Do not issue no-op writes. Then go to STEP 7.

STEP 6 - Weather failure fallback.
The forecast for this location is unusable. Per spec section 7, fall back to always-on:
  - Call list_google_ads_asset_groups and PAUSE every managed weather asset group for this
    campaign, meaning those whose can_be_paused is true. Leave the always-on group running.
  - Make no budget change of any kind.
  - Reset conditionHistory so that no condition is treated as recently seen.
  - Use notes 'weather API failure - fell back to always-on', then continue to STEP 7 and 8.

STEP 7 - Persist state.
Call set_document on 'AssetGroupState' / '{{assetGroupStateDocId}}' with merge true, writing
conditionHistory (runsSinceLastSeen and currentStatus per condition), lastRunId '{{runId}}' and
lastRunAt '{{nowIsoUtc}}'.
If a status update returned dry_run=true then the account did not change: record the status you
observed, not the status you intended.

STEP 8 - Change log. Always write one row per condition evaluated, even when nothing changed.
Call set_document on collection 'ChangeLog', document_id
'{{today}}_{{campaignId}}_<condition>_{{runId}}', with:
  runId '{{runId}}', timestampUtc '{{nowIsoUtc}}', timestampLocal '{{nowIsoLocal}}',
  account '{{account}}', customerId '{{customerId}}', campaignId '{{campaignId}}',
  campaignName (as returned by the API, never from config), weatherLocation '{{city}}',
  condition, assetGroupName, assetGroupAction ('enabled', 'paused' or 'no change'),
  severityMet false, triggerDetail (the observed value and the threshold),
  budgetBeforeMicros 'unchanged', budgetAfterMicros 'unchanged',
  mode ('live' or 'log-only') and notes.

RULES
- Never change campaign status. Campaigns remain enabled at all times.
- Never change the bidding strategy, and never set, alter or remove a target ROAS.
- Never change a budget in this playbook.
- Never pause an always-on asset group. If a tool returns blocked_by_guard, accept it and move
  on. Do not attempt a workaround.
- Report each condition with the observed value and the threshold it was compared against.
"""

SEVERE_BUDGET_PLAYBOOK = """MODE: live writes are controlled in code by the ADSTA_DRY_RUN environment variable, not by
this prompt. Call the tools you judge correct. If a tool returns dry_run=true the write was
suppressed: treat it as 'would have applied', record mode 'log-only' in the change log, and do
NOT record the change as applied in state. Do not retry a suppressed write.

TASK: severe weather budget adjustment (spec section 5) for campaign {{campaignId}}.
Account: {{account}}. Weather location: {{city}}. Geo token: {{geo}}.

RUN CONTEXT - already resolved, do not recompute:
  Run id                : {{runId}}
  Today (UTC)           : {{today}}
  Today (account local) : {{todayLocal}} [{{timezone}}]
  Timestamp (UTC)       : {{nowIsoUtc}}
  Timestamp (local)     : {{nowIsoLocal}}
  Current month number  : {{currentMonth}}
  Current season        : {{season}}
  Look-ahead window     : {{lookAheadHours}} hours
  State document        : 'CampaignBudgetState' / '{{budgetStateDocId}}'

SEVERE MODIFIERS for this campaign:
  Absolute rain floor   : {{severeModifiers.severeRainMm}} mm per 24h
  Absolute snow floor   : {{severeModifiers.severeSnowMm}} mm water equivalent per 24h
  Cold margin           : {{severeModifiers.severeColdMarginC}} C below the seasonal baseline
  Relative multiplier   : {{severeModifiers.relativeMultiplier}} times the monthly baseline
  Budget bump           : {{severeModifiers.budgetBumpPct}} percent
  Optional ceiling      : {{severeModifiers.maxDailyBudgetMicros}} micros (null means no ceiling)
  Max consecutive days  : {{maxConsecutiveDays}}
  Rolling window        : {{maxIncreasedDaysInWindow}} increased days per {{rollingWindowDays}} days

STEP 1 - Resolve coordinates and baselines.
Call get_document on collection 'ClimateBaselines', document_id '{{city}}'. From that one
document read:
  latitude, longitude                 coordinates for the forecast call
  monthlyRainMmPerWetDay[{{currentMonth}}]   rain baseline, mm
  monthlySnowMmSwePerSnowDay[{{currentMonth}}]  snow baseline, mm water equivalent
  seasonalColdBaselineC.{{season}}     cold baseline, Celsius
Never assume coordinates or baselines. If the document does not exist, or either coordinate is
missing, make NO change, log notes 'missing ClimateBaselines document' and stop.
If a baseline needed for one condition is missing or null, that condition cannot qualify: treat
it as not severe, log a warning, and carry on with the others.

STEP 2 - Forecast.
Call get_24h_weather_signals with the latitude and longitude from STEP 1 and hours
{{lookAheadHours}}.
If success is false, or hours_received is less than hours_requested: make NO budget change. If
an increase is currently active, LEAVE IT IN PLACE and let the clock continue, because
reverting on a transient failure and re-applying next run would create churn. Write a change
log row with notes 'weather API failure' and stop.

STEP 3 - Severity test. Rain and snow require BOTH a relative and an absolute test to pass, so
a wet month cannot trigger an increase on a trivial amount of rain.
  RAIN severe : rain_accumulation_mm >= {{severeModifiers.relativeMultiplier}} * rain baseline
                AND rain_accumulation_mm >= {{severeModifiers.severeRainMm}}
  SNOW severe : snow_accumulation_mm >= {{severeModifiers.relativeMultiplier}} * snow baseline
                AND snow_accumulation_mm >= {{severeModifiers.severeSnowMm}}
  COLD severe : min_temperature_c <= (cold baseline - {{severeModifiers.severeColdMarginC}})
Hot and warm conditions NEVER qualify for a budget change.
SEVERE is true if any single test passes. Increases never stack: if several conditions qualify
on the same day the increase is still {{severeModifiers.budgetBumpPct}} percent in total.

STEP 4 - Read the live budget. Two calls, in this order.
  a. get_google_ads_campaign_details(customer_id '{{customerId}}', campaign_id '{{campaignId}}')
     and read the campaignBudget field, which is the budget resource name. Also read the
     campaign name for the change log. If {{campaignNameContains}} is not null, verify the
     campaign name contains that text; if it does not, make NO change and report a naming
     mismatch.
  b. list_google_ads_shared_budgets(customer_id '{{customerId}}', budget_resource_name = that
     resource name) and read amountMicros from the single budget returned. Call this LIVE_BUDGET.
get_google_ads_campaign_details does NOT return the budget amount. Call (b) as well; do not
infer the amount from anywhere else.

STEP 5 - Read state.
Call get_document on 'CampaignBudgetState' / '{{budgetStateDocId}}'. Fields used:
  normalBudgetMicros      the budget to return to
  increaseActive          whether an increase is currently applied
  increaseStartDate       date the current increase began
  increaseDayNumber       which day of the {{maxConsecutiveDays}}-day window this is
  lastAppliedBudgetMicros the elevated amount this logic last wrote
  increasedDays           list of YYYY-MM-DD dates on which an increase was applied
If the document does not exist, create it with normalBudgetMicros = LIVE_BUDGET,
increaseActive false, increaseDayNumber 0, increasedDays [], then continue.
If the document exists but normalBudgetMicros is missing, make NO change, log notes
'missing normal budget - alert' and stop. Do not guess a value.

STEP 6 - Decide. Evaluate in this order and take the FIRST case that applies.
  A. increaseActive is true AND LIVE_BUDGET != lastAppliedBudgetMicros
     A person changed the budget by hand and the human decision wins. Adopt the live value:
     normalBudgetMicros = LIVE_BUDGET, increaseActive false, increaseDayNumber 0. Make NO budget
     call. Notes 'manual override detected'.
  B. increaseActive is true AND increaseDayNumber >= {{maxConsecutiveDays}}
     Maximum duration reached. Call update_google_ads_campaign_budget with
     new_budget_micros = normalBudgetMicros. Set increaseActive false, increaseDayNumber 0.
  C. increaseActive is true AND SEVERE is false
     Event no longer qualifies. Revert exactly as in case B.
  D. increaseActive is true AND SEVERE is true
     Still qualifying and inside the window. The budget is already elevated, so make NO budget
     call. Set increaseDayNumber = increaseDayNumber + 1 and add TODAY to increasedDays if absent.
  E. increaseActive is false AND SEVERE is true
     New event. First check the rolling cap: count the entries in increasedDays falling in the
     trailing {{rollingWindowDays}} days including TODAY. If that count is
     {{maxIncreasedDaysInWindow}} or more, make NO change and note 'rolling
     {{rollingWindowDays}}-day cap reached'. Otherwise:
       TARGET = normalBudgetMicros * (1 + {{severeModifiers.budgetBumpPct}} / 100)
       if the optional ceiling is not null and TARGET > ceiling, TARGET = ceiling
     Compute TARGET from normalBudgetMicros, NEVER from LIVE_BUDGET, so an increase can never
     compound across runs. Call update_google_ads_campaign_budget(customer_id '{{customerId}}',
     campaign_id '{{campaignId}}', new_budget_micros = TARGET). Set increaseActive true,
     increaseStartDate TODAY, increaseDayNumber 1, lastAppliedBudgetMicros TARGET, and add TODAY
     to increasedDays.
  F. Otherwise, no change.

STEP 7 - Persist state.
Call set_document on 'CampaignBudgetState' / '{{budgetStateDocId}}' with merge true, writing
normalBudgetMicros, increaseActive, increaseStartDate, increaseDayNumber,
lastAppliedBudgetMicros, increasedDays (dropping entries older than {{rollingWindowDays}} days)
and lastRunDate '{{today}}'.
If the budget call returned dry_run=true the account did not change: do NOT set increaseActive
true, do NOT add TODAY to increasedDays, and do NOT change lastAppliedBudgetMicros.

STEP 8 - Change log. Always write one row, whether or not anything changed.
Call set_document on collection 'ChangeLog', document_id
'{{today}}_{{campaignId}}_budget_{{runId}}', with:
  runId '{{runId}}', timestampUtc '{{nowIsoUtc}}', timestampLocal '{{nowIsoLocal}}',
  account '{{account}}', customerId '{{customerId}}', campaignId '{{campaignId}}',
  campaignName (as returned by the API, never from config), weatherLocation '{{city}}',
  condition (Cold, Rain, Snow or None), assetGroupAction 'no change', severityMet,
  triggerDetail (every test you ran, each with the observed value and the threshold it was
  compared against), budgetBeforeMicros, budgetAfterMicros (or 'unchanged'), increaseDayNumber,
  increasedDaysInTrailing{{rollingWindowDays}}, mode ('live' or 'log-only') and notes.

RULES
- Never change the bidding strategy and never set, alter or remove a target ROAS. Daily budget
  is the only lever in scope.
- Never change campaign status, and never touch asset groups in this playbook.
- Report which test passed, quoting both the observed value and the threshold.
"""

GLOBAL_INSTRUCTION = """You manage Arc'teryx weather-triggered Performance Max campaigns. Every decision must be
justified by a numeric weather signal you actually retrieved.

NON-NEGOTIABLE SAFETY RULES:
1. NEVER pause an always-on asset group. The asset group tools enforce this; if a tool returns
   blocked_by_guard, accept it and move on. Do not attempt a workaround.
2. Only ever change asset groups that the tools report as managed weather asset groups.
3. Bidding is Maximise Conversions. NEVER set, alter or remove a target ROAS or change the
   bidding strategy. Daily budget is the only budget lever in scope.
4. Never change campaign status. Campaigns remain enabled at all times.
5. If any weather tool returns success=false, or hours_received is less than hours_requested,
   follow your playbook's failure path. Never guess a weather value.
6. Never hardcode or assume coordinates, campaign names or asset group names. Resolve them from
   Firestore and from the Google Ads API at run time.
7. Report every action you take with the weather number that caused it.
8. Append a row to Firestore collection 'ChangeLog' for every decision, including the decisions
   that changed nothing.
"""

# Playbooks are account-agnostic: the same two documents serve every account and
# every city. They are included in each sample file so either can be uploaded on
# its own and produce a working system; set() is idempotent, so uploading both
# simply writes the identical document twice.
SHARED_PLAYBOOKS = [
    {
        "collection_name": "Playbooks",
        "document_id": "weather_asset_groups",
        "data": {
            "description": (
                "Advance weather triggering, spec section 4. Enables and pauses weather asset "
                "groups from a forward-looking forecast, with a trailing window."
            ),
            "requiredParams": ["city"],
            "defaults": {
                "geo": "(unset)",
                "campaignNameContains": None,
            },
            "template": ASSET_GROUP_PLAYBOOK,
        },
    },
    {
        "collection_name": "Playbooks",
        "document_id": "severe_budget",
        "data": {
            "description": (
                "Severe weather budget adjustment, spec section 5. Raises the daily budget when "
                "an event is materially worse than the seasonal norm, then reverts."
            ),
            "requiredParams": [
                "city",
                "severeModifiers.severeRainMm",
                "severeModifiers.severeSnowMm",
                "severeModifiers.severeColdMarginC",
                "severeModifiers.relativeMultiplier",
                "severeModifiers.budgetBumpPct",
            ],
            "defaults": {
                "geo": "(unset)",
                "campaignNameContains": None,
                "maxConsecutiveDays": 5,
                "rollingWindowDays": 30,
                "maxIncreasedDaysInWindow": 10,
                "severeModifiers": {"maxDailyBudgetMicros": None},
            },
            "template": SEVERE_BUDGET_PLAYBOOK,
        },
    },
]

CONFIG = [
    {
        "collection_name": "CustomerInstructions",
        "document_id": "1234567890",
        "data": {"instruction": GLOBAL_INSTRUCTION},
    },
    {
        "collection_name": "WeatherConditions",
        "document_id": "default",
        "data": {
            "_comment": (
                "Shared activation rules from spec section 3.2. These are the same for every "
                "city; per-campaign severity thresholds live in the campaign's "
                "params.severeModifiers. Changing a threshold here changes it everywhere."
            ),
            "lookAheadHours": 24,
            "trailingWindowHours": 24,
            "conditions": [
                {
                    "name": "Cold",
                    "assetGroupToken": "_COLD_",
                    "test": "min_temperature_c < 9.0",
                    "severityEligible": True,
                },
                {
                    "name": "Rain",
                    "assetGroupToken": "_RAIN_",
                    "test": "max_rain_rate_mm_per_h >= 0.2 AND rain_present is true",
                    "severityEligible": True,
                },
                {
                    "name": "Snow",
                    "assetGroupToken": "_SNOW_",
                    "test": "snow_present is true",
                    "severityEligible": True,
                },
                {
                    "name": "Warm",
                    "assetGroupToken": "_SUN_",
                    "test": "max_temperature_c > 10.0",
                    "severityEligible": False,
                },
            ],
        },
    },
    *SHARED_PLAYBOOKS,
    {
        "collection_name": "GoogleAdsConfig",
        "document_id": "1234567890",
        "data": {
            "customerId": 1234567890,
            # Informational only. The login-customer-id header is taken from the
            # GOOGLE_ADS_LOGIN_CUSTOMER_ID environment variable, set from
            # google_ads_login_customer_id in config.yaml. Nothing reads this field.
            "loginCustomerId": "REPLACE_WITH_MCC_ID",
            "account": "Canada",
            "instruction": "Arc'teryx Canada weather-triggered PMax.",
            "schedule": {
                "timezone": "America/Toronto",
                "runsPerDay": 2,
                "runHours": [6, 18],
            },
            "notifications": {
                "summaryRecipients": ["REPLACE_WITH_TEAM_ALIAS@example.com"],
            },
            "changeVolumeGuard": {
                "enabled": True,
                "maxFractionOfEligibleCampaigns": 0.25,
            },
            "weatherConditionsId": "default",
            "campaigns": [
                {
                    "campaignId": 1111111111,
                    "playbooks": ["weather_asset_groups", "severe_budget"],
                    "params": {
                        "city": "Vancouver BC",
                        "geo": "VAN",
                        "campaignNameContains": "VAN",
                        "severeModifiers": {
                            "severeRainMm": 45.0,
                            "severeSnowMm": 10.0,
                            "severeColdMarginC": 5.0,
                            "relativeMultiplier": 1.5,
                            "budgetBumpPct": 50,
                            "maxDailyBudgetMicros": 200000000,
                        },
                    },
                },
                {
                    "campaignId": 3333333333,
                    "playbooks": ["weather_asset_groups", "severe_budget"],
                    "params": {
                        "city": "Calgary AB",
                        "geo": "YYC",
                        "campaignNameContains": "YYC",
                        "severeModifiers": {
                            "severeRainMm": 30.0,
                            "severeSnowMm": 12.0,
                            "severeColdMarginC": 5.0,
                            "relativeMultiplier": 1.5,
                            "budgetBumpPct": 50,
                        },
                    },
                },
                {
                    # No severeModifiers: asset groups are still toggled, but no
                    # budget increase is possible. Spec section 7.
                    "campaignId": 4444444444,
                    "playbooks": ["weather_asset_groups", "severe_budget"],
                    "params": {
                        "city": "Toronto ON",
                        "geo": "YYZ",
                        "campaignNameContains": "YYZ",
                    },
                },
            ],
        },
    },
    {
        "collection_name": "CampaignBudgetState",
        "document_id": "1234567890_1111111111",
        "data": {
            "customerId": "1234567890",
            "campaignId": "1111111111",
            "weatherLocation": "Vancouver BC",
            "normalBudgetMicros": 100000000,
            "increaseActive": False,
            "increaseStartDate": None,
            "increaseDayNumber": 0,
            "lastAppliedBudgetMicros": None,
            "increasedDays": [],
            "lastRunDate": None,
        },
    },
    {
        "collection_name": "AssetGroupState",
        "document_id": "1234567890_1111111111",
        "data": {
            "customerId": "1234567890",
            "campaignId": "1111111111",
            "weatherLocation": "Vancouver BC",
            "conditionHistory": {
                "Cold": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
                "Rain": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
                "Snow": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
                "Warm": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
            },
            "lastRunId": None,
            "lastRunAt": None,
        },
    },
]

# The sandbox account is a live ADSTA test property. Its asset groups are named
# Arcteryx_WEATHER_Snow rather than ARC_Weather_Snow_VAN_..., so the token that
# identifies a condition differs from production. That is exactly why the token
# lives in a WeatherConditions document rather than in the playbook text: the
# same playbook serves both naming conventions.
SANDBOX_CONFIG = [
    *SHARED_PLAYBOOKS,
    {
        "collection_name": "CustomerInstructions",
        "document_id": "5341114500",
        "data": {"instruction": GLOBAL_INSTRUCTION},
    },
    {
        "collection_name": "WeatherConditions",
        "document_id": "sandbox",
        "data": {
            "_comment": (
                "Sandbox condition set. Tokens have no trailing underscore because the "
                "sandbox asset groups are named Arcteryx_WEATHER_Snow, not "
                "ARC_Weather_Snow_VAN_ENG_Neutral_Null_FW25. All four spec conditions "
                "now have a matching asset group in the sandbox campaign "
                "(Arcteryx_WEATHER_Cold, _Rain, _Snow and _Sun), so every condition "
                "path can be validated end to end. Each token matches exactly one "
                "asset group; the always-on group Arcteryx_AO_Core matches none and "
                "is protected from pausing."
            ),
            "lookAheadHours": 24,
            "trailingWindowHours": 24,
            "conditions": [
                {
                    "name": "Cold",
                    "assetGroupToken": "_COLD",
                    "test": "min_temperature_c < 9.0",
                    "severityEligible": True,
                },
                {
                    "name": "Rain",
                    "assetGroupToken": "_RAIN",
                    "test": "max_rain_rate_mm_per_h >= 0.2 AND rain_present is true",
                    "severityEligible": True,
                },
                {
                    "name": "Snow",
                    "assetGroupToken": "_SNOW",
                    "test": "snow_present is true",
                    "severityEligible": True,
                },
                {
                    "name": "Warm",
                    "assetGroupToken": "_SUN",
                    "test": "max_temperature_c > 10.0",
                    "severityEligible": False,
                },
            ],
        },
    },
    {
        "collection_name": "GoogleAdsConfig",
        "document_id": "5341114500",
        "data": {
            "customerId": 5341114500,
            # Informational only; see the note on the Canada config above.
            "loginCustomerId": "REPLACE_WITH_MCC_ID",
            "account": "Canada",
            "instruction": (
                "Arc'teryx weather-triggered PMax sandbox test. Vancouver signals against "
                "the ADSTA test campaign."
            ),
            "schedule": {
                "timezone": "America/Vancouver",
                "runsPerDay": 2,
                "runHours": [6, 18],
            },
            "notifications": {"summaryRecipients": []},
            "changeVolumeGuard": {
                "enabled": True,
                "maxFractionOfEligibleCampaigns": 0.25,
            },
            "weatherConditionsId": "sandbox",
            "campaigns": [
                {
                    "campaignId": 24252893412,
                    "playbooks": ["weather_asset_groups", "severe_budget"],
                    "params": {
                        "city": "Vancouver BC",
                        "geo": "VAN",
                        "campaignNameContains": "ADSTA Weather PMax Test",
                        "severeModifiers": {
                            "severeRainMm": 45.0,
                            "severeSnowMm": 10.0,
                            "severeColdMarginC": 5.0,
                            "relativeMultiplier": 1.5,
                            "budgetBumpPct": 50,
                            "maxDailyBudgetMicros": 2000000,
                        },
                    },
                },
            ],
        },
    },
    {
        "collection_name": "CampaignBudgetState",
        "document_id": "5341114500_24252893412",
        "data": {
            "customerId": "5341114500",
            "campaignId": "24252893412",
            "weatherLocation": "Vancouver BC",
            "normalBudgetMicros": 1000000,
            "increaseActive": False,
            "increaseStartDate": None,
            "increaseDayNumber": 0,
            "lastAppliedBudgetMicros": None,
            "increasedDays": [],
            "lastRunDate": None,
        },
    },
    {
        "collection_name": "AssetGroupState",
        "document_id": "5341114500_24252893412",
        "data": {
            "customerId": "5341114500",
            "campaignId": "24252893412",
            "weatherLocation": "Vancouver BC",
            "conditionHistory": {
                "Cold": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
                "Rain": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
                "Snow": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
                "Warm": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
            },
            "lastRunId": None,
            "lastRunAt": None,
        },
    },
]


def _write(path: str, documents) -> None:
    """Writes a config file, preserving any existing ClimateBaselines entries.

    Climate baselines are produced by infra/scripts/baselines/build_climate_baselines.py
    from real reanalysis data, so they are carried over rather than regenerated here.

    Args:
        path: Destination path relative to the repository's agentic_dsta directory.
        documents: The documents to write after the preserved baselines.
    """
    target = pathlib.Path(path)
    baselines = []
    if target.exists():
        existing = json.loads(target.read_text())
        baselines = [
            item for item in existing if item.get("collection_name") == "ClimateBaselines"
        ]

    output = baselines + documents
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {target} with {len(output)} documents.")


def main() -> None:
    """Regenerates both sample configs."""
    _write("infra/config/samples/arcteryx_test_firestore_config.json", CONFIG)
    _write("infra/config/samples/arcteryx_sandbox_firestore_config.json", SANDBOX_CONFIG)


if __name__ == "__main__":
    main()
