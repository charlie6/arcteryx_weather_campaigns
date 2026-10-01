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
"""Structured monitoring events for Agentic DSTA.

Cloud Monitoring builds its log-based metrics and alerts on the ``event``
field of each structured log line (``jsonPayload.extra.event`` once rendered
by ``JsonFormatter``). Matching on message text is brittle: rewording a log
line would silently break an alert. This module is therefore the single
source of truth for event names and their low-cardinality labels. The
Terraform ``monitoring`` module filters on exactly these strings, so rename
an event here only together with ``infra/terraform/modules/monitoring``.

Tool-level events (``tool_error`` and ``mutation_applied``) are emitted from
ADK agent callbacks rather than from each tool. One hook point covers every
current and future tool, and the tools themselves stay free of monitoring
code. The callbacks only observe: they always return None, so they never
alter a tool response or swallow an error, and any failure inside them is
logged and discarded.
"""

import dataclasses
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# --- Event names (keep in sync with infra/terraform/modules/monitoring) ---
EVENT_RUN_STARTED = "run_started"
EVENT_RUN_COMPLETED = "run_completed"
EVENT_PLAYBOOK_FAILED = "playbook_failed"
EVENT_MUTATION_APPLIED = "mutation_applied"
EVENT_TOOL_ERROR = "tool_error"
EVENT_MODEL_ERROR = "model_error"
EVENT_CHANGE_GUARD_EXCEEDED = "change_guard_exceeded"

# --- Run outcomes ---
OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_ABORTED = "aborted"
OUTCOME_NOOP = "noop"

# --- Abort reasons ---
ABORT_MISSING_INSTRUCTIONS = "missing_instructions"
ABORT_MISSING_CONFIG = "missing_config"
ABORT_NO_CAMPAIGNS = "no_campaigns"

# Advertising platform writes and the action label each one reports. Firestore
# writes are ADSTA's own state and are deliberately not counted as mutations.
MUTATING_TOOL_ACTIONS: Dict[str, str] = {
    "update_google_ads_campaign_status": "campaign_status",
    "update_google_ads_campaign_budget": "budget",
    "update_google_ads_shared_budget": "budget",
    "update_google_ads_bidding_strategy": "bidding",
    "update_google_ads_portfolio_bidding_strategy": "bidding",
    "update_google_ads_campaign_geo_targets": "geo",
    "update_google_ads_ad_group_geo_targets": "geo",
    "update_google_ads_asset_group_status": "asset_group",
    "update_google_ads_asset_group_status_by_name": "asset_group",
    "update_sa360_campaign_status": "campaign_status",
    "update_sa360_campaign_budget": "budget",
    "update_sa360_campaign_geolocation": "geo",
}

