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
"""This agent is responsible for managing marketing campaigns for customers stored in Firestore."""
import asyncio
import concurrent.futures
import dataclasses
import datetime
import functools
import logging
import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

from google.genai import Client
from google.genai import types
from google.adk.models.google_llm import Gemini
from google.adk import apps
from google.adk import runners
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from agentic_dsta.agents.decision_agent import playbooks as playbooks_lib
from agentic_dsta.config_sync import sync as config_sync
from agentic_dsta.core import telemetry
from agentic_dsta.tools.firestore.firestore_toolset import FirestoreToolset
from google.adk import agents
from agentic_dsta.tools.google_ads.google_ads_getter import GoogleAdsGetterToolset
from agentic_dsta.tools.google_ads.google_ads_updater import GoogleAdsUpdaterToolset
from agentic_dsta.tools.google_ads.google_ads_asset_groups import GoogleAdsAssetGroupToolset
from agentic_dsta.tools.sa360.sa360_toolset import SA360Toolset
from agentic_dsta.tools.weather.weather_signals import WeatherSignalsToolset



logger = logging.getLogger(__name__)

# Default model, can be overridden.
#
# gemini-2.5-flash retires on 2026-10-20. gemini-3.5-flash is the GA successor
# (retirement 2027-05-19 or later).
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")

# The Gemini serving location is deliberately separate from GOOGLE_CLOUD_LOCATION.
#
# GOOGLE_CLOUD_LOCATION is the Cloud Run region (us-central1) and is also used
# for Firestore and other regional resources. No Gemini 3.x model is served from
# any single US region, and for gemini-3.5-flash pay-as-you-go is only offered on
# the `global`, `us`, and `eu` endpoints. So the model call has to target a
# multi-region endpoint even though the service itself stays in us-central1.
#
# `us` keeps ML processing inside the United States. Switch to `global` if you
# would rather trade residency for the widest capacity pool (fewer 429s).
GEMINI_LOCATION = (
    os.environ.get("GEMINI_LOCATION")
    or os.environ.get("GOOGLE_CLOUD_LOCATION")
    or "us"
)
LOCATION = GEMINI_LOCATION

# Campaigns are processed in parallel, each in its own worker thread. At about
# 2-4 minutes per campaign, 5 at a time handles ~20 campaigns well inside the
# 30-minute scheduler deadline while keeping Gemini and Google Ads request
# rates modest. Raise it for larger accounts; watch for 429s.
MAX_CONCURRENT_CAMPAIGNS_ENV = "ADSTA_MAX_CONCURRENT_CAMPAIGNS"
DEFAULT_MAX_CONCURRENT_CAMPAIGNS = 5
MAX_CONCURRENT_CAMPAIGNS_LIMIT = 20

# One playbook normally takes 30-220s. The limit stops a hung call from holding
# the run past the scheduler deadline.
PLAYBOOK_TIMEOUT_ENV = "ADSTA_PLAYBOOK_TIMEOUT_SECONDS"
DEFAULT_PLAYBOOK_TIMEOUT_SECONDS = 600


