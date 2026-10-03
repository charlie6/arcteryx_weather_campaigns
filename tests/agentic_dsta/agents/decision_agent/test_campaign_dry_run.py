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
"""End-to-end test of the per-campaign dry run through the real ADK runner.

A campaign's dry run flag is a context variable set in its worker thread.
Whether it reaches the tools depends on how ADK schedules tool calls, so
this test runs real playbooks: a scripted model calls the real budget tool
and the real Firestore ``set_document`` in one turn, several campaigns run on
reused worker threads, and only the campaigns in dry run may be suppressed.
Only the model, Google Ads and Firestore are faked.
"""

import asyncio
import logging
import re
import threading
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from google.adk import agents
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
import pytest

from agentic_dsta.agents.decision_agent import agent as agent_module
from agentic_dsta.core import dry_run
from agentic_dsta.core import telemetry
from agentic_dsta.tools.firestore import firestore_toolset as firestore_module
from agentic_dsta.tools.google_ads import google_ads_updater

CUSTOMER_ID = "5341114500"
CAMPAIGNS = ["100", "101", "102", "103"]
DRY_RUN_CAMPAIGNS = {"101", "103"}
PLAYBOOKS = [("weather_asset_groups", "Run the asset group playbook."), ("severe_budget", "Run the budget playbook.")]


class _Record:
    """What the fakes observed, across every worker thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mutated: List[str] = []  # campaign ids whose budget write reached Google Ads
        self.model_threads: List[Tuple[str, int]] = []  # (campaign id, thread id)


RECORD = _Record()


class _ScriptedModel(BaseLlm):
    """Changes the campaign's budget and logs it, then finishes."""

    model: str = "scripted"
    campaign_id: str = ""
    playbook_id: str = ""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        with RECORD.lock:
            RECORD.model_threads.append((self.campaign_id, threading.get_ident()))
        last = llm_request.contents[-1] if llm_request.contents else None
        if last is not None and any(part.function_response for part in last.parts or []):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="done")]))
            return
        # Two calls in one turn, so ADK runs them as a batch of tool calls.
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            name="update_google_ads_campaign_budget",
                            args={
                                "customer_id": CUSTOMER_ID,
                                "campaign_id": self.campaign_id,
                                "new_budget_micros": 2_000_000,
                            },
                        )
                    ),
                    types.Part(
                        function_call=types.FunctionCall(
                            name="set_document",
                            args={
                                "collection": "ChangeLog",
                                "document_id": f"run_{self.campaign_id}_{self.playbook_id}",
                                "data": {"campaignId": self.campaign_id, "mode": "live"},
                            },
                        )
                    ),
                ],
            )
        )


def _create_agent(instruction: str, run_context: Optional[Dict[str, Any]] = None, **_: Any) -> agents.LlmAgent:
    """create_agent with the scripted model and the two real tools."""
    context = run_context or {}
    return agents.LlmAgent(
        name="decision_agent",
        instruction=instruction,
        model=_ScriptedModel(campaign_id=str(context["campaign_id"]), playbook_id=str(context["playbook_id"])),
        tools=[FunctionTool(func=google_ads_updater.update_google_ads_campaign_budget), firestore_module.FirestoreToolset()],
        **telemetry.make_agent_callbacks(context),
    )


def _google_ads_client(_customer_id: str) -> MagicMock:
    """A fresh fake client per tool call that records which campaign it changed."""
    client = MagicMock()
    service = client.get_service.return_value
    seen: Dict[str, str] = {}

    def search_stream(customer_id: str, query: str) -> List[Any]:
        seen["campaign"] = re.search(r"campaign\.id = '(\d+)'", query).group(1)
        row = SimpleNamespace(
            campaign=SimpleNamespace(campaign_budget=f"customers/{customer_id}/campaignBudgets/{seen['campaign']}")
        )
        return [SimpleNamespace(results=[row])]

    def mutate_campaign_budgets(customer_id: str, operations: List[Any]) -> Any:
        with RECORD.lock:
            RECORD.mutated.append(seen["campaign"])
        return SimpleNamespace(results=[SimpleNamespace(resource_name=f"customers/{customer_id}/campaignBudgets/1")])

    service.search_stream.side_effect = search_stream
    service.mutate_campaign_budgets.side_effect = mutate_campaign_budgets
    return client


