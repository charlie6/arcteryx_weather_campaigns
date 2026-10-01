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
"""Tests for the structured monitoring events in agentic_dsta.core.telemetry."""

import json
import logging
from types import SimpleNamespace
from typing import Any, List

import pytest

from agentic_dsta.core import telemetry
from agentic_dsta.core.logging_config import JsonFormatter


def _events(caplog: pytest.LogCaptureFixture, event: str) -> List[logging.LogRecord]:
    """Returns captured records carrying the given monitoring event."""
    return [r for r in caplog.records if getattr(r, "event", None) == event]


def _tool(name: str) -> Any:
    """Builds a minimal stand-in for an ADK BaseTool."""
    return SimpleNamespace(name=name)


@pytest.mark.parametrize(
    "error, expected",
    [
        ("('invalid_grant: Token has been expired or revoked.')", "auth"),
        ("Failed to get Google Ads client.", "auth"),
        ("Request had insufficient scopes (Code: authorization_error: USER_PERMISSION_DENIED)", "auth"),
        ("429 RESOURCE_EXHAUSTED: Quota exceeded", "quota"),
        ("Weather API request failed: Read timed out.", "timeout"),
        ("503 Service Unavailable", "unavailable"),
        ("Campaign 123 not found", "not_found"),
        ("Invalid latitude: 'abc'.", "invalid_argument"),
        ("something odd", "other"),
        (None, "other"),
    ],
)
def test_classify_error(error: Any, expected: str) -> None:
    assert telemetry.classify_error(error) == expected


def test_classify_error_uses_exception_type_name() -> None:
    class RefreshError(Exception):
        pass

    assert telemetry.classify_error(RefreshError("boom")) == "auth"


@pytest.mark.parametrize(
    "tool_name, expected",
    [
        ("update_google_ads_campaign_budget", "google_ads"),
        ("list_google_ads_asset_groups", "google_ads"),
        ("update_sa360_campaign_status", "sa360"),
        ("get_24h_weather_signals", "weather"),
        ("set_document", "firestore"),
        ("get_current_datetime", "internal"),
    ],
)
def test_dependency_for_tool(tool_name: str, expected: str) -> None:
    assert telemetry.dependency_for_tool(tool_name) == expected


def test_event_fields_drops_none_values() -> None:
    assert telemetry.event_fields("run_completed", a=1, b=None) == {"event": "run_completed", "a": 1}


def test_run_summary_is_failure() -> None:
    summary = telemetry.RunSummary(customer_id="1", usecase="GoogleAds")
    assert not summary.is_failure
    for outcome in (telemetry.OUTCOME_FAILED, telemetry.OUTCOME_ABORTED):
        summary.outcome = outcome
        assert summary.is_failure
    for outcome in (telemetry.OUTCOME_PARTIAL, telemetry.OUTCOME_NOOP, telemetry.OUTCOME_SUCCESS):
        summary.outcome = outcome
        assert not summary.is_failure


# --- Agent callbacks ---------------------------------------------------------

RUN_CONTEXT = {
    "customer_id": "5341114500",
    "usecase": "GoogleAds",
    "run_id": "run-1",
    "campaign_id": "999",
    "playbook_id": "weather_asset_groups",
}