def get_current_datetime() -> Dict[str, Any]:
    """Returns the current date, time, year, and timezone in UTC.

    Use this tool to determine today's date and calculate date ranges for forecasts
    or historical comparisons without writing code.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "current_date": now.strftime("%Y-%m-%d"),
        "current_time_utc": now.strftime("%H:%M:%S"),
        "current_year": now.year,
        "current_month": now.month,
        "current_day": now.day,
        "iso_timestamp": now.isoformat(),
        "timezone": "UTC",
    }


class DateTimeToolset(BaseToolset):
    """Toolset providing date and time utilities for the decision agent."""

    async def get_tools(self, readonly_context: Optional[Any] = None) -> List[FunctionTool]:
        return [FunctionTool(func=get_current_datetime)]


def create_agent(
    instruction: str,
    model: str = DEFAULT_MODEL,
    run_context: Optional[Dict[str, Any]] = None,
) -> agents.LlmAgent:
    """
    Creates a new instance of the decision agent with specific instructions.

    Args:
        instruction: The system instruction for this agent instance.
        model: The Gemini model to use.
        run_context: Labels (customer_id, usecase, run_id, campaign_id,
            playbook_id) attached to the monitoring events this agent emits.

    Returns:
        A configured LlmAgent instance.
    """
    tools = [
        GoogleAdsGetterToolset(),
        GoogleAdsUpdaterToolset(),
        GoogleAdsAssetGroupToolset(),
        WeatherSignalsToolset(),
        FirestoreToolset(),
        SA360Toolset(),
        DateTimeToolset(),
    ]

    client = Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION,
    )

    # Pass the client through the `client` field rather than assigning to
    # `model.api_client` after construction. In google-adk 2.x, Gemini is a
    # Pydantic model and `api_client` is a cached_property that returns
    # `self.client` when set; assigning over the property is no longer the
    # supported path.
    configured_model = Gemini(model=model, client=client)

    return agents.LlmAgent(
        name="decision_agent",
        instruction=instruction,
        model=configured_model,
        tools=tools,
        # Observe-only hooks: emit mutation_applied / tool_error / model_error
        # events for Cloud Monitoring. They never alter tool results.
        **telemetry.make_agent_callbacks(run_context),
    )


def _log_agent_chunk(chunk: Any, campaign_id: str) -> None:
    """Safely extracts and logs events, tool calls, and outputs from an agent execution chunk.

    Args:
        chunk: The event or message chunk yielded by runner.run_async.
        campaign_id: ID of the campaign being processed, used for context tagging.
    """
    try:
        # Check direct text attribute
        if hasattr(chunk, "text") and chunk.text:
            text = str(chunk.text).strip()
            if text:
                logger.info(
                    "[Campaign %s] Agent output: %s",
                    campaign_id,
                    text,
                    extra={"campaign_id": str(campaign_id), "chunk_type": "text"},
                )

        # Check content object with parts
        content = getattr(chunk, "content", None)
        if content:
            parts = getattr(content, "parts", [])
            for part in parts:
                # Function/Tool call
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    name = getattr(fc, "name", "unknown_tool")
                    args = getattr(fc, "args", {})
                    logger.info(
                        "[Campaign %s] Invoking tool '%s' with args: %s",
                        campaign_id,
                        name,
                        args,
                        extra={
                            "campaign_id": str(campaign_id),
                            "tool_name": name,
                            "tool_args": args,
                        },
                    )
                # Function/Tool response
                elif hasattr(part, "function_response") and part.function_response:
                    fr = part.function_response
                    name = getattr(fr, "name", "unknown_tool")
                    resp = getattr(fr, "response", "")
                    resp_str = str(resp)
                    if len(resp_str) > 500:
                        resp_str = resp_str[:500] + "... [truncated]"
                    logger.info(
                        "[Campaign %s] Tool '%s' response: %s",
                        campaign_id,
                        name,
                        resp_str,
                        extra={
                            "campaign_id": str(campaign_id),
                            "tool_name": name,
                        },
                    )
                # Thought / Reasoning text
                elif hasattr(part, "text") and part.text:
                    thought = str(part.text).strip()
                    if thought:
                        logger.info(
                            "[Campaign %s] Agent reasoning: %s",
                            campaign_id,
                            thought,
                            extra={
                                "campaign_id": str(campaign_id),
                                "chunk_type": "reasoning",
                            },
                        )

        # Check for event actions
        if hasattr(chunk, "actions") and chunk.actions:
            logger.info(
                "[Campaign %s] Agent action: %s",
                campaign_id,
                chunk.actions,
                extra={"campaign_id": str(campaign_id)},
            )
    except Exception as err:
        logger.debug(
            "[Campaign %s] Unable to parse chunk (%s): %s",
            campaign_id,
            err,
            str(chunk)[:200],
            extra={"campaign_id": str(campaign_id)},
        )


def _load_playbook_library(
    firestore_toolset: FirestoreToolset, playbook_ids: Set[str]
) -> Dict[str, Dict[str, Any]]:
    """Loads the referenced playbook templates from Firestore.

    Playbooks are shared across every campaign and account, so they are fetched
    once per run rather than once per campaign.

    Args:
        firestore_toolset: Client used to read the 'Playbooks' collection.
        playbook_ids: The distinct playbook ids referenced by the config.

    Returns:
        Mapping of playbook id to its document data. Ids that could not be
        loaded are absent, having been logged.
    """
    library: Dict[str, Dict[str, Any]] = {}
    for playbook_id in sorted(playbook_ids):
        try:
            doc = firestore_toolset.get_document(collection="Playbooks", document_id=playbook_id)
        except Exception as err:
            logger.exception("Error fetching playbook '%s': %s", playbook_id, err)
            continue

        if not doc or not doc.get("exists"):
            logger.error(
                "Playbook '%s' is referenced by the config but does not exist in "
                "Firestore collection 'Playbooks'.",
                playbook_id,
            )
            continue

        library[playbook_id] = doc.get("data", {}) or {}
        logger.info("Loaded playbook '%s'", playbook_id)

    return library


def _account_weather_values(
    ads_config: Dict[str, Any], customer_id: str
) -> Dict[str, Any]:
    """Extracts the account-level weather settings every playbook may reference.

    These used to live in a separate WeatherConditions collection, selected by
    ``weatherConditionsId``. Each account pointed at exactly one such document,
    so the indirection bought nothing, and the document mixed three unrelated
    things: the account's asset group naming (tokens), account policy (the
    windows) and spec rules (the activation tests). Tokens and windows now sit
    on the account document; the activation thresholds are set per city on
    ClimateBaselines/{city}.activation (see _city_values), with the playbook
    defaults as the fallback.

    Args:
        ads_config: The GoogleAdsConfig document data.
        customer_id: Used only for log context.

    Returns:
        A mapping of lookAheadHours, trailingWindowHours and assetGroupTokens,
        with every token upper-cased.
        When tokens are missing they are left absent, so the asset group
        playbook fails to render and is skipped with a log line naming the
        token, rather than matching asset groups on a guess.
    """
    if ads_config.get("weatherConditionsId"):
        # A config seeded before the collection was retired. Nothing breaks,
        # but an operator may be editing a document that is now ignored.
        logger.warning(
            "GoogleAdsConfig/%s still sets weatherConditionsId=%r. The "
            "WeatherConditions collection is no longer read; set "
            "lookAheadHours, trailingWindowHours and assetGroupTokens on the "
            "account document instead.",
            customer_id,
            ads_config.get("weatherConditionsId"),
            extra={"customer_id": str(customer_id)},
        )

    values: Dict[str, Any] = {
        "lookAheadHours": ads_config.get("lookAheadHours", 24),
        "trailingWindowHours": ads_config.get("trailingWindowHours", 24),
    }
    tokens = ads_config.get("assetGroupTokens")
    if isinstance(tokens, dict) and tokens:
        # Upper-cased here so the playbook only ever compares upper case with
        # upper case. Left to the model, a lower-case entry such as "_sun"
        # could silently fail to match and the asset group would never move.
        values["assetGroupTokens"] = {
            condition: str(token).strip().upper()
            for condition, token in tokens.items()
        }
    else:
        logger.warning(
            "GoogleAdsConfig/%s has no assetGroupTokens; the weather asset "
            "group playbook cannot match any asset group and will be skipped.",
            customer_id,
            extra={"customer_id": str(customer_id)},
        )
    return values


def _city_values(
    firestore_toolset: FirestoreToolset,
    city: Optional[str],
    cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Loads the per-city values (activation thresholds) for a campaign's city.

    The runner reads these, rather than leaving the model to look them up, so
    the thresholds in the prompt are exact numbers from Firestore. Any failure
    falls back to the playbook defaults: asset group toggling must keep running
    even if a city is unconfigured or the read fails.

    Args:
        firestore_toolset: Client used to read 'ClimateBaselines'.
        city: The campaign's params.city, or None.
        cache: Per-run cache keyed by city, so each city is read once.

    Returns:
        Values for playbooks.render_campaign_playbook's city_values layer.
    """
    if not city:
        return {}
    if city in cache:
        return cache[city]

    baseline: Optional[Dict[str, Any]] = None
    try:
        doc = firestore_toolset.get_document(collection="ClimateBaselines", document_id=str(city))
        if doc and doc.get("exists"):
            baseline = doc.get("data") or {}
        else:
            logger.warning(
                "No ClimateBaselines document for city %r; activation thresholds fall back "
                "to the playbook defaults.",
                city,
                extra={"city": city},
            )
    except Exception as err:  # pylint: disable=broad-except
        logger.exception("Error reading ClimateBaselines/%s: %s", city, err)

    values = playbooks_lib.city_values_from_baseline(baseline, city)
    logger.info(
        "City %s activation thresholds: %s (source: %s)",
        city,
        values.get("activation") or "(playbook defaults)",
        values.get("activationSource"),
        extra={"city": city, "activation_source": values.get("activationSource")},
    )
    cache[city] = values
    return values