# Ordered: the first matching class wins, so the more specific classes come
# first. Auth failures (typically an expired or revoked refresh token) are the
# most important to single out because they stop every run until a human acts.
_ERROR_CLASS_PATTERNS = (
    (
        "auth",
        re.compile(
            r"invalid_grant|refresherror|authentication_error|authorization_error|"
            r"unauthenticated|permission_denied|oauth_token|developer_token|"
            r"failed to obtain credentials|failed to get google ads client|"
            r"\b401\b|\b403\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quota",
        re.compile(
            r"resource_exhausted|quota|rate.?limit|too many requests|\b429\b",
            re.IGNORECASE,
        ),
    ),
    (
        "timeout",
        re.compile(r"timed? ?out|deadline|\b504\b", re.IGNORECASE),
    ),
    (
        "unavailable",
        re.compile(r"unavailable|\b503\b|\b502\b|\b500\b|internal error", re.IGNORECASE),
    ),
    (
        "not_found",
        re.compile(r"not[ _]found|\b404\b", re.IGNORECASE),
    ),
    (
        "invalid_argument",
        re.compile(r"invalid|malformed|required|unsupported|not allowed", re.IGNORECASE),
    ),
)


@dataclasses.dataclass
class RunSummary:
    """Outcome of one decision agent run, used for events and HTTP status.

    Attributes:
        customer_id: Normalised customer ID the run processed.
        usecase: Target platform ('GoogleAds' or 'SA360').
        run_id: Identifier shared by every decision in the run. Empty when the
            run aborted before one was assigned.
        outcome: One of the OUTCOME_* constants.
        reason: Abort reason (ABORT_* constant) when outcome is 'aborted'.
        successful_playbooks: Playbook executions that completed.
        failed_playbooks: Playbook executions that raised.
        eligible_campaigns: Campaigns with at least one runnable playbook.
        duration_s: Wall-clock duration of the run in seconds.
    """

    customer_id: str
    usecase: str
    run_id: str = ""
    outcome: str = OUTCOME_SUCCESS
    reason: str = ""
    successful_playbooks: int = 0
    failed_playbooks: int = 0
    eligible_campaigns: int = 0
    duration_s: float = 0.0

    @property
    def is_failure(self) -> bool:
        """True when the run should be reported to the caller as failed."""
        return self.outcome in (OUTCOME_FAILED, OUTCOME_ABORTED)


def event_fields(event: str, **fields: Any) -> Dict[str, Any]:
    """Builds the ``extra`` dict for a structured monitoring event.

    Args:
        event: One of the EVENT_* constants.
        **fields: Additional labels. None values are dropped so they do not
            render as the string "None" and pollute metric labels.

    Returns:
        A dictionary suitable for ``logger.<level>(..., extra=...)``.
    """
    extra: Dict[str, Any] = {"event": event}
    for key, value in fields.items():
        if value is not None:
            extra[key] = value
    return extra


def classify_error(error: Any) -> str:
    """Maps an error message or exception to a small, fixed set of classes.

    Metric labels must be low cardinality, so raw error messages cannot be
    used directly. The class is what alerts key on (for example 'auth').

    Args:
        error: An exception, error string or None.

    Returns:
        One of 'auth', 'quota', 'timeout', 'unavailable', 'not_found',
        'invalid_argument' or 'other'.
    """
    if error is None:
        return "other"
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    for error_class, pattern in _ERROR_CLASS_PATTERNS:
        if pattern.search(text):
            return error_class
    return "other"


def dependency_for_tool(tool_name: str) -> str:
    """Infers the external dependency a tool talks to from its name.

    Args:
        tool_name: The ADK tool (function) name.

    Returns:
        One of 'google_ads', 'sa360', 'weather', 'firestore' or 'internal'.
    """
    name = (tool_name or "").lower()
    if "sa360" in name:
        return "sa360"
    if "google_ads" in name:
        return "google_ads"
    if "weather" in name:
        return "weather"
    if name in {
        "get_document",
        "query_collection",
        "set_document",
        "delete_document",
        "list_collections",
    } or "firestore" in name:
        return "firestore"
    return "internal"


def _response_error(tool_response: Any) -> Optional[str]:
    """Extracts the error message from a tool's result dict, if it failed.

    Tools report failures by returning a dict with an 'error' key (and
    usually 'success': False). Dry run results also set 'success': False but
    are not errors, so they are excluded.

    Args:
        tool_response: The value returned by the tool.

    Returns:
        The error message, or None when the call did not fail.
    """
    if not isinstance(tool_response, dict) or tool_response.get("dry_run"):
        return None
    error = tool_response.get("error")
    if error:
        return str(error)
    if tool_response.get("success") is False:
        return str(tool_response.get("message") or "success=False")
    return None


def make_agent_callbacks(run_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Creates ADK LlmAgent callbacks that emit tool and model events.

    Args:
        run_context: Labels attached to every event from this agent, such as
            customer_id, usecase, run_id, campaign_id and playbook_id.

    Returns:
        Keyword arguments for ``agents.LlmAgent``: after_tool_callback,
        on_tool_error_callback and on_model_error_callback.
    """
    context = {k: str(v) for k, v in (run_context or {}).items() if v is not None}

    def _labels(**overrides: Any) -> Dict[str, Any]:
        """Merges per-call labels over the agent's run context.

        A plain ``**context, campaign_id=...`` call would raise a duplicate
        keyword TypeError whenever the context already carries that label.
        """
        return {**context, **{k: v for k, v in overrides.items() if v is not None}}

    def _campaign_id(args: Optional[Dict[str, Any]]) -> Optional[str]:
        """Prefers the campaign the tool was called for over the run context."""
        value = (args or {}).get("campaign_id") or context.get("campaign_id")
        return str(value) if value else None

    def after_tool_callback(tool: Any, args: Dict[str, Any], tool_context: Any, tool_response: Any) -> None:
        """Emits mutation_applied or tool_error for each completed tool call."""
        del tool_context  # Unused; part of the ADK callback signature.
        try:
            tool_name = getattr(tool, "name", "") or ""
            dependency = dependency_for_tool(tool_name)
            campaign_id = _campaign_id(args)
            error = _response_error(tool_response)
            action = MUTATING_TOOL_ACTIONS.get(tool_name)

            if action and error is None:
                dry_run = bool(isinstance(tool_response, dict) and tool_response.get("dry_run"))
                logger.info(
                    "Audit: %s %s via %s on campaign %s (dry_run=%s)",
                    "suppressed" if dry_run else "applied",
                    action,
                    tool_name,
                    campaign_id or "(unknown)",
                    dry_run,
                    extra=event_fields(
                        EVENT_MUTATION_APPLIED,
                        **_labels(
                            tool=tool_name,
                            action=action,
                            dependency=dependency,
                            campaign_id=campaign_id,
                            dry_run=str(dry_run).lower(),
                            # 'args' is a reserved LogRecord attribute.
                            tool_args=args,
                        ),
                    ),
                )
            elif error is not None:
                error_class = classify_error(error)
                logger.error(
                    "Tool %s (%s) failed [%s]: %s",
                    tool_name,
                    dependency,
                    error_class,
                    error[:500],
                    extra=event_fields(
                        EVENT_TOOL_ERROR,
                        **_labels(
                            tool=tool_name,
                            dependency=dependency,
                            error_class=error_class,
                            mutating=str(action is not None).lower(),
                            campaign_id=campaign_id,
                        ),
                    ),
                )
        except Exception:  # pylint: disable=broad-except
            logger.warning("telemetry after_tool_callback failed", exc_info=True)
        return None

    def on_tool_error_callback(tool: Any, args: Dict[str, Any], tool_context: Any, error: Exception) -> None:
        """Emits tool_error for a tool that raised instead of returning."""
        del tool_context  # Unused; part of the ADK callback signature.
        try:
            tool_name = getattr(tool, "name", "") or ""
            error_class = classify_error(error)
            logger.error(
                "Tool %s raised [%s]: %s",
                tool_name,
                error_class,
                error,
                extra=event_fields(
                    EVENT_TOOL_ERROR,
                    **_labels(
                        tool=tool_name,
                        dependency=dependency_for_tool(tool_name),
                        error_class=error_class,
                        mutating=str(tool_name in MUTATING_TOOL_ACTIONS).lower(),
                        campaign_id=_campaign_id(args),
                    ),
                ),
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning("telemetry on_tool_error_callback failed", exc_info=True)
        return None  # Re-raise: the playbook's own error handling decides.

    def on_model_error_callback(callback_context: Any, llm_request: Any, error: Exception) -> None:
        """Emits model_error for Gemini failures (quota, availability, etc.)."""
        del callback_context, llm_request  # Unused; part of the ADK signature.
        try:
            error_class = classify_error(error)
            logger.error(
                "Gemini model call failed [%s]: %s",
                error_class,
                error,
                extra=event_fields(
                    EVENT_MODEL_ERROR,
                    **_labels(dependency="gemini", error_class=error_class),
                ),
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning("telemetry on_model_error_callback failed", exc_info=True)
        return None  # Re-raise: the playbook's own error handling decides.

    return {
        "after_tool_callback": after_tool_callback,
        "on_tool_error_callback": on_tool_error_callback,
        "on_model_error_callback": on_model_error_callback,
    }