def _config_store() -> MagicMock:
    """The run's own Firestore reads: instructions and the campaign list."""
    campaigns = [
        {"campaignId": c, **({"dryRun": True} if c in DRY_RUN_CAMPAIGNS else {})} for c in CAMPAIGNS
    ]
    docs = {"CustomerInstructions": {"instruction": "hi"}, "GoogleAdsConfig": {"campaigns": campaigns}}

    def get_document(collection: str, document_id: str) -> Dict[str, Any]:
        if collection not in docs:
            return {"id": document_id, "exists": False}
        return {"id": document_id, "exists": True, "data": docs[collection]}

    store = MagicMock()
    store.get_document.side_effect = get_document
    store.query_collection.return_value = {"documents": []}
    return store


@pytest.mark.parametrize("concurrency", ["1", "2"])
def test_only_dry_run_campaigns_are_suppressed(concurrency: str, caplog: pytest.LogCaptureFixture) -> None:
    global RECORD
    RECORD = _Record()
    env = {
        agent_module.MAX_CONCURRENT_CAMPAIGNS_ENV: concurrency,
        dry_run.DRY_RUN_ENV_VAR: "",
        "CONFIG_SHEET_ID": "",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "FIRESTORE_DB": "test-firestore",
    }
    with patch.dict("os.environ", env), patch.object(
        agent_module, "FirestoreToolset", return_value=_config_store()
    ), patch.object(agent_module, "create_agent", side_effect=_create_agent), patch.object(
        agent_module.playbooks_lib, "resolve_playbook_ids", return_value={p for p, _ in PLAYBOOKS}
    ), patch.object(
        agent_module, "_load_playbook_library", return_value={p: {} for p, _ in PLAYBOOKS}
    ), patch.object(
        agent_module.playbooks_lib, "resolve_campaign_instructions", return_value=PLAYBOOKS
    ), patch.object(
        google_ads_updater, "get_google_ads_client", side_effect=_google_ads_client
    ), patch.object(
        firestore_module.firestore, "Client"
    ) as firestore_client, caplog.at_level(logging.INFO):
        summary = asyncio.run(agent_module.run_decision_agent(CUSTOMER_ID, "GoogleAds"))

    assert summary.outcome == telemetry.OUTCOME_SUCCESS, summary
    assert summary.successful_playbooks == len(CAMPAIGNS) * len(PLAYBOOKS)
    assert summary.dry_run_campaigns == len(DRY_RUN_CAMPAIGNS)

    # Every playbook of every campaign ran, on at most `concurrency` threads.
    assert {c for c, _ in RECORD.model_threads} == set(CAMPAIGNS)
    assert len({t for _, t in RECORD.model_threads}) <= int(concurrency)

    # Only the live campaigns' budget writes reached Google Ads.
    live = [c for c in CAMPAIGNS if c not in DRY_RUN_CAMPAIGNS]
    assert sorted(RECORD.mutated) == sorted(live * len(PLAYBOOKS))

    # The audit events say which writes were suppressed, and why.
    mutations = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_MUTATION_APPLIED]
    assert len(mutations) == len(CAMPAIGNS) * len(PLAYBOOKS)
    for record in mutations:
        if record.campaign_id in DRY_RUN_CAMPAIGNS:
            assert (record.dry_run, record.dry_run_source) == ("true", dry_run.SOURCE_CAMPAIGN)
        else:
            assert record.dry_run == "false"
            assert not hasattr(record, "dry_run_source")

    # ChangeLog rows of dry-run campaigns read 'log-only' even though the
    # model wrote 'live'.
    doc_ref = firestore_client.return_value.collection.return_value.document.return_value
    modes = [(call.args[0]["campaignId"], call.args[0]["mode"]) for call in doc_ref.set.call_args_list]
    assert len(modes) == len(CAMPAIGNS) * len(PLAYBOOKS)
    for campaign_id, mode in modes:
        assert mode == ("log-only" if campaign_id in DRY_RUN_CAMPAIGNS else "live"), modes

    completed = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_RUN_COMPLETED]
    assert completed[-1].dry_run_campaigns == len(DRY_RUN_CAMPAIGNS)

    # Nothing leaks back to the caller.
    assert not dry_run.is_dry_run()