def _maybe_sync_config_sheet(firestore_toolset: FirestoreToolset, customer_id: str) -> None:
    """Syncs the configuration sheet into Firestore before a run, if configured.

    Controlled by the CONFIG_SHEET_ID environment variable. A failed or invalid
    sync never stops the run: Firestore keeps the last good configuration and
    the config_sync event (needs_attention=true) raises an alert.

    Args:
        firestore_toolset: Supplies the Firestore client.
        customer_id: The account being run; only its rows are synced (cities
            are shared and always synced).
    """
    sheet_id = os.environ.get(config_sync.CONFIG_SHEET_ID_ENV, "").strip()
    if not sheet_id:
        return
    try:
        db = firestore_toolset._get_client()  # pylint: disable=protected-access
    except Exception as err:  # pylint: disable=broad-except
        logger.exception("Config sheet sync skipped: no Firestore client: %s", err)
        return
    config_sync.run_sheet_sync(sheet_id, db, customer_id=customer_id, source="scheduled_run")


def _fetch_campaign_name(customer_id: str, campaign_id: Any) -> Optional[str]:
    """Reads a campaign's live name from Google Ads with one GAQL query.

    Args:
        customer_id: Google Ads customer ID, digits only.
        campaign_id: Campaign ID.

    Returns:
        The campaign name, or None if the campaign does not exist.

    Raises:
        Exception: If the client is unavailable or the query fails.
    """
    # Imported lazily: the Google Ads SDK is heavy and unit tests patch this.
    from agentic_dsta.tools.google_ads.google_ads_client import get_google_ads_client  # pylint: disable=import-outside-toplevel

    client = get_google_ads_client(customer_id)
    if not client:
        raise RuntimeError("Failed to get Google Ads client.")
    query = f"SELECT campaign.name FROM campaign WHERE campaign.id = {int(campaign_id)}"
    service = client.get_service("GoogleAdsService")
    for batch in service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            return row.campaign.name
    return None


