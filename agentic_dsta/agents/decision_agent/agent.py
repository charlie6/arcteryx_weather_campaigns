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
import datetime
import logging
import math
import os
import time
from typing import Any, Dict, List, Optional, Set
import uuid

from google.genai import Client
from google.genai import types
from google.adk.models.google_llm import Gemini
from google.adk import apps
from google.adk import runners
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from agentic_dsta.agents.decision_agent import playbooks as playbooks_lib
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


def create_agent(instruction: str, model: str = DEFAULT_MODEL) -> agents.LlmAgent:
    """
    Creates a new instance of the decision agent with specific instructions.

    Args:
        instruction: The system instruction for this agent instance.
        model: The Gemini model to use.

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


def _load_weather_conditions(
    firestore_toolset: FirestoreToolset, document_id: str = "default"
) -> Dict[str, Any]:
    """Loads the shared weather condition definitions.

    These are the activation rules (section 3.2 of the Arc'teryx spec) that
    apply identically to every city, as distinct from the per-campaign severity
    thresholds. Keeping them in one document means a threshold change is a
    single edit rather than one per campaign.

    Args:
        firestore_toolset: Client used to read the 'WeatherConditions' collection.
        document_id: The condition set to load.

    Returns:
        The condition set data, or an empty dict when absent.
    """
    try:
        doc = firestore_toolset.get_document(
            collection="WeatherConditions", document_id=document_id
        )
    except Exception as err:
        logger.exception("Error fetching WeatherConditions/%s: %s", document_id, err)
        return {}

    if not doc or not doc.get("exists"):
        logger.warning(
            "No WeatherConditions/%s document found. Playbooks that reference "
            "shared condition definitions will be skipped.",
            document_id,
        )
        return {}

    return doc.get("data", {}) or {}


def _render_conditions_table(weather_conditions: Dict[str, Any]) -> str:
    """Renders the condition definitions as prompt text.

    Args:
        weather_conditions: The WeatherConditions document data.

    Returns:
        A newline-separated list of condition rules, or a placeholder note when
        no conditions are configured.
    """
    conditions = weather_conditions.get("conditions") or []
    if not conditions:
        return "(no conditions configured)"

    lines = []
    for condition in conditions:
        name = condition.get("name", "?")
        token = condition.get("assetGroupToken", "?")
        test = condition.get("test", "?")
        # Deliberately no severity column. This table is only ever rendered into
        # the asset group playbook, which does not decide budgets, and severity
        # is now a property of the city's ClimateBaselines.severeThresholds
        # rather than of a condition.
        lines.append(
            f"  - {name}: asset group name contains '{token}'; active when {test}"
        )
    return "\n".join(lines)


def _check_change_volume_guard(
    firestore_toolset: FirestoreToolset,
    run_id: str,
    guard_config: Dict[str, Any],
    eligible_campaigns: int,
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

    Args:
        firestore_toolset: Client used to read the 'ChangeLog' collection.
        run_id: The identifier shared by every decision in this run.
        guard_config: The account's 'changeVolumeGuard' block.
        eligible_campaigns: Number of campaigns considered in this run.
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
        if (doc.get("data") or {}).get("budgetAfterMicros") not in (None, "unchanged")
    ]

    logger.info(
        "Change volume for run %s: %d budget change(s) across %d eligible campaign(s), cap %d",
        run_id,
        len(changed),
        eligible_campaigns,
        cap,
        extra={"run_id": run_id, "budget_changes": len(changed), "cap": cap},
    )

    if len(changed) > cap:
        logger.error(
            "CHANGE VOLUME GUARD EXCEEDED for run %s: %d budget changes against a cap "
            "of %d (%.0f%% of %d eligible campaigns). This may indicate a bad weather "
            "data feed. Review ChangeLog rows for runId=%s.",
            run_id,
            len(changed),
            cap,
            float(fraction) * 100,
            eligible_campaigns,
            run_id,
            extra={"run_id": run_id, "budget_changes": len(changed), "cap": cap},
        )


async def run_decision_agent(customer_id: str, usecase: Optional[str] = "GoogleAds") -> None:
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

    logger.info(
        "=== Starting Decision Agent Run: customer_id=%s, usecase=%s ===",
        customer_id,
        usecase or "GoogleAds",
        extra={"customer_id": str(customer_id), "usecase": str(usecase)},
    )

    # 1. Fetch Global Instructions
    firestore_toolset = FirestoreToolset()
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
        return

    # 2. Fetch Campaign Config
    collection = (usecase or "GoogleAds") + "Config"
    logger.info(
        "Fetching campaign configuration from Firestore: collection=%s, doc_id=%s",
        collection,
        customer_id,
    )
    try:
        doc = firestore_toolset.get_document(collection=collection, document_id=customer_id)
        if not doc:
            logger.warning("No document found in Firestore collection '%s' for customer_id: %s", collection, customer_id)
            ads_config = {}
        else:
            ads_config = doc.get("data", {})
    except Exception as e:
        logger.exception("Error fetching %s for customer_id=%s: %s", collection, customer_id, e)
        ads_config = {}

    campaigns = ads_config.get("campaigns", [])

    if not campaigns:
        logger.info(
            "No campaigns configured in %s for customer_id=%s. Completed with no actions.",
            collection,
            customer_id,
        )
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
    # Playbooks and condition definitions are shared across every campaign, so
    # they are read once per run. This is what allows a new city to be four
    # lines of config rather than a duplicated copy of the decision logic.
    referenced_playbooks: Set[str] = set()
    for campaign in campaigns:
        referenced_playbooks.update(playbooks_lib.resolve_playbook_ids(campaign))

    playbook_library = (
        _load_playbook_library(firestore_toolset, referenced_playbooks)
        if referenced_playbooks
        else {}
    )
    weather_conditions = _load_weather_conditions(
        firestore_toolset, ads_config.get("weatherConditionsId", "default")
    )

    account = ads_config.get("account", "")
    schedule = ads_config.get("schedule", {}) or {}
    account_timezone = schedule.get("timezone")
    run_id = f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_{uuid.uuid4().hex[:8]}"

    # Values every playbook may reference, regardless of city.
    shared_values: Dict[str, Any] = {
        "globalInstruction": global_instruction,
        "conditionsTable": _render_conditions_table(weather_conditions),
        "lookAheadHours": weather_conditions.get("lookAheadHours", 24),
        "trailingWindowHours": weather_conditions.get("trailingWindowHours", 24),
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

    # 4. Loop and Process Each Campaign
    successful_campaigns = 0
    failed_campaigns = 0
    eligible_campaigns = 0

    for idx, campaign in enumerate(campaigns, start=1):
        campaign_id = campaign.get("campaignId")

        if not campaign_id:
            logger.warning("Skipping campaign %d/%d: missing 'campaignId' field.", idx, len(campaigns))
            continue

        campaign_start_time = time.perf_counter()
        logger.info(
            "--- Processing Campaign %d/%d: ID=%s ---",
            idx,
            len(campaigns),
            campaign_id,
            extra={"campaign_id": str(campaign_id), "campaign_index": idx, "total_campaigns": len(campaigns)},
        )

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
        )

        if not resolved:
            logger.warning(
                "Campaign %s resolved to no runnable playbooks. Skipping.",
                campaign_id,
                extra={"campaign_id": str(campaign_id)},
            )
            continue

        eligible_campaigns += 1

        # Each playbook gets its own agent and session. Isolation is per
        # (campaign, playbook) rather than per campaign so that a campaign
        # missing its Severe Modifiers params still gets its asset groups
        # toggled, per section 7 of the spec.
        for playbook_id, instruction in resolved:
            try:
                agent = create_agent(instruction=instruction)

                app = apps.App(name="decision_app", root_agent=agent)
                runner = runners.InMemoryRunner(app=app)

                session_id = str(uuid.uuid4())
                await runner.session_service.create_session(
                    session_id=session_id,
                    user_id=customer_id,
                    app_name="decision_app"
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
                    extra={
                        "campaign_id": str(campaign_id),
                        "playbook_id": playbook_id,
                        "run_id": run_id,
                    },
                )
                async for chunk in runner.run_async(
                    user_id=customer_id,
                    session_id=session_id,
                    new_message=content
                ):
                    _log_agent_chunk(chunk, str(campaign_id))

                campaign_elapsed = time.perf_counter() - campaign_start_time
                logger.info(
                    "Playbook '%s' completed for Campaign %s in %.2fs",
                    playbook_id,
                    campaign_id,
                    campaign_elapsed,
                    extra={
                        "campaign_id": str(campaign_id),
                        "playbook_id": playbook_id,
                        "duration_s": campaign_elapsed,
                    },
                )
                successful_campaigns += 1

            except Exception as e:
                campaign_elapsed = time.perf_counter() - campaign_start_time
                logger.exception(
                    "Failed playbook '%s' for Campaign %s after %.2fs: %s",
                    playbook_id,
                    campaign_id,
                    campaign_elapsed,
                    e,
                    extra={
                        "campaign_id": str(campaign_id),
                        "playbook_id": playbook_id,
                        "duration_s": campaign_elapsed,
                    },
                )
                failed_campaigns += 1
                continue

    # 5. Cross-campaign safety net (spec section 5.5).
    _check_change_volume_guard(
        firestore_toolset=firestore_toolset,
        run_id=run_id,
        guard_config=ads_config.get("changeVolumeGuard", {}) or {},
        eligible_campaigns=eligible_campaigns,
    )

    total_elapsed = time.perf_counter() - total_start_time
    logger.info(
        "=== Completed Decision Agent Run %s for Customer %s in %.2fs (success: %d, failed: %d) ===",
        run_id,
        customer_id,
        total_elapsed,
        successful_campaigns,
        failed_campaigns,
        extra={
            "customer_id": str(customer_id),
            "run_id": run_id,
            "total_duration_s": total_elapsed,
            "successful_campaigns": successful_campaigns,
            "failed_campaigns": failed_campaigns,
        },
    )


root_agent = create_agent(instruction="You are a decision agent helper.")
