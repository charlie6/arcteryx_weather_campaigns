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
"""Tests for per-city activation thresholds and the pre-run sheet sync."""

import asyncio
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from agentic_dsta.agents.decision_agent import agent as agent_module
from agentic_dsta.agents.decision_agent import playbooks


def _toolset(baselines: Dict[str, Any]) -> MagicMock:
    toolset = MagicMock()

    def get_document(collection: str, document_id: str) -> Dict[str, Any]:
        if collection == "ClimateBaselines" and document_id in baselines:
            return {"id": document_id, "exists": True, "data": baselines[document_id]}
        return {"id": document_id, "exists": False}

    toolset.get_document.side_effect = get_document
    return toolset


class TestCityValues:
    def test_reads_each_city_once_per_run(self):
        toolset = _toolset({"Vancouver BC": {"activation": {"coldBelowC": 5.0}}})
        cache: Dict[str, Dict[str, Any]] = {}
        first = agent_module._city_values(toolset, "Vancouver BC", cache)
        second = agent_module._city_values(toolset, "Vancouver BC", cache)
        assert first == second == {
            "activation": {"coldBelowC": 5.0},
            "activationSource": playbooks.ACTIVATION_SOURCE_PARTIAL,
        }
        assert toolset.get_document.call_count == 1

    def test_missing_city_falls_back_to_defaults(self):
        values = agent_module._city_values(_toolset({}), "Nowhere", {})
        assert values == {"activationSource": playbooks.ACTIVATION_SOURCE_DEFAULT}

    def test_read_error_falls_back_to_defaults(self):
        toolset = MagicMock()
        toolset.get_document.side_effect = RuntimeError("unavailable")
        values = agent_module._city_values(toolset, "Vancouver BC", {})
        assert values == {"activationSource": playbooks.ACTIVATION_SOURCE_DEFAULT}

    def test_no_city_gives_no_values(self):
        assert agent_module._city_values(_toolset({}), None, {}) == {}


class TestSheetSyncHook:
    def test_skipped_without_a_sheet_id(self, monkeypatch):
        monkeypatch.delenv("CONFIG_SHEET_ID", raising=False)
        with patch.object(agent_module.config_sync, "run_sheet_sync") as run_sync:
            agent_module._maybe_sync_config_sheet(MagicMock(), "5341114500")
        run_sync.assert_not_called()

    def test_runs_for_the_current_customer(self, monkeypatch):
        monkeypatch.setenv("CONFIG_SHEET_ID", "sheet123")
        toolset = MagicMock()
        with patch.object(agent_module.config_sync, "run_sheet_sync") as run_sync:
            agent_module._maybe_sync_config_sheet(toolset, "5341114500")
        run_sync.assert_called_once_with(
            "sheet123", toolset._get_client.return_value, customer_id="5341114500", source="scheduled_run"
        )

    def test_runs_before_config_is_read_and_passes_city_values(self, monkeypatch):
        monkeypatch.setenv("CONFIG_SHEET_ID", "sheet123")
        order = []
        docs = {
            "CustomerInstructions": {"instruction": "hi"},
            "GoogleAdsConfig": {"campaigns": [{"campaignId": "1", "params": {"city": "Vancouver BC"}}]},
            "ClimateBaselines": {"activation": {"coldBelowC": 2.0, "rainRateMmPerH": 1.0, "sunnyMinHours": 6}},
        }
        toolset = MagicMock()

        def get_document(collection: str, document_id: str) -> Dict[str, Any]:
            order.append(collection)
            return {"id": document_id, "exists": True, "data": docs[collection]}

        toolset.get_document.side_effect = get_document
        toolset.query_collection.return_value = {"documents": []}

        def fake_sync(*_args, **_kwargs):
            order.append("sync")

        resolve = MagicMock(return_value=[])
        with patch.object(agent_module, "FirestoreToolset", return_value=toolset), \
             patch.object(agent_module.config_sync, "run_sheet_sync", side_effect=fake_sync), \
             patch.object(agent_module, "_load_playbook_library", return_value={}), \
             patch.object(agent_module.playbooks_lib, "resolve_campaign_instructions", resolve):
            asyncio.run(agent_module.run_decision_agent("5341114500", "GoogleAds"))

        assert order[0] == "sync"
        city_values = resolve.call_args.kwargs["city_values"]
        assert city_values["activation"] == {"coldBelowC": 2.0, "rainRateMmPerH": 1.0, "sunnyMinHours": 6}
        assert city_values["activationSource"] == playbooks.ACTIVATION_SOURCE_CITY