def _campaign_name_mismatch(
    customer_id: str, campaign: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Enforces params.campaignNameContains before any playbook runs.

    The playbooks also ask the model to check the name, but end-to-end testing
    showed the model can skip that check and change the wrong campaign's
    budget. This check runs in code, so a renamed campaign or a config row
    pointing at the wrong campaign ID is never acted on. It fails closed: if a
    name is configured but cannot be read, the campaign is skipped.

    The comparison is case-insensitive and ignores surrounding whitespace.

    Args:
        customer_id: Google Ads customer ID, digits only.
        campaign: The campaign config entry.

    Returns:
        None when the campaign may proceed (no name configured, or the name
        matches). Otherwise a dict with ``reason`` ('mismatch', 'not_found' or
        'lookup_failed'), ``expected``, ``actual`` and ``detail``.
    """
    expected = str(((campaign.get("params") or {}).get("campaignNameContains")) or "").strip()
    if not expected:
        return None
    campaign_id = campaign.get("campaignId")
    try:
        actual = _fetch_campaign_name(customer_id, campaign_id)
    except Exception as err:  # pylint: disable=broad-except
        return {
            "reason": "lookup_failed",
            "expected": expected,
            "actual": None,
            "detail": f"could not read the campaign name: {err}",
        }
    if actual is None:
        return {
            "reason": "not_found",
            "expected": expected,
            "actual": None,
            "detail": f"campaign {campaign_id} was not found in account {customer_id}",
        }
    if expected.casefold() not in actual.casefold():
        return {
            "reason": "mismatch",
            "expected": expected,
            "actual": actual,
            "detail": f"campaign name {actual!r} does not contain {expected!r}",
        }
    return None


def _report_campaign_skipped(
    firestore_toolset: FirestoreToolset,
    customer_id: str,
    campaign: Dict[str, Any],
    run_id: str,
    account: str,
    mismatch: Dict[str, Any],
) -> None:
    """Logs, alerts on and audits a campaign skipped by the name guard.

    Args:
        firestore_toolset: Used to write the ChangeLog row.
        customer_id: Google Ads customer ID.
        campaign: The skipped campaign's config entry.
        run_id: The current run id.
        account: Account label for the change log.
        mismatch: The result of _campaign_name_mismatch.
    """
    campaign_id = str(campaign.get("campaignId"))
    params = campaign.get("params") or {}
    logger.error(
        "SKIPPING campaign %s: %s. No playbook was run and nothing was changed. Fix the "
        "'Campaign name contains' column (or the campaign ID) in the config sheet.",
        campaign_id,
        mismatch["detail"],
        extra=telemetry.event_fields(
            telemetry.EVENT_CAMPAIGN_NAME_MISMATCH,
            customer_id=str(customer_id),
            run_id=run_id,
            campaign_id=campaign_id,
            reason=mismatch["reason"],
            expected_name_contains=mismatch["expected"],
            actual_name=mismatch["actual"],
        ),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        firestore_toolset.set_document(
            collection="ChangeLog",
            document_id=f"{now.strftime('%Y-%m-%d')}_{campaign_id}_skipped_{run_id}",
            data={
                "runId": run_id,
                "timestampUtc": now.isoformat(),
                "account": account,
                "customerId": str(customer_id),
                "campaignId": campaign_id,
                "campaignName": mismatch["actual"],
                "weatherLocation": params.get("city"),
                "condition": "None",
                "assetGroupAction": "no change",
                "budgetBeforeMicros": "unchanged",
                "budgetAfterMicros": "unchanged",
                "mode": "skipped",
                "notes": f"campaign skipped by name guard ({mismatch['reason']}): {mismatch['detail']}",
            },
            merge=False,
        )
    except Exception as err:  # pylint: disable=broad-except
        logger.exception("Unable to write the skipped-campaign ChangeLog row for %s: %s", campaign_id, err)


def _micros(value: Any) -> Optional[int]:
    """Parses a ChangeLog budget field into micros, or None if not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _is_budget_increase(row: Dict[str, Any]) -> bool:
    """True if a ChangeLog row records a budget increase.

    A row whose after-value is numeric but whose before-value is missing or
    unparseable is counted as an increase, so a malformed row errs towards
    alerting rather than hiding a change.

    Args:
        row: ChangeLog document data.

    Returns:
        Whether the row raised the budget.
    """
    after = _micros(row.get("budgetAfterMicros"))
    if after is None:
        return False
    before = _micros(row.get("budgetBeforeMicros"))
    return before is None or after > before


def _check_change_volume_guard(
    firestore_toolset: FirestoreToolset,
    run_id: str,
    guard_config: Dict[str, Any],
    eligible_campaigns: int,
    customer_id: Optional[str] = None,
) -> None:
    """Reports whether a run exceeded the configured change volume cap.

    Section 5.5 of the Arc'teryx spec asks for a cap on how many campaigns may
    have their budget changed in a single run, as a safety net against a bad
    data feed. This is a cross-campaign constraint, and campaigns are processed
    in deliberate isolation so that no campaign's context can pollute another's.
    That isolation means no agent can know how many of its peers already acted.

    This implementation therefore detects and alerts after the fact rather than
    blocking mid-run. Enforcing the cap properly requires a two-phase run in
    which all campaigns propose decisions and the runner applies a capped
    subset; that is tracked as follow-up work.

    Only budget INCREASES count towards the cap. A bad feed shows up as spend
    being added; returning budgets to normal when a widespread storm ends is
    expected and would otherwise raise a CRITICAL alert for no reason.

    Args:
        firestore_toolset: Client used to read the 'ChangeLog' collection.
        run_id: The identifier shared by every decision in this run.
        guard_config: The account's 'changeVolumeGuard' block.
        eligible_campaigns: Number of campaigns considered in this run.
        customer_id: The account the run processed, added to the alert event.
    """
    if not guard_config.get("enabled"):
        return

    fraction = guard_config.get("maxFractionOfEligibleCampaigns", 0.25)
    try:
        cap = max(1, math.ceil(float(fraction) * eligible_campaigns))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid maxFractionOfEligibleCampaigns %r; skipping the change volume check.",
            fraction,
        )
        return

    try:
        result = firestore_toolset.query_collection(
            collection="ChangeLog", field="runId", operator="==", value=run_id, limit=500
        )
    except Exception as err:
        logger.exception("Unable to read ChangeLog for run %s: %s", run_id, err)
        return

    changed = [
        doc
        for doc in result.get("documents", [])
        if _is_budget_increase(doc.get("data") or {})
    ]

    logger.info(
        "Change volume for run %s: %d budget increase(s) across %d eligible campaign(s), cap %d",
        run_id,
        len(changed),
        eligible_campaigns,
        cap,
        extra={"run_id": run_id, "budget_changes": len(changed), "cap": cap},
    )

    if len(changed) > cap:
        logger.error(
            "CHANGE VOLUME GUARD EXCEEDED for run %s: %d budget increases against a cap "
            "of %d (%.0f%% of %d eligible campaigns). This may indicate a bad weather "
            "data feed. Review ChangeLog rows for runId=%s.",
            run_id,
            len(changed),
            cap,
            float(fraction) * 100,
            eligible_campaigns,
            run_id,
            extra=telemetry.event_fields(
                telemetry.EVENT_CHANGE_GUARD_EXCEEDED,
                run_id=run_id,
                customer_id=customer_id,
                budget_changes=len(changed),
                cap=cap,
                eligible_campaigns=eligible_campaigns,
            ),
        )


@dataclasses.dataclass
class _CampaignJob:
    """A campaign prepared for execution.

    Attributes:
        index: 1-based position in the account's campaign list (for logs).
        total: Number of campaigns in the account's config.
        campaign: The campaign config entry.
        instructions: Rendered (playbook_id, instruction) pairs, in run order.
    """

    index: int
    total: int
    campaign: Dict[str, Any]
    instructions: List[Tuple[str, str]]


