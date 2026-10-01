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
"""Tests that run_decision_agent reports honest outcomes and run events."""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from agentic_dsta.agents.decision_agent import agent as agent_module
from agentic_dsta.core import telemetry

CUSTOMER_ID = "5341114500"
CONFIG = {"campaigns": [{"campaignId": "111"}, {"campaignId": "222"}]}


def _firestore(docs: Dict[str, Optional[Dict[str, Any]]]) -> MagicMock:
    """Builds a FirestoreToolset mock serving documents keyed by collection."""
    toolset = MagicMock()

    def get_document(collection: str, document_id: str) -> Dict[str, Any]:
        data = docs.get(collection)
        if data is None:
            # Mirrors the real toolset: a missing doc is a truthy dict.
            return {"id": document_id, "exists": False, "message": "Document not found"}
        return {"id": document_id, "exists": True, "data": data}

    toolset.get_document.side_effect = get_document
    toolset.query_collection.return_value = {"documents": []}
    return toolset


class _Runner:
    """InMemoryRunner stand-in whose run fails for chosen campaigns."""

    fail_campaigns: List[str] = []

    def __init__(self, app: Any) -> None:
        self.session_service = MagicMock()

        async def create_session(**_: Any) -> None:
            return None

        self.session_service.create_session = create_session

    async def run_async(self, user_id: str, session_id: str, new_message: Any):
        text = new_message.parts[0].text
        if any(f"Campaign {c} " in text for c in self.fail_campaigns):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _run(docs: Dict[str, Optional[Dict[str, Any]]], fail_campaigns: List[str]) -> telemetry.RunSummary:
    _Runner.fail_campaigns = fail_campaigns
    with patch.object(agent_module, "FirestoreToolset", return_value=_firestore(docs)), \
         patch.object(agent_module, "create_agent", return_value=MagicMock()), \
         patch.object(agent_module.apps, "App", MagicMock()), \
         patch.object(agent_module.runners, "InMemoryRunner", _Runner), \
         patch.object(agent_module.playbooks_lib, "resolve_playbook_ids", return_value={"pb"}), \
         patch.object(agent_module, "_load_playbook_library", return_value={"pb": {}}), \
         patch.object(
             agent_module.playbooks_lib,
             "resolve_campaign_instructions",
             return_value=[("pb", "do the thing")],
         ):
        return asyncio.run(agent_module.run_decision_agent(CUSTOMER_ID, "GoogleAds"))


def _completed(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_RUN_COMPLETED]
    assert len(records) == 1, "every run must emit exactly one run_completed event"
    return records[0]


def test_missing_instructions_aborts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary = _run({"GoogleAdsConfig": CONFIG}, [])
    assert summary.outcome == telemetry.OUTCOME_ABORTED
    assert summary.reason == telemetry.ABORT_MISSING_INSTRUCTIONS
    assert summary.is_failure
    record = _completed(caplog)
    assert record.outcome == "aborted"
    assert record.levelno == logging.ERROR


def test_missing_config_aborts_rather_than_noop(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary = _run({"CustomerInstructions": {"instruction": "hi"}}, [])
    assert summary.outcome == telemetry.OUTCOME_ABORTED
    assert summary.reason == telemetry.ABORT_MISSING_CONFIG
    assert _completed(caplog).reason == telemetry.ABORT_MISSING_CONFIG


def test_empty_campaigns_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary = _run({"CustomerInstructions": {"instruction": "hi"}, "GoogleAdsConfig": {"campaigns": []}}, [])
    assert summary.outcome == telemetry.OUTCOME_NOOP
    assert not summary.is_failure


def test_all_success(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary = _run({"CustomerInstructions": {"instruction": "hi"}, "GoogleAdsConfig": CONFIG}, [])
    assert summary.outcome == telemetry.OUTCOME_SUCCESS
    assert summary.successful_playbooks == 2
    assert summary.run_id
    record = _completed(caplog)
    assert record.run_id == summary.run_id
    assert record.usecase == "GoogleAds"


def test_partial_failure(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary = _run({"CustomerInstructions": {"instruction": "hi"}, "GoogleAdsConfig": CONFIG}, ["111"])
    assert summary.outcome == telemetry.OUTCOME_PARTIAL
    assert (summary.successful_playbooks, summary.failed_playbooks) == (1, 1)
    assert not summary.is_failure
    failures = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_PLAYBOOK_FAILED]
    assert len(failures) == 1
    assert failures[0].campaign_id == "111"
    assert failures[0].error_class == "quota"


def test_total_failure(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary = _run({"CustomerInstructions": {"instruction": "hi"}, "GoogleAdsConfig": CONFIG}, ["111", "222"])
    assert summary.outcome == telemetry.OUTCOME_FAILED
    assert summary.is_failure


def test_unhandled_exception_still_emits_run_completed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO), \
         patch.object(agent_module, "FirestoreToolset", side_effect=RuntimeError("no creds")):
        with pytest.raises(RuntimeError):
            asyncio.run(agent_module.run_decision_agent(CUSTOMER_ID, "GoogleAds"))
    record = _completed(caplog)
    assert record.outcome == "failed"
    assert record.reason == "unhandled_exception"
