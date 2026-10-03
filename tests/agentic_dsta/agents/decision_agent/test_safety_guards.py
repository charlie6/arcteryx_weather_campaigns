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
"""Tests for the runner's deterministic safety guards.

Covers the campaign name guard (enforced in code because end-to-end testing
showed the model can ignore the prompt-level check) and the change volume
guard, which counts only budget increases.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from agentic_dsta.agents.decision_agent import agent as agent_module
from agentic_dsta.core import telemetry

CUSTOMER_ID = "5341114500"


# --- _campaign_name_mismatch -------------------------------------------------


def _campaign(name_contains: Optional[str]) -> Dict[str, Any]:
    params: Dict[str, Any] = {"city": "Toronto ON"}
    if name_contains is not None:
        params["campaignNameContains"] = name_contains
    return {"campaignId": 24310181894, "params": params}


def test_no_name_configured_skips_lookup() -> None:
    with patch.object(agent_module, "_fetch_campaign_name") as fetch:
        assert agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign(None)) is None
        assert agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign("  ")) is None
    fetch.assert_not_called()


def test_matching_name_passes_case_insensitively() -> None:
    with patch.object(agent_module, "_fetch_campaign_name", return_value="ADSTA Weather PMax Test - Toronto"):
        assert agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign("toronto")) is None
        assert agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign(" Toronto ")) is None


def test_mismatched_name_is_reported() -> None:
    with patch.object(agent_module, "_fetch_campaign_name", return_value="ADSTA Weather PMax Test - Toronto"):
        result = agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign("WRONGNAME"))
    assert result is not None
    assert result["reason"] == "mismatch"
    assert result["expected"] == "WRONGNAME"
    assert result["actual"] == "ADSTA Weather PMax Test - Toronto"


def test_missing_campaign_fails_closed() -> None:
    with patch.object(agent_module, "_fetch_campaign_name", return_value=None):
        result = agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign("Toronto"))
    assert result is not None and result["reason"] == "not_found"


def test_lookup_error_fails_closed() -> None:
    with patch.object(agent_module, "_fetch_campaign_name", side_effect=RuntimeError("503 unavailable")):
        result = agent_module._campaign_name_mismatch(CUSTOMER_ID, _campaign("Toronto"))
    assert result is not None and result["reason"] == "lookup_failed"
    assert "503" in result["detail"]


# --- Name guard inside a run -------------------------------------------------


def _firestore(config: Dict[str, Any]) -> MagicMock:
    toolset = MagicMock()
    docs = {"CustomerInstructions": {"instruction": "hi"}, "GoogleAdsConfig": config}

    def get_document(collection: str, document_id: str) -> Dict[str, Any]:
        data = docs.get(collection)
        if data is None:
            return {"id": document_id, "exists": False}
        return {"id": document_id, "exists": True, "data": data}

    toolset.get_document.side_effect = get_document
    toolset.query_collection.return_value = {"documents": []}
    return toolset


class _RecordingRunner:
    """InMemoryRunner stand-in that records which campaigns were run."""

    prompts: List[str] = []

    def __init__(self, app: Any) -> None:
        self.session_service = MagicMock()

        async def create_session(**_: Any) -> None:
            return None

        self.session_service.create_session = create_session

    async def run_async(self, user_id: str, session_id: str, new_message: Any):
        _RecordingRunner.prompts.append(new_message.parts[0].text)
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _run(config: Dict[str, Any], names: Dict[str, Optional[str]]):
    _RecordingRunner.prompts = []
    firestore = _firestore(config)

    def fetch(customer_id: str, campaign_id: Any) -> Optional[str]:
        return names.get(str(campaign_id))

    with patch.object(agent_module, "FirestoreToolset", return_value=firestore), \
         patch.object(agent_module, "create_agent", return_value=MagicMock()), \
         patch.object(agent_module.apps, "App", MagicMock()), \
         patch.object(agent_module.runners, "InMemoryRunner", _RecordingRunner), \
         patch.object(agent_module, "_fetch_campaign_name", side_effect=fetch), \
         patch.object(agent_module.playbooks_lib, "resolve_playbook_ids", return_value={"pb"}), \
         patch.object(agent_module, "_load_playbook_library", return_value={"pb": {}}), \
         patch.object(
             agent_module.playbooks_lib,
             "resolve_campaign_instructions",
             return_value=[("pb", "do the thing")],
         ):
        summary = asyncio.run(agent_module.run_decision_agent(CUSTOMER_ID, "GoogleAds"))
    return summary, firestore


CONFIG = {
    "campaigns": [
        {"campaignId": "111", "params": {"campaignNameContains": "Vancouver", "city": "Vancouver BC"}},
        {"campaignId": "222", "params": {"campaignNameContains": "WRONGNAME", "city": "Toronto ON"}},
    ]
}


def test_mismatched_campaign_never_runs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary, firestore = _run(CONFIG, {"111": "PMax - Vancouver", "222": "PMax - Toronto"})

    # Only the matching campaign reached the model.
    assert any("Campaign 111 " in p for p in _RecordingRunner.prompts)
    assert not any("Campaign 222 " in p for p in _RecordingRunner.prompts)

    assert summary.outcome == telemetry.OUTCOME_PARTIAL
    assert summary.reason == telemetry.REASON_CAMPAIGNS_SKIPPED
    assert summary.skipped_campaigns == 1
    assert summary.eligible_campaigns == 1
    assert not summary.is_failure

    events = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_CAMPAIGN_NAME_MISMATCH]
    assert len(events) == 1
    assert events[0].campaign_id == "222"
    assert events[0].levelno == logging.ERROR
    completed = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_RUN_COMPLETED]
    assert completed[0].skipped_campaigns == 1

    # Audit trail: a ChangeLog row records the skip.
    rows = [c.kwargs for c in firestore.set_document.call_args_list if c.kwargs.get("collection") == "ChangeLog"]
    assert len(rows) == 1
    assert rows[0]["data"]["campaignId"] == "222"
    assert rows[0]["data"]["budgetAfterMicros"] == "unchanged"
    assert "name guard" in rows[0]["data"]["notes"]


def test_all_matching_is_success() -> None:
    summary, firestore = _run(CONFIG, {"111": "PMax - Vancouver", "222": "PMax WRONGNAME"})
    assert summary.outcome == telemetry.OUTCOME_SUCCESS
    assert summary.skipped_campaigns == 0
    firestore.set_document.assert_not_called()


# --- Change volume guard -----------------------------------------------------


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000}, True),
        ({"budgetBeforeMicros": "1000000", "budgetAfterMicros": "1500000"}, True),
        ({"budgetBeforeMicros": 1500000, "budgetAfterMicros": 1000000}, False),  # revert
        ({"budgetBeforeMicros": 1000000, "budgetAfterMicros": "unchanged"}, False),
        ({"budgetBeforeMicros": "unchanged", "budgetAfterMicros": "unchanged"}, False),
        ({"budgetAfterMicros": 1500000}, True),  # unknown before: err towards alerting
        ({"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1000000}, False),
        ({}, False),
    ],
)
def test_is_budget_increase(row: Dict[str, Any], expected: bool) -> None:
    assert agent_module._is_budget_increase(row) is expected


def _guard(
    rows: List[Dict[str, Any]],
    eligible: int,
    caplog: pytest.LogCaptureFixture,
    customer_id: Optional[str] = None,
) -> List[logging.LogRecord]:
    toolset = MagicMock()
    toolset.query_collection.return_value = {"documents": [{"data": r} for r in rows]}
    with caplog.at_level(logging.INFO):
        agent_module._check_change_volume_guard(
            firestore_toolset=toolset,
            run_id="run-1",
            guard_config={"enabled": True, "maxFractionOfEligibleCampaigns": 0.25},
            eligible_campaigns=eligible,
            customer_id=customer_id,
        )
    return [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_CHANGE_GUARD_EXCEEDED]


def test_guard_ignores_reverts(caplog: pytest.LogCaptureFixture) -> None:
    # A storm ending in both cities: two reverts, cap 1. Must not alert.
    rows = [
        {"budgetBeforeMicros": 2000000, "budgetAfterMicros": 1400000},
        {"budgetBeforeMicros": 2000000, "budgetAfterMicros": 1750000},
    ]
    assert _guard(rows, eligible=2, caplog=caplog) == []


def test_guard_fires_on_increases(caplog: pytest.LogCaptureFixture) -> None:
    rows = [
        {"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000},
        {"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000},
        {"budgetBeforeMicros": 2000000, "budgetAfterMicros": 1000000},
    ]
    events = _guard(rows, eligible=2, caplog=caplog)
    assert len(events) == 1
    assert events[0].budget_changes == 2
    assert events[0].cap == 1


def test_guard_event_names_the_account(caplog: pytest.LogCaptureFixture) -> None:
    # One deployment can run several accounts; the alert must say which one.
    rows = [{"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000}] * 2
    events = _guard(rows, eligible=2, caplog=caplog, customer_id=CUSTOMER_ID)
    assert len(events) == 1
    assert events[0].customer_id == CUSTOMER_ID


def test_guard_counts_dry_run_increases_and_reports_them(caplog: pytest.LogCaptureFixture) -> None:
    # A bad weather feed shows up in dry-run campaigns too, so their
    # would-have increases count towards the cap. The event says how many
    # of them changed nothing, so the operator knows the real spend added.
    rows = [
        {"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000, "mode": "live"},
        {"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000, "mode": "log-only"},
        {"budgetBeforeMicros": 1000000, "budgetAfterMicros": 1500000, "mode": " Log-Only "},
        {"budgetBeforeMicros": 2000000, "budgetAfterMicros": 1000000, "mode": "log-only"},  # revert
    ]
    events = _guard(rows, eligible=2, caplog=caplog)
    assert len(events) == 1
    assert events[0].budget_changes == 3
    assert events[0].log_only_changes == 2
    assert "(2 log-only)" in events[0].getMessage()