@dataclasses.dataclass
class _CampaignResult:
    """What happened to one campaign.

    Attributes:
        campaign_id: The campaign ID.
        eligible: True if its playbooks were run (it passed the name guard).
        skipped: True if the name guard skipped it.
        succeeded: Playbook executions that completed.
        failed: Playbook executions that raised or timed out.
    """

    campaign_id: str
    eligible: bool = False
    skipped: bool = False
    succeeded: int = 0
    failed: int = 0


def _int_setting(env_name: str, default: int, minimum: int, maximum: int) -> int:
    """Reads a bounded integer setting from the environment.

    Args:
        env_name: Environment variable name.
        default: Value used when the variable is unset or invalid.
        minimum: Smallest allowed value.
        maximum: Largest allowed value.

    Returns:
        The setting, clamped to [minimum, maximum].
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d.", env_name, raw, default)
        return default
    clamped = max(minimum, min(value, maximum))
    if clamped != value:
        logger.warning("%s=%d is outside [%d, %d]; using %d.", env_name, value, minimum, maximum, clamped)
    return clamped


def _max_concurrent_campaigns() -> int:
    """How many campaigns run at once (ADSTA_MAX_CONCURRENT_CAMPAIGNS)."""
    return _int_setting(
        MAX_CONCURRENT_CAMPAIGNS_ENV, DEFAULT_MAX_CONCURRENT_CAMPAIGNS, 1, MAX_CONCURRENT_CAMPAIGNS_LIMIT
    )


def _playbook_timeout_seconds() -> int:
    """Per-playbook time limit in seconds (ADSTA_PLAYBOOK_TIMEOUT_SECONDS)."""
    return _int_setting(PLAYBOOK_TIMEOUT_ENV, DEFAULT_PLAYBOOK_TIMEOUT_SECONDS, 60, 1800)


async def _run_playbook(
    customer_id: str,
    usecase: str,
    run_id: str,
    campaign_id: Any,
    playbook_id: str,
    instruction: str,
) -> None:
    """Runs one playbook for one campaign in a fresh agent and session.

    Args:
        customer_id: Google Ads customer ID.
        usecase: Target platform, for event labels.
        run_id: The current run id.
        campaign_id: The campaign being processed.
        playbook_id: The playbook being run.
        instruction: The rendered playbook instruction.
    """
    agent = create_agent(
        instruction=instruction,
        run_context={
            "customer_id": customer_id,
            "usecase": usecase,
            "run_id": run_id,
            "campaign_id": campaign_id,
            "playbook_id": playbook_id,
        },
    )
    app = apps.App(name="decision_app", root_agent=agent)
    runner = runners.InMemoryRunner(app=app)

    session_id = str(uuid.uuid4())
    await runner.session_service.create_session(
        session_id=session_id, user_id=customer_id, app_name="decision_app"
    )
    prompt_text = (
        f"Proceed with playbook '{playbook_id}' for Campaign {campaign_id} "
        "based on your instructions."
    )
    content = types.Content(parts=[types.Part(text=prompt_text)])

    logger.info(
        "Executing playbook '%s' for Campaign %s (session_id=%s, run_id=%s)",
        playbook_id,
        campaign_id,
        session_id,
        run_id,
        extra={"campaign_id": str(campaign_id), "playbook_id": playbook_id, "run_id": run_id},
    )
    async for chunk in runner.run_async(user_id=customer_id, session_id=session_id, new_message=content):
        _log_agent_chunk(chunk, str(campaign_id))


async def _run_playbook_with_timeout(timeout_s: int, **kwargs: Any) -> None:
    """Runs _run_playbook, raising a descriptive TimeoutError after timeout_s.

    The timeout exists so that one hung model or API call cannot hold the
    whole run past the scheduler deadline, which would lose every other
    campaign's run_completed accounting. Cancellation takes effect at the next
    await, so a tool call already in progress finishes first.
    """
    try:
        await asyncio.wait_for(_run_playbook(**kwargs), timeout=timeout_s)
    except asyncio.TimeoutError as err:
        raise TimeoutError(f"playbook timed out after {timeout_s}s") from err


def _process_campaign(
    job: _CampaignJob,
    *,
    customer_id: str,
    usecase: str,
    run_id: str,
    account: str,
    playbook_timeout_s: int,
) -> _CampaignResult:
    """Processes one campaign: name guard, then each playbook in order.

    Runs in a worker thread. Every playbook gets its own event loop
    (asyncio.run), agent and session, so nothing is shared with other
    campaigns running at the same time.

    Args:
        job: The prepared campaign.
        customer_id: Google Ads customer ID.
        usecase: Target platform, for event labels.
        run_id: The current run id.
        account: Account label for the change log.
        playbook_timeout_s: Per-playbook time limit.

    Returns:
        The campaign's result.
    """
    campaign_id = job.campaign.get("campaignId")
    result = _CampaignResult(campaign_id=str(campaign_id))
    logger.info(
        "--- Processing Campaign %d/%d: ID=%s ---",
        job.index,
        job.total,
        campaign_id,
        extra={"campaign_id": str(campaign_id), "campaign_index": job.index, "total_campaigns": job.total},
    )

    # Deterministic safety check, enforced in code rather than left to the
    # model: never act on a campaign whose name does not match the config.
    mismatch = _campaign_name_mismatch(customer_id, job.campaign)
    if mismatch:
        _report_campaign_skipped(FirestoreToolset(), customer_id, job.campaign, run_id, account, mismatch)
        result.skipped = True
        return result

    result.eligible = True
    campaign_start_time = time.perf_counter()

    # Isolation is per (campaign, playbook) rather than per campaign so that a
    # campaign missing its Severe Modifiers params still gets its asset groups
    # toggled, per section 7 of the spec. Playbooks run in order: the budget
    # playbook must see the asset group state the first one left behind.
    for playbook_id, instruction in job.instructions:
        try:
            asyncio.run(
                _run_playbook_with_timeout(
                    playbook_timeout_s,
                    customer_id=customer_id,
                    usecase=usecase,
                    run_id=run_id,
                    campaign_id=campaign_id,
                    playbook_id=playbook_id,
                    instruction=instruction,
                )
            )
            elapsed = time.perf_counter() - campaign_start_time
            logger.info(
                "Playbook '%s' completed for Campaign %s in %.2fs",
                playbook_id,
                campaign_id,
                elapsed,
                extra={"campaign_id": str(campaign_id), "playbook_id": playbook_id, "duration_s": elapsed},
            )
            result.succeeded += 1
        except Exception as e:  # pylint: disable=broad-except
            elapsed = time.perf_counter() - campaign_start_time
            logger.exception(
                "Failed playbook '%s' for Campaign %s after %.2fs: %s",
                playbook_id,
                campaign_id,
                elapsed,
                e,
                extra=telemetry.event_fields(
                    telemetry.EVENT_PLAYBOOK_FAILED,
                    customer_id=str(customer_id),
                    usecase=usecase,
                    run_id=run_id,
                    campaign_id=str(campaign_id),
                    playbook_id=playbook_id,
                    error_class=telemetry.classify_error(e),
                    duration_s=elapsed,
                ),
            )
            result.failed += 1
    return result


async def _run_campaign_jobs(
    jobs: List[_CampaignJob],
    max_concurrency: int,
    worker: Callable[[_CampaignJob], _CampaignResult],
    *,
    customer_id: str,
    usecase: str,
    run_id: str,
) -> List[_CampaignResult]:
    """Runs campaign jobs on a bounded thread pool.

    A dedicated pool (rather than asyncio.to_thread) is used so the limit is
    exactly max_concurrency: the default executor is sized from the CPU count.
    The tools are synchronous (Google Ads, Firestore, weather HTTP), so a
    thread per campaign is what lets their I/O overlap; ADK alone would run
    them on one event loop, one at a time.

    Args:
        jobs: Prepared campaigns.
        max_concurrency: Maximum campaigns in flight.
        worker: Processes one job; normally a partial of _process_campaign.
        customer_id: For failure events.
        usecase: For failure events.
        run_id: For failure events.

    Returns:
        One result per job, in job order. A worker that raised unexpectedly is
        reported as all of that campaign's playbooks failing.
    """
    if not jobs:
        return []
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_concurrency, thread_name_prefix="adsta_campaign"
    ) as pool:
        outcomes = await asyncio.gather(
            *(loop.run_in_executor(pool, worker, job) for job in jobs), return_exceptions=True
        )

    results: List[_CampaignResult] = []
    for job, outcome in zip(jobs, outcomes):
        if isinstance(outcome, _CampaignResult):
            results.append(outcome)
            continue
        campaign_id = str(job.campaign.get("campaignId"))
        logger.error(
            "Campaign %s worker failed unexpectedly: %s",
            campaign_id,
            outcome,
            exc_info=outcome if isinstance(outcome, BaseException) else None,
            extra=telemetry.event_fields(
                telemetry.EVENT_PLAYBOOK_FAILED,
                customer_id=str(customer_id),
                usecase=usecase,
                run_id=run_id,
                campaign_id=campaign_id,
                playbook_id="*",
                error_class=telemetry.classify_error(outcome),
            ),
        )
        results.append(
            _CampaignResult(campaign_id=campaign_id, eligible=True, failed=len(job.instructions))
        )
    return results


def _log_run_completed(summary: telemetry.RunSummary) -> None:
    """Emits the single run_completed event for a run.

    Every run ends with exactly one of these, whatever the outcome. It is the
    heartbeat the missed-run alert watches and the source of the run outcome
    and duration metrics, so it must be emitted even when the run raised.

    Args:
        summary: The finished run's summary.
    """
    level = {
        telemetry.OUTCOME_FAILED: logging.ERROR,
        telemetry.OUTCOME_ABORTED: logging.ERROR,
        telemetry.OUTCOME_PARTIAL: logging.WARNING,
    }.get(summary.outcome, logging.INFO)
    logger.log(
        level,
        "=== Completed Decision Agent Run %s for Customer %s in %.2fs "
        "(outcome: %s%s, success: %d, failed: %d) ===",
        summary.run_id or "(none)",
        summary.customer_id,
        summary.duration_s,
        summary.outcome,
        f", reason: {summary.reason}" if summary.reason else "",
        summary.successful_playbooks,
        summary.failed_playbooks,
        extra=telemetry.event_fields(
            telemetry.EVENT_RUN_COMPLETED,
            customer_id=summary.customer_id,
            usecase=summary.usecase,
            run_id=summary.run_id or None,
            outcome=summary.outcome,
            reason=summary.reason or None,
            total_duration_s=summary.duration_s,
            eligible_campaigns=summary.eligible_campaigns,
            skipped_campaigns=summary.skipped_campaigns,
            # Legacy field names kept so existing saved log queries still work.
            successful_campaigns=summary.successful_playbooks,
            failed_campaigns=summary.failed_playbooks,
        ),
    )


async def run_decision_agent(
    customer_id: str, usecase: Optional[str] = "GoogleAds"
) -> telemetry.RunSummary:
    """
    Main entry point for the Decision Agent.
    Controller Logic:
    1. Fetches Customer Intent/Instructions from Firestore.
    2. Fetches Google Ads Config or SA360Config from Firestore.
    3. Loops through campaigns, creating an isolated agent for each.

    Args:
        customer_id: The customer ID to process. Accepted either bare
            ("5341114500") or hyphenated ("534-111-4500"); it is normalised to
            the bare form before use.
        usecase: Target platform ('GoogleAds' or 'SA360').

    Returns:
        A RunSummary describing the outcome. The caller uses it to report
        failures honestly instead of always answering "success".

    Raises:
        Exception: Anything unexpected from the run is re-raised after the
            run_completed event (outcome 'failed') has been emitted.
    """
    total_start_time = time.perf_counter()

    # The Google Ads UI displays customer IDs hyphenated, so scheduler payloads
    # and hand-written configs frequently carry that form. Firestore document
    # IDs and the Google Ads API both require the bare digits, and a mismatch
    # here fails silently: the instruction lookup misses, the run aborts
    # cleanly, and the caller still sees success.
    raw_customer_id = customer_id
    if customer_id:
        customer_id = str(customer_id).replace("-", "").strip()
    if customer_id != raw_customer_id:
        logger.info(
            "Normalised customer_id %r to %r",
            raw_customer_id,
            customer_id,
            extra={"customer_id": str(customer_id)},
        )

    summary = telemetry.RunSummary(customer_id=str(customer_id), usecase=usecase or "GoogleAds")
    logger.info(
        "=== Starting Decision Agent Run: customer_id=%s, usecase=%s ===",
        customer_id,
        summary.usecase,
        extra=telemetry.event_fields(
            telemetry.EVENT_RUN_STARTED,
            customer_id=summary.customer_id,
            usecase=summary.usecase,
        ),
    )

    try:
        await _execute_run(customer_id, usecase, summary)
    except Exception:
        summary.outcome = telemetry.OUTCOME_FAILED
        summary.reason = "unhandled_exception"
        raise
    finally:
        summary.duration_s = time.perf_counter() - total_start_time
        _log_run_completed(summary)
    return summary


async def _execute_run(
    customer_id: str, usecase: Optional[str], summary: telemetry.RunSummary
) -> None:
    """Runs the decision agent for one account, filling in ``summary``.

    Args:
        customer_id: The normalised customer ID.
        usecase: Target platform ('GoogleAds' or 'SA360'), or None.
        summary: Updated in place with the run's id, counters and outcome.
    """

    firestore_toolset = FirestoreToolset()

    # 0. Pull operator edits from the configuration sheet, if one is configured.
    if (usecase or "GoogleAds") == "GoogleAds":
        _maybe_sync_config_sheet(firestore_toolset, customer_id)

    # 1. Fetch Global Instructions
    logger.info(
        "Fetching global instructions from Firestore: collection=CustomerInstructions, doc_id=%s",
        customer_id,
    )
    try:
        doc = firestore_toolset.get_document(collection="CustomerInstructions", document_id=customer_id)
        if not doc:
            logger.warning(
                "No document found in Firestore collection 'CustomerInstructions' for customer_id: %s",
                customer_id,
            )
            global_instruction = ""
        else:
            global_instruction = doc.get("data", {}).get("instruction", "")
            logger.info(
                "Loaded CustomerInstructions for customer_id=%s (%d characters)",
                customer_id,
                len(global_instruction),
            )
    except Exception as e:
        logger.exception("Error fetching CustomerInstructions for customer_id=%s: %s", customer_id, e)
        global_instruction = ""

    if not global_instruction:
        logger.warning(
            "No global instructions found for customer_id=%s in Firestore 'CustomerInstructions'. Aborting run.",
            customer_id,
        )
        summary.outcome = telemetry.OUTCOME_ABORTED
        summary.reason = telemetry.ABORT_MISSING_INSTRUCTIONS
        return

    # 2. Fetch Campaign Config
    collection = (usecase or "GoogleAds") + "Config"
    logger.info(
        "Fetching campaign configuration from Firestore: collection=%s, doc_id=%s",
        collection,
        customer_id,
    )
    config_found = False
    try:
        doc = firestore_toolset.get_document(collection=collection, document_id=customer_id)
        # get_document returns a truthy {"exists": False, ...} dict for a
        # missing document or a read error, so check the flag explicitly.
        if not doc or doc.get("exists") is False:
            logger.warning("No document found in Firestore collection '%s' for customer_id: %s", collection, customer_id)
            ads_config = {}
        else:
            ads_config = doc.get("data", {}) or {}
            config_found = True
    except Exception as e:
        logger.exception("Error fetching %s for customer_id=%s: %s", collection, customer_id, e)
        ads_config = {}

    campaigns = ads_config.get("campaigns", [])

    if not config_found:
        summary.outcome = telemetry.OUTCOME_ABORTED
        summary.reason = telemetry.ABORT_MISSING_CONFIG
        return

    if not campaigns:
        logger.info(
            "No campaigns configured in %s for customer_id=%s. Completed with no actions.",
            collection,
            customer_id,
        )
        summary.outcome = telemetry.OUTCOME_NOOP
        summary.reason = telemetry.ABORT_NO_CAMPAIGNS
        return

    logger.info(
        "Discovered %d campaign(s) in %s for customer_id=%s: %s",
        len(campaigns),
        collection,
        customer_id,
        [c.get("campaignId") for c in campaigns],
    )

    # 3. Load shared, city-agnostic configuration.
    #
    # Playbooks are shared across every campaign, so they are read once per
    # run. This is what allows a new city to be four lines of config rather
    # than a duplicated copy of the decision logic.
    referenced_playbooks: Set[str] = set()
    for campaign in campaigns:
        referenced_playbooks.update(playbooks_lib.resolve_playbook_ids(campaign))

    playbook_library = (
        _load_playbook_library(firestore_toolset, referenced_playbooks)
        if referenced_playbooks
        else {}
    )

    account = ads_config.get("account", "")
    schedule = ads_config.get("schedule", {}) or {}
    account_timezone = schedule.get("timezone")
    run_id = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_{uuid.uuid4().hex[:8]}"
    summary.run_id = run_id

    # Values every playbook may reference, regardless of city.
    shared_values: Dict[str, Any] = {
        "globalInstruction": global_instruction,
        **_account_weather_values(ads_config, customer_id),
        "runsPerDay": schedule.get("runsPerDay", 2),
    }

    # The trailing window is expressed in hours but enforced in runs, so it has
    # to scale with the schedule. At two runs per day a 24-hour window means the
    # two preceding runs, exactly as the spec's worked example describes.
    runs_per_day = shared_values["runsPerDay"] or 2
    try:
        shared_values["trailingWindowRuns"] = max(
            1, round(float(shared_values["trailingWindowHours"]) * float(runs_per_day) / 24.0)
        )
    except (TypeError, ValueError, ZeroDivisionError):
        shared_values["trailingWindowRuns"] = 2

    logger.info(
        "Run %s: account=%s, timezone=%s, playbooks=%s, trailing window=%s run(s)",
        run_id,
        account or "(unset)",
        account_timezone or "UTC",
        sorted(playbook_library) or "(none)",
        shared_values["trailingWindowRuns"],
        extra={"run_id": run_id, "customer_id": str(customer_id)},
    )

    # Per-city values (activation thresholds), read once per city per run.
    city_cache: Dict[str, Dict[str, Any]] = {}

    # 4. Prepare every campaign: Firestore reads and prompt rendering. This is
    #    cheap and shares the per-run city cache, so it stays sequential.
    jobs: List[_CampaignJob] = []
    for idx, campaign in enumerate(campaigns, start=1):
        campaign_id = campaign.get("campaignId")

        if not campaign_id:
            logger.warning("Skipping campaign %d/%d: missing 'campaignId' field.", idx, len(campaigns))
            continue

        hemisphere = ((campaign.get("params") or {}).get("hemisphere")) or "northern"

        def _context_for(playbook_id: str, _campaign_id=campaign_id, _hemisphere=hemisphere) -> Dict[str, Any]:
            """Builds the runner-owned context for one playbook of this campaign."""
            return playbooks_lib.build_context(
                customer_id=customer_id,
                campaign_id=_campaign_id,
                playbook_id=playbook_id,
                run_id=run_id,
                account=account,
                timezone_name=account_timezone,
                hemisphere=_hemisphere,
            )

        resolved = playbooks_lib.resolve_campaign_instructions(
            campaign=campaign,
            playbook_library=playbook_library,
            context_builder=_context_for,
            shared_values=shared_values,
            city_values=_city_values(
                firestore_toolset, (campaign.get("params") or {}).get("city"), city_cache
            ),
        )

        if not resolved:
            logger.warning(
                "Campaign %s resolved to no runnable playbooks. Skipping.",
                campaign_id,
                extra={"campaign_id": str(campaign_id)},
            )
            continue

        jobs.append(
            _CampaignJob(index=idx, total=len(campaigns), campaign=campaign, instructions=resolved)
        )

    # 5. Execute the campaigns in parallel. Each campaign runs in its own
    #    worker thread with its own event loop and its own agent per playbook,
    #    so campaigns stay isolated; a campaign's playbooks still run in order.
    max_concurrency = _max_concurrent_campaigns()
    playbook_timeout_s = _playbook_timeout_seconds()
    logger.info(
        "Run %s: executing %d campaign(s), up to %d in parallel (playbook timeout %ds)",
        run_id,
        len(jobs),
        max_concurrency,
        playbook_timeout_s,
        extra={
            "run_id": run_id,
            "customer_id": str(customer_id),
            "campaigns": len(jobs),
            "max_concurrency": max_concurrency,
        },
    )
    results = await _run_campaign_jobs(
        jobs,
        max_concurrency,
        functools.partial(
            _process_campaign,
            customer_id=customer_id,
            usecase=summary.usecase,
            run_id=run_id,
            account=account,
            playbook_timeout_s=playbook_timeout_s,
        ),
        customer_id=customer_id,
        usecase=summary.usecase,
        run_id=run_id,
    )

    successful_campaigns = sum(r.succeeded for r in results)
    failed_campaigns = sum(r.failed for r in results)
    eligible_campaigns = sum(1 for r in results if r.eligible)
    skipped_campaigns = sum(1 for r in results if r.skipped)

    # 6. Cross-campaign safety net (spec section 5.5).
    _check_change_volume_guard(
        firestore_toolset=firestore_toolset,
        run_id=run_id,
        guard_config=ads_config.get("changeVolumeGuard", {}) or {},
        eligible_campaigns=eligible_campaigns,
        customer_id=customer_id,
    )

    # The run_completed event itself is emitted by run_decision_agent.
    summary.successful_playbooks = successful_campaigns
    summary.failed_playbooks = failed_campaigns
    summary.eligible_campaigns = eligible_campaigns
    summary.skipped_campaigns = skipped_campaigns
    if failed_campaigns and successful_campaigns:
        summary.outcome = telemetry.OUTCOME_PARTIAL
    elif failed_campaigns:
        summary.outcome = telemetry.OUTCOME_FAILED
        summary.reason = "all_playbooks_failed"
    elif skipped_campaigns:
        # Some campaigns were deliberately not acted on (name guard). The run
        # did what it safely could, but an operator must fix the config.
        summary.outcome = telemetry.OUTCOME_PARTIAL
        summary.reason = telemetry.REASON_CAMPAIGNS_SKIPPED
    elif successful_campaigns:
        summary.outcome = telemetry.OUTCOME_SUCCESS
    else:
        # Campaigns exist but none resolved to a runnable playbook, which is
        # almost always a config problem (e.g. missing assetGroupTokens).
        summary.outcome = telemetry.OUTCOME_NOOP
        summary.reason = "no_runnable_playbooks"


root_agent = create_agent(instruction="You are a decision agent helper.")