def test_successful_mutation_emits_mutation_applied(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        result = callbacks["after_tool_callback"](
            tool=_tool("update_google_ads_campaign_budget"),
            args={"customer_id": "5341114500", "campaign_id": "123", "new_budget_micros": 5},
            tool_context=None,
            tool_response={"success": True, "resource_name": "x"},
        )

    assert result is None  # Observe-only: never replaces the tool response.
    (record,) = _events(caplog, telemetry.EVENT_MUTATION_APPLIED)
    assert record.action == "budget"
    assert record.dependency == "google_ads"
    # The campaign the tool was actually called for wins over the run context.
    assert record.campaign_id == "123"
    assert record.dry_run == "false"
    assert record.run_id == "run-1"
    assert record.tool_args["new_budget_micros"] == 5
    assert not _events(caplog, telemetry.EVENT_TOOL_ERROR)


def test_dry_run_mutation_is_audited_not_errored(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        callbacks["after_tool_callback"](
            tool=_tool("update_google_ads_asset_group_status"),
            args={},
            tool_context=None,
            tool_response={"success": False, "dry_run": True, "action_suppressed": "x"},
        )

    (record,) = _events(caplog, telemetry.EVENT_MUTATION_APPLIED)
    assert record.dry_run == "true"
    assert record.action == "asset_group"
    assert record.campaign_id == "999"  # Falls back to the run context.
    assert not _events(caplog, telemetry.EVENT_TOOL_ERROR)


def test_failed_tool_emits_tool_error_with_class(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        callbacks["after_tool_callback"](
            tool=_tool("update_google_ads_campaign_status"),
            args={"campaign_id": "123"},
            tool_context=None,
            tool_response={
                "success": False,
                "error": "Failed to update campaign status: bad (Code: authentication_error: OAUTH_TOKEN_EXPIRED)",
            },
        )

    (record,) = _events(caplog, telemetry.EVENT_TOOL_ERROR)
    assert record.levelno == logging.ERROR
    assert record.error_class == "auth"
    assert record.mutating == "true"
    assert record.dependency == "google_ads"
    assert not _events(caplog, telemetry.EVENT_MUTATION_APPLIED)


def test_read_only_success_emits_nothing(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        callbacks["after_tool_callback"](
            tool=_tool("get_document"),
            args={},
            tool_context=None,
            tool_response={"exists": False, "message": "Document not found"},
        )
    assert not [r for r in caplog.records if getattr(r, "event", None)]


def test_firestore_read_error_is_a_tool_error(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        callbacks["after_tool_callback"](
            tool=_tool("get_document"),
            args={},
            tool_context=None,
            tool_response={"exists": False, "error": "503 Service Unavailable"},
        )
    (record,) = _events(caplog, telemetry.EVENT_TOOL_ERROR)
    assert record.dependency == "firestore"
    assert record.error_class == "unavailable"
    assert record.mutating == "false"


def test_raising_tool_emits_tool_error_and_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        result = callbacks["on_tool_error_callback"](
            tool=_tool("get_24h_weather_signals"),
            args={"latitude": 1.0},
            tool_context=None,
            error=TimeoutError("Read timed out"),
        )
    assert result is None  # None lets ADK re-raise the original error.
    (record,) = _events(caplog, telemetry.EVENT_TOOL_ERROR)
    assert record.dependency == "weather"
    assert record.error_class == "timeout"


def test_model_error_emits_model_error(caplog: pytest.LogCaptureFixture) -> None:
    callbacks = telemetry.make_agent_callbacks(RUN_CONTEXT)
    with caplog.at_level(logging.INFO):
        result = callbacks["on_model_error_callback"](
            callback_context=None,
            llm_request=None,
            error=RuntimeError("429 RESOURCE_EXHAUSTED"),
        )
    assert result is None
    (record,) = _events(caplog, telemetry.EVENT_MODEL_ERROR)
    assert record.dependency == "gemini"
    assert record.error_class == "quota"


def test_callbacks_never_raise(caplog: pytest.LogCaptureFixture) -> None:
    class Exploding:
        @property
        def name(self) -> str:
            raise RuntimeError("boom")

    callbacks = telemetry.make_agent_callbacks(None)
    assert callbacks["after_tool_callback"](Exploding(), {}, None, {}) is None
    assert callbacks["on_tool_error_callback"](Exploding(), {}, None, ValueError()) is None


def test_event_renders_under_jsonpayload_extra(caplog: pytest.LogCaptureFixture) -> None:
    """The Terraform filters rely on jsonPayload.extra.event; pin that shape."""
    logger = logging.getLogger("agentic_dsta.test_telemetry")
    with caplog.at_level(logging.INFO):
        logger.info("x", extra=telemetry.event_fields(telemetry.EVENT_RUN_COMPLETED, outcome="success"))
    payload = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert payload["extra"]["event"] == "run_completed"
    assert payload["extra"]["outcome"] == "success"
