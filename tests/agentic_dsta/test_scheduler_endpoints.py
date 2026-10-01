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

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient
import os

# Ensure we can import from agentic_dsta
import sys
sys.path.append(os.getcwd())

from agentic_dsta.main import app

client = TestClient(app)

import asyncio

def test_scheduler_init_and_run_success():
    async def run_test():
        payload = {
            "app_name": "decision_agent",
            "new_message": {
                "parts": [{"text": "Analyse and update customerid: 4086619433"}],
                "role": "user"
            },
            "session_id": "session-2026-01-04T15:39:43Z",
            "streaming": False,
            "user_id": "test-v5-runner@gta-solutions-agentic-dsta.iam.gserviceaccount.com",
            "customer_id": "4086619433"
        }

        with patch("agentic_dsta.main.run_decision_agent", new_callable=AsyncMock) as mock_run_agent:
            
            # Mock successful run
            mock_run_agent.return_value = {"status": "success"}

            response = client.post(
                "/scheduler/init_and_run",
                json=payload,
                headers={"Authorization": "Bearer token"}
            )

            assert response.status_code == 200
            # Expecting success message from endpoint
            assert response.json() == {'message': 'Decision agent run completed for 4086619433', 'status': 'success'}

            # Verify run_decision_agent was called with correct customer_id
            mock_run_agent.assert_called_once_with("4086619433", None)

    asyncio.run(run_test())

def test_scheduler_init_and_run_session_failure():
    async def run_test():
        payload = {
            "app_name": "decision_agent",
            "new_message": {
                "parts": [{"text": "No customer id here"}], # Lacks customer_id
                "role": "user"
            }
        }

        # The endpoint /scheduler/init_and_run is expected to return a 400
        # when get_customer_id_from_task raises a ValueError.
        response = client.post("/scheduler/init_and_run", json=payload)
        assert response.status_code == 400
        assert "Missing customer_id" in response.json()["detail"]

    asyncio.run(run_test())


def _post_with_summary(summary):
    """Posts a scheduler request with run_decision_agent returning `summary`."""
    payload = {"app_name": "decision_agent", "customer_id": "534-111-4500", "usecase": "GoogleAds"}
    with patch("agentic_dsta.main.run_decision_agent", new_callable=AsyncMock) as mock_run_agent:
        mock_run_agent.return_value = summary
        return client.post("/scheduler/init_and_run", json=payload)


def test_scheduler_failed_run_returns_500():
    from agentic_dsta.core import telemetry

    summary = telemetry.RunSummary(
        customer_id="5341114500", usecase="GoogleAds",
        outcome=telemetry.OUTCOME_ABORTED, reason=telemetry.ABORT_MISSING_CONFIG,
    )
    response = _post_with_summary(summary)
    assert response.status_code == 500
    assert "missing_config" in response.json()["detail"]


def test_scheduler_partial_run_returns_200_partial():
    from agentic_dsta.core import telemetry

    summary = telemetry.RunSummary(
        customer_id="5341114500", usecase="GoogleAds", outcome=telemetry.OUTCOME_PARTIAL,
        successful_playbooks=1, failed_playbooks=1,
    )
    response = _post_with_summary(summary)
    assert response.status_code == 200
    assert response.json()["status"] == "partial"
