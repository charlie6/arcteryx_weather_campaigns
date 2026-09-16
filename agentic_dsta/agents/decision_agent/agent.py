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
import os
import time
from typing import Any, Dict, List, Optional
import uuid

from google.genai import Client
from google.genai import types
from google.adk.models.google_llm import Gemini
from google.adk import apps
from google.adk import runners
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from agentic_dsta.tools.api_hub.apihub_toolset import DynamicMultiAPIToolset
from agentic_dsta.tools.firestore.firestore_toolset import FirestoreToolset
from google.adk import agents
from agentic_dsta.tools.google_ads.google_ads_getter import GoogleAdsGetterToolset
from agentic_dsta.tools.google_ads.google_ads_updater import GoogleAdsUpdaterToolset
from agentic_dsta.tools.google_ads.google_ads_asset_groups import GoogleAdsAssetGroupToolset
from agentic_dsta.tools.sa360.sa360_toolset import SA360Toolset
from agentic_dsta.tools.weather.weather_signals import WeatherSignalsToolset



logger = logging.getLogger(__name__)

# Default model, can be overridden
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION")


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
        DynamicMultiAPIToolset(),
        WeatherSignalsToolset(),
        FirestoreToolset(),
        SA360Toolset(),
        DateTimeToolset(),
    ]

    client = Client(
        vertexai=True,
        project=PROJECT_ID,
        location=LOCATION
    )

    configured_model = Gemini(model=model)
    configured_model.api_client = client

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

    # 3. Loop and Process Each Campaign
    successful_campaigns = 0
    failed_campaigns = 0

    for idx, campaign in enumerate(campaigns, start=1):
        campaign_id = campaign.get("campaignId")
        campaign_instruction = campaign.get("instruction", "No specific instruction.")

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

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")
        three_days_later_str = (now_utc + datetime.timedelta(days=2)).strftime("%Y-%m-%d")

        # Construct the context-rich prompt
        combined_instruction = f"""
        You are a Marketing Campaign Manager Agent.

        **Customer Context:**
        Customer ID: {customer_id}
        Global Strategy: {global_instruction}

        **Current Focus:**
        Campaign ID: {campaign_id}
        Campaign Specific Rules: {campaign_instruction}

        **Current Temporal Context:**
        - Today's Date: {today_str} (UTC)
        - Current Year: {now_utc.year}
        - 3-Day Forecast Window: {today_str} to {three_days_later_str}
        - Previous 3 Years for Historical Baseline: {now_utc.year - 1}, {now_utc.year - 2}, {now_utc.year - 3}

        **Task:**
        1. Analyze the current situation for Campaign {campaign_id}.
        2. Check if any external factors (Weather, POLLEN, AQI etc) are relevant based on the instructions.
           If so, use the API Hub tools to fetch that data.
        3. Check the campaign's current performance/status using GoogleAds tools for GoogleAds campaigns.
        4. Decide on an action (Pause, Enable, Change Bid, Change Location, or No Action).
        5. Execute the action if necessary.
        6. Provide a concise summary of your analysis and actions.

        **CRITICAL EXECUTION RULES:**
        - You MUST invoke tools directly using standard function calling one by one.
        - NEVER output Python code, scripts, loops, `print()` statements, or `import` statements. You do NOT have a Python code execution environment.
        - You already have today's date ({today_str}) in context. Do not try to run python to calculate dates or periods.
        - Any mathematical comparisons (e.g. comparing temperatures >= 3°C) must be performed directly in your thought reasoning, not in code blocks.
        """

        try:
            # Create a fresh agent for this campaign
            agent = create_agent(instruction=combined_instruction)

            # Wrap in App and Runner for execution
            app = apps.App(name="decision_app", root_agent=agent)
            runner = runners.InMemoryRunner(app=app)

            session_id = str(uuid.uuid4())
            await runner.session_service.create_session(
                session_id=session_id,
                user_id=customer_id,
                app_name="decision_app"
            )

            prompt_text = f"Proceed with the analysis and management of Campaign {campaign_id} based on your instructions."
            content = types.Content(parts=[types.Part(text=prompt_text)])

            logger.info("Executing agent runner for Campaign %s (session_id=%s)", campaign_id, session_id)
            async for chunk in runner.run_async(
                user_id=customer_id,
                session_id=session_id,
                new_message=content
            ):
                _log_agent_chunk(chunk, str(campaign_id))

            campaign_elapsed = time.perf_counter() - campaign_start_time
            logger.info(
                "Execution completed successfully for Campaign %s in %.2fs",
                campaign_id,
                campaign_elapsed,
                extra={"campaign_id": str(campaign_id), "duration_s": campaign_elapsed},
            )
            successful_campaigns += 1

        except Exception as e:
            campaign_elapsed = time.perf_counter() - campaign_start_time
            logger.exception(
                "Failed to process Campaign %s after %.2fs: %s",
                campaign_id,
                campaign_elapsed,
                e,
                extra={"campaign_id": str(campaign_id), "duration_s": campaign_elapsed},
            )
            failed_campaigns += 1
            continue

    total_elapsed = time.perf_counter() - total_start_time
    logger.info(
        "=== Completed Decision Agent Run for Customer %s in %.2fs (success: %d, failed: %d) ===",
        customer_id,
        total_elapsed,
        successful_campaigns,
        failed_campaigns,
        extra={
            "customer_id": str(customer_id),
            "total_duration_s": total_elapsed,
            "successful_campaigns": successful_campaigns,
            "failed_campaigns": failed_campaigns,
        },
    )


root_agent = create_agent(instruction="You are a decision agent helper.")
