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
"""Tests for parallel campaign execution in the decision agent runner."""

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from agentic_dsta.agents.decision_agent import agent as agent_module
from agentic_dsta.core import telemetry

CUSTOMER_ID = "5341114500"


class _Tracker:
    """Records concurrency and call order across worker threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: List[Tuple[str, str, int]] = []  # (campaign, playbook, thread id)


TRACKER = _Tracker()


class _SlowRunner:
    """InMemoryRunner stand-in that blocks like a synchronous tool call."""

    sleep_s = 0.2
    hang_campaigns: List[str] = []

    def __init__(self, app: Any) -> None:
        self.session_service = MagicMock()

        async def create_session(**_: Any) -> None:
            return None

        self.session_service.create_session = create_session

    async def run_async(self, user_id: str, session_id: str, new_message: Any):
        text = new_message.parts[0].text
        campaign = text.split("Campaign ")[1].split(" ")[0]
        playbook = text.split("'")[1]
        if campaign in self.hang_campaigns:
            await asyncio.sleep(30)
        with TRACKER.lock:
            TRACKER.active += 1
            TRACKER.max_active = max(TRACKER.max_active, TRACKER.active)
            TRACKER.calls.append((campaign, playbook, threading.get_ident()))
        time.sleep(self.sleep_s)  # blocking I/O, as the real sync tools are
        with TRACKER.lock:
            TRACKER.active -= 1
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _firestore(n_campaigns: int) -> MagicMock:
    toolset = MagicMock()
    docs = {
        "CustomerInstructions": {"instruction": "hi"},
        "GoogleAdsConfig": {"campaigns": [{"campaignId": str(100 + i)} for i in range(n_campaigns)]},
    }

    def get_document(collection: str, document_id: str) -> Dict[str, Any]:
        data = docs.get(collection)
        if data is None:
            return {"id": document_id, "exists": False}
        return {"id": document_id, "exists": True, "data": data}

    toolset.get_document.side_effect = get_document
    toolset.query_collection.return_value = {"documents": []}
    return toolset


def _run(
    n_campaigns: int,
    concurrency: Optional[str] = None,
    timeout_s: Optional[float] = None,
    hang: Optional[List[str]] = None,
) -> Tuple[telemetry.RunSummary, float]:
    global TRACKER
    TRACKER = _Tracker()
    _SlowRunner.hang_campaigns = hang or []
    env = {agent_module.MAX_CONCURRENT_CAMPAIGNS_ENV: concurrency} if concurrency else {}
    patches = [
        patch.dict("os.environ", env),
        patch.object(agent_module, "FirestoreToolset", return_value=_firestore(n_campaigns)),
        patch.object(agent_module, "create_agent", return_value=MagicMock()),
        patch.object(agent_module.apps, "App", MagicMock()),
        patch.object(agent_module.runners, "InMemoryRunner", _SlowRunner),
        patch.object(agent_module.playbooks_lib, "resolve_playbook_ids", return_value={"a", "b"}),
        patch.object(agent_module, "_load_playbook_library", return_value={"a": {}, "b": {}}),
        patch.object(
            agent_module.playbooks_lib,
            "resolve_campaign_instructions",
            return_value=[("weather_asset_groups", "x"), ("severe_budget", "y")],
        ),
    ]
    if timeout_s is not None:
        patches.append(patch.object(agent_module, "_playbook_timeout_seconds", return_value=timeout_s))
    for p in patches:
        p.start()
    try:
        start = time.perf_counter()
        summary = asyncio.run(agent_module.run_decision_agent(CUSTOMER_ID, "GoogleAds"))
        return summary, time.perf_counter() - start
    finally:
        for p in reversed(patches):
            p.stop()


def test_campaigns_run_in_parallel_within_limit() -> None:
    summary, elapsed = _run(6, concurrency="3")
    assert summary.outcome == telemetry.OUTCOME_SUCCESS
    assert summary.successful_playbooks == 12
    assert summary.eligible_campaigns == 6
    assert TRACKER.max_active == 3
    # Sequential would be 6 campaigns x 2 playbooks x 0.2s = 2.4s.
    assert elapsed < 1.6


def test_concurrency_one_is_sequential() -> None:
    _run(3, concurrency="1")
    assert TRACKER.max_active == 1


def test_playbooks_of_a_campaign_run_in_order_on_one_thread() -> None:
    _run(4, concurrency="4")
    by_campaign: Dict[str, List[Tuple[str, int]]] = {}
    for campaign, playbook, thread_id in TRACKER.calls:
        by_campaign.setdefault(campaign, []).append((playbook, thread_id))
    assert len(by_campaign) == 4
    for calls in by_campaign.values():
        assert [p for p, _ in calls] == ["weather_asset_groups", "severe_budget"]
        assert calls[0][1] == calls[1][1]


def test_hung_playbook_times_out_without_blocking_others(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        summary, elapsed = _run(3, concurrency="3", timeout_s=0.5, hang=["100"])
    assert elapsed < 5
    # Campaign 100: both playbooks time out. The other two succeed.
    assert (summary.successful_playbooks, summary.failed_playbooks) == (4, 2)
    assert summary.outcome == telemetry.OUTCOME_PARTIAL
    failures = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_PLAYBOOK_FAILED]
    assert {r.campaign_id for r in failures} == {"100"}
    assert all(r.error_class == "timeout" for r in failures)


def test_worker_crash_is_counted_as_failure(caplog: pytest.LogCaptureFixture) -> None:
    jobs = [
        agent_module._CampaignJob(index=1, total=2, campaign={"campaignId": "1"}, instructions=[("a", "x"), ("b", "y")]),
        agent_module._CampaignJob(index=2, total=2, campaign={"campaignId": "2"}, instructions=[("a", "x")]),
    ]

    def worker(job: Any) -> Any:
        if job.campaign["campaignId"] == "1":
            raise RuntimeError("boom")
        return agent_module._CampaignResult(campaign_id="2", eligible=True, succeeded=1)

    with caplog.at_level(logging.INFO):
        results = asyncio.run(
            agent_module._run_campaign_jobs(jobs, 2, worker, customer_id=CUSTOMER_ID, usecase="GoogleAds", run_id="r")
        )
    assert [(r.campaign_id, r.succeeded, r.failed) for r in results] == [("1", 0, 2), ("2", 1, 0)]
    failures = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_PLAYBOOK_FAILED]
    assert len(failures) == 1 and failures[0].campaign_id == "1"


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 5), ("", 5), ("8", 8), ("0", 1), ("99", 20), ("abc", 5)],
)
def test_max_concurrent_campaigns_setting(raw: Optional[str], expected: int) -> None:
    env = {agent_module.MAX_CONCURRENT_CAMPAIGNS_ENV: raw} if raw is not None else {}
    with patch.dict("os.environ", env, clear=False):
        if raw is None:
            import os
            os.environ.pop(agent_module.MAX_CONCURRENT_CAMPAIGNS_ENV, None)
        assert agent_module._max_concurrent_campaigns() == expected


def test_playbook_timeout_setting_bounds() -> None:
    with patch.dict("os.environ", {agent_module.PLAYBOOK_TIMEOUT_ENV: "5"}):
        assert agent_module._playbook_timeout_seconds() == 60
    with patch.dict("os.environ", {agent_module.PLAYBOOK_TIMEOUT_ENV: "900"}):
        assert agent_module._playbook_timeout_seconds() == 900
