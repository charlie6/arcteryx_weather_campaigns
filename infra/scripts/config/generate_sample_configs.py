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

`ClimateBaselines` numbers are never invented here. They come from real reanalysis
data via `infra/scripts/baselines/build_climate_baselines.py` and are carried over
from whatever is already in the target file. This script does reshape them: the
three fields a playbook actually reads stay at the top level and everything else
is nested under `_provenance`, so no one mistakes a monthly average for a
threshold. Where the two sample files disagree about a city, the richer document
wins, which reconciles a divergence the per-file copies had already accumulated.
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
These are the spec section 3.2 activation rules. Use the named signal exactly; do not
substitute a different temperature or an average.
  Cold : min_temperature_c < {{activation.coldBelowC}}
         (the LOWEST temperature in the look-ahead)
         asset group name contains '{{assetGroupTokens.Cold}}'
  Rain : max_rain_rate_mm_per_h >= {{activation.rainRateMmPerH}} AND rain_present is true
         asset group name contains '{{assetGroupTokens.Rain}}'
  Snow : snow_present is true
         asset group name contains '{{assetGroupTokens.Snow}}'
  Sunny: sunny_daytime_hours >= {{activation.sunnyMinHours}}
         (daylight hours forecast CLEAR, MOSTLY_CLEAR or PARTLY_CLOUDY; temperature is
         NOT part of this test)
         asset group name contains '{{assetGroupTokens.Sunny}}'
Sunny and Rain can both be active on the same day, for example a sunny morning followed by
an afternoon shower. Evaluate each on its own and do not suppress one because another is
active.
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
  Look-ahead window     : {{lookAheadHours}} hours
  State document        : 'CampaignBudgetState' / '{{budgetStateDocId}}'

BUDGET LEVERS for this campaign. These control the SIZE and DURATION of an increase only.
What counts as severe weather is a property of the city, not of the campaign, and is read
from Firestore in STEP 1.
  Budget bump           : {{severeModifiers.budgetBumpPct}} percent
  Optional ceiling      : {{severeModifiers.maxDailyBudgetMicros}} micros (null means no ceiling)
  Max consecutive days  : {{maxConsecutiveDays}}
  Rolling window        : {{maxIncreasedDaysInWindow}} increased days per {{rollingWindowDays}} days

STEP 1 - Resolve coordinates and severity thresholds.
Call get_document on collection 'ClimateBaselines', document_id '{{city}}'. From that one
document read exactly these four fields and no others:
  latitude, longitude               coordinates for the forecast call
  severeThresholds.rainMm           rain threshold, mm accumulated over the window
  severeThresholds.snowMmSwe        snow threshold, mm water equivalent over the window
  severeThresholds.coldC            cold threshold, degrees Celsius
Each threshold is a percentile of this city's own multi-decade record, so it already means
'unusual for here'. Use it exactly as stored. Do NOT scale, multiply or offset it, and do NOT
combine it with any other number in the document.
The document also has a '_provenance' object recording how the thresholds were derived
(monthly averages, wet-day counts, data source). NOTHING in _provenance is a threshold.
Never read a threshold from it.
Never assume coordinates or thresholds. If the document does not exist, or either coordinate is
missing, make NO change, log notes 'missing ClimateBaselines document' and stop.
If a threshold needed for one condition is missing or null, that condition cannot qualify:
treat it as not severe, log a warning, and carry on with the others.

STEP 2 - Forecast.
Call get_24h_weather_signals with the latitude and longitude from STEP 1 and hours
{{lookAheadHours}}.
If success is false, or hours_received is less than hours_requested: make NO budget change. If
an increase is currently active, LEAVE IT IN PLACE and let the clock continue, because
reverting on a transient failure and re-applying next run would create churn. Write a change
log row with notes 'weather API failure' and stop.

STEP 3 - Severity test. One test per condition, comparing the forecast directly against that
city's threshold from STEP 1.
  RAIN severe : rain_accumulation_mm >= severeThresholds.rainMm
  SNOW severe : snow_accumulation_mm >= severeThresholds.snowMmSwe
  COLD severe : min_temperature_c <= severeThresholds.coldC
