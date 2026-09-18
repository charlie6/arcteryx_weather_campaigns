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
"""Marketing agent for managing marketing campaigns interactively."""
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

from agentic_dsta.tools.google_ads.google_ads_getter import GoogleAdsGetterToolset
from agentic_dsta.tools.google_ads.google_ads_updater import GoogleAdsUpdaterToolset
from agentic_dsta.tools.google_ads.google_ads_asset_groups import GoogleAdsAssetGroupToolset
from agentic_dsta.tools.firestore.firestore_toolset import FirestoreToolset
from agentic_dsta.tools.sa360.sa360_toolset import SA360Toolset
from agentic_dsta.tools.weather.weather_signals import WeatherSignalsToolset
from google.adk import agents
from google.adk.models.google_llm import Gemini
from google.genai import Client


import os

# gemini-2.5-pro retires on 2026-10-20. There is no GA text-only Gemini 3 Pro;
# gemini-3.5-flash is the successor, offering near-Pro reasoning at Flash cost.
model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# See the comment in decision_agent/agent.py: Gemini 3.x is not served from any
# single US region, so the model endpoint must be a multi-region one even though
# Cloud Run itself runs in us-central1. Passing an explicit client avoids ADK
# falling back to GOOGLE_CLOUD_LOCATION, which would resolve to us-central1 and
# fail to find the model.
GEMINI_LOCATION = (
    os.environ.get("GEMINI_LOCATION")
    or os.environ.get("GOOGLE_CLOUD_LOCATION")
    or "us"
)

with open(os.path.join(os.path.dirname(__file__), "prompt.txt"), "r", encoding='utf-8') as f:
    prompt = f.read()

root_agent = agents.LlmAgent(
    instruction=prompt,
    model=Gemini(
        model=model,
        client=Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=GEMINI_LOCATION,
        ),
    ),
    name="marketing_campaign_manager",
    tools=[
        GoogleAdsGetterToolset(),
        GoogleAdsUpdaterToolset(),
        GoogleAdsAssetGroupToolset(),
        WeatherSignalsToolset(),
        FirestoreToolset(),
        SA360Toolset(),
    ],
)