Note the direction: rain and snow trigger at or ABOVE the threshold, cold at or BELOW it.
Sunny NEVER qualifies for a budget change.
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
                # Spec section 3.2 activation rules, the same for every city.
                # A campaign may override any one of these in
                # params.activation; the rest keep these values.
                "activation": {
                    "coldBelowC": 9.0,
                    # Daylight hours in the look-ahead forecast as clear,
                    # mostly clear or partly cloudy.
                    "sunnyMinHours": 3,
                    "rainRateMmPerH": 0.2,
                },
            },
            "template": ASSET_GROUP_PLAYBOOK,
        },
    },
    {
        "collection_name": "Playbooks",
        "document_id": "severe_budget",
        "data": {
            "description": (
                "Severe weather budget adjustment, spec section 5. Raises the daily budget "
                "when the forecast crosses that city's severeThresholds in ClimateBaselines, "
                "then reverts. A campaign opts in by declaring budgetBumpPct."
            ),
            "requiredParams": [
                "city",
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
    *SHARED_PLAYBOOKS,
    {
        "collection_name": "GoogleAdsConfig",
        "document_id": "1234567890",
        "data": {
            # The document id IS the customer id, and the login-customer-id
            # header comes from the GOOGLE_ADS_LOGIN_CUSTOMER_ID environment
            # variable (set from google_ads_login_customer_id in config.yaml).
            # Neither is repeated here.
            "account": "Canada",
            "schedule": {
                "timezone": "America/Toronto",
                # Used to convert trailingWindowHours into a number of runs.
                # The actual run times come from Cloud Scheduler, not from here.
                "runsPerDay": 2,
            },
            # Detect-only. The run logs an error if the cap is breached, but the
            # changes are already applied by then; real enforcement needs a
            # two-phase run. See _check_change_volume_guard.
            "changeVolumeGuard": {
                "enabled": True,
                "maxFractionOfEligibleCampaigns": 0.25,
            },
            # Forecast window, and how long an asset group stays live after
            # its condition leaves the forecast.
            "lookAheadHours": 24,
            "trailingWindowHours": 24,
            # Substring that identifies each condition's asset group in THIS
            # account's naming convention (ARC_Weather_Cold_VAN_..._FW25).
            # Each token must match exactly one asset group per campaign.
            "assetGroupTokens": {
                "Cold": "_COLD_",
                "Rain": "_RAIN_",
                "Snow": "_SNOW_",
                "Sunny": "_SUN_",
            },
            "campaigns": [
                {
                    "campaignId": 1111111111,
                    "playbooks": ["weather_asset_groups", "severe_budget"],
                    "params": {
                        "city": "Vancouver BC",
                        "geo": "VAN",
                        "campaignNameContains": "VAN",
                        # Budget levers only. What counts as severe weather is
                        # per city and comes from
                        # ClimateBaselines/<city>.severeThresholds.
                        "severeModifiers": {
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
                            "budgetBumpPct": 50,
                        },
                    },
                },
                {
                    # No severeModifiers, so severe_budget is missing its
                    # required budgetBumpPct and is skipped with a log line.
                    # Asset groups are still toggled. Spec section 7.
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
                "Sunny": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
            },
            "lastRunId": None,
            "lastRunAt": None,
        },
    },
]

# The sandbox account is a live ADSTA test property. Its asset groups are named
# Arcteryx_WEATHER_Snow rather than ARC_Weather_Snow_VAN_..., so the token that
# identifies a condition differs from production. That is exactly why the tokens
# live in the account's GoogleAdsConfig.assetGroupTokens rather than in the
# playbook text: the same playbook serves both naming conventions.
SANDBOX_CONFIG = [
    *SHARED_PLAYBOOKS,
    {
        "collection_name": "CustomerInstructions",
        "document_id": "5341114500",
        "data": {"instruction": GLOBAL_INSTRUCTION},
    },
    {
        "collection_name": "GoogleAdsConfig",
        "document_id": "5341114500",
        "data": {
            "account": "Canada",
            "schedule": {
                "timezone": "America/Vancouver",
                "runsPerDay": 2,
            },
            # Detect-only. The run logs an error if the cap is breached, but the
            # budget changes are already applied by then; real enforcement needs
            # a two-phase run. See _check_change_volume_guard.
            "changeVolumeGuard": {
                "enabled": True,
                "maxFractionOfEligibleCampaigns": 0.25,
            },
            "lookAheadHours": 24,
            "trailingWindowHours": 24,
            # No trailing underscore: sandbox groups are named
            # Arcteryx_WEATHER_Cold, not ARC_Weather_Cold_VAN_..._FW25. Each
            # token matches exactly one group; the always-on Arcteryx_AO_Core
            # matches none and is protected from pausing in code.
            "assetGroupTokens": {
                "Cold": "_COLD",
                "Rain": "_RAIN",
                "Snow": "_SNOW",
                "Sunny": "_SUN",
            },
            "campaigns": [
                {
                    "campaignId": 24252893412,
                    "playbooks": ["weather_asset_groups", "severe_budget"],
                    "params": {
                        "city": "Vancouver BC",
                        "geo": "VAN",
                        "campaignNameContains": "ADSTA Weather PMax Test",
                        # Budget levers only. What counts as severe weather is
                        # per city, not per campaign, and comes from
                        # ClimateBaselines.severeThresholds.
                        "severeModifiers": {
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
                "Sunny": {"runsSinceLastSeen": 99, "currentStatus": "PAUSED"},
            },
            "lastRunId": None,
            "lastRunAt": None,
        },
    },
]


TEST_CONFIG_PATH = "infra/config/samples/arcteryx_test_firestore_config.json"
SANDBOX_CONFIG_PATH = "infra/config/samples/arcteryx_sandbox_firestore_config.json"

# The only ClimateBaselines fields any playbook reads. Everything else in the
# document describes how the numbers were derived.
#
# The split exists because a flat namespace invited the reader to treat any
# plausible-looking number as a threshold. severeThresholds sat directly beside
# monthlyRainMmPerWetDay, and during QA the two were understandably confused.
# The provenance still answers "where did 48.0 come from", so it is nested
# rather than deleted.
LIVE_BASELINE_FIELDS = ("latitude", "longitude", "severeThresholds")


def _normalise_baseline(document):
    """Splits a ClimateBaselines document into live fields and provenance.

    Idempotent: a document already carrying '_provenance' is returned with the
    same shape, so repeated regeneration does not nest it further.

    Args:
        document: A ClimateBaselines entry from a sample file.

    Returns:
        The entry with only LIVE_BASELINE_FIELDS at the top level of 'data'
        and every other field under 'data._provenance'.
    """
    data = dict(document.get("data", {}))
    provenance = dict(data.pop("_provenance", {}))

    for key in list(data):
        if key not in LIVE_BASELINE_FIELDS:
            provenance[key] = data.pop(key)

    if provenance:
        data["_provenance"] = provenance

    return {**document, "data": data}


def _baseline_rank(document):
    """Scores a baseline document by completeness, for de-duplication.

    Args:
        document: A normalised ClimateBaselines entry.

    Returns:
        A sort key where a document carrying severeThresholds always wins.
    """
    data = document.get("data", {})
    return (
        1 if data.get("severeThresholds") else 0,
        len(json.dumps(data, sort_keys=True)),
    )


def _canonical_baselines(paths):
    """Builds the best-known baseline document per city across the sample files.

    The samples each preserved their own copy and had already diverged: the
    sandbox seed carried severeThresholds while the test seed predated the
    field, and nothing would ever have reconciled them. Taking the richest
    version of each city heals that and keeps the two files consistent.

    Args:
        paths: Sample file paths to read.

    Returns:
        A mapping of city document id to normalised ClimateBaselines entry.
    """
    canonical = {}
    for path in paths:
        target = pathlib.Path(path)
        if not target.exists():
            continue
        for item in json.loads(target.read_text()):
            if item.get("collection_name") != "ClimateBaselines":
                continue
            normalised = _normalise_baseline(item)
            city = normalised.get("document_id")
            incumbent = canonical.get(city)
            if incumbent is None or _baseline_rank(normalised) > _baseline_rank(incumbent):
                canonical[city] = normalised
    return canonical


def _write(path: str, documents, canonical) -> None:
    """Writes a config file, preserving the ClimateBaselines entries it already had.

    Climate baselines come from real reanalysis data via
    infra/scripts/baselines/build_climate_baselines.py, so they are carried over
    rather than regenerated. Each file keeps its own set of cities; only the
    content of those documents is taken from the canonical set.

    Args:
        path: Destination path relative to the repository's agentic_dsta directory.
        documents: The documents to write after the preserved baselines.
        canonical: Best-known baseline per city, from _canonical_baselines.
    """
    target = pathlib.Path(path)
    baselines = []
    if target.exists():
        for item in json.loads(target.read_text()):
            if item.get("collection_name") != "ClimateBaselines":
                continue
            city = item.get("document_id")
            baselines.append(canonical.get(city) or _normalise_baseline(item))

    missing = [
        b["document_id"]
        for b in baselines
        if not b.get("data", {}).get("severeThresholds")
    ]
    if missing:
        # Not fatal here, but the severe_budget playbook stops for these cities
        # rather than guessing, so surface it at generation time.
        print(f"  WARNING: no severeThresholds for {', '.join(missing)}")

    _check_campaign_cities(documents, {b["document_id"] for b in baselines})

    output = baselines + documents
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {target} with {len(output)} documents.")


def _check_campaign_cities(documents, baseline_cities) -> None:
    """Warns about campaigns whose city has no ClimateBaselines document.

    A campaign referencing a city that was never built is not a loud failure at
    run time: severe_budget reads the missing document, logs a note and stops.
    The budget simply never moves. Surfacing it here turns a silent no-op into
    something a reviewer sees before the config is uploaded.

    Args:
        documents: The documents about to be written.
        baseline_cities: Document ids of the baselines in the same file.
    """
    for document in documents:
        if document.get("collection_name") != "GoogleAdsConfig":
            continue
        for campaign in document.get("data", {}).get("campaigns", []):
            city = (campaign.get("params") or {}).get("city")
            if city and city not in baseline_cities:
                print(
                    f"  WARNING: campaign {campaign.get('campaignId')} uses city "
                    f"'{city}', which has no ClimateBaselines document. "
                    "severe_budget will stop for it and no budget will change."
                )


def main() -> None:
    """Regenerates both sample configs."""
    paths = [TEST_CONFIG_PATH, SANDBOX_CONFIG_PATH]
    canonical = _canonical_baselines(paths)
    _write(TEST_CONFIG_PATH, CONFIG, canonical)
    _write(SANDBOX_CONFIG_PATH, SANDBOX_CONFIG, canonical)


if __name__ == "__main__":
    main()
