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
"""Enforced dry run for advertising platform mutations.

Instructing the model not to act is not a control. Asked to operate in
"dry run mode", the model narrated its intent ("I would pause the following
asset groups") and then issued the pause calls anyway, finally reporting
"DRY RUN - NO ACTUAL CHANGES MADE" after two live asset groups had already
been paused. The instruction shaped the prose, not the behaviour.

This module moves the control into code. Each mutating tool consults
``is_dry_run`` immediately before issuing its write and, when enabled,
returns ``dry_run_response`` instead. The check sits at the write itself
rather than at the top of the tool so that everything preceding it -- ID
resolution, current state, validation -- still runs, and the recorded
"would have" payload therefore describes a real, fully resolved change.

Scope is deliberately limited to advertising platform writes: Google Ads
mutations and SA360 bulk sheet edits. Firestore is ADSTA's own state store
and shadow-mode operation needs it, so it is not suppressed. This mirrors
LOG_ONLY in the Apps Script this service replaces, which likewise blocks
account changes while still advancing its state sheet.

Dry run has two sources, and either one suppresses a write:

* the deployment: ``ADSTA_DRY_RUN`` covers every account and campaign;
* the campaign: the decision agent wraps one campaign's playbooks in
  ``campaign_dry_run`` when its config entry has ``dryRun: true`` (the
  config sheet's "Dry run" column).

The campaign flag lives in a context variable rather than in global state
because campaigns run in parallel worker threads. Each thread has its own
context, ``asyncio.run`` copies it into the playbook's event loop, and ADK
copies it again into the thread that runs each synchronous tool, so the flag
reaches exactly that campaign's tool calls and no other campaign's.
"""

import contextlib
import contextvars
import logging
import os
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# Set to a truthy value to suppress every advertising platform write.
DRY_RUN_ENV_VAR = "ADSTA_DRY_RUN"

# Values of dry_run_source().
SOURCE_DEPLOYMENT = "deployment"
SOURCE_CAMPAIGN = "campaign"

_TRUTHY_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})

# True while the current campaign runs in dry run. Set only through
# campaign_dry_run(), which always restores the previous value.
_CAMPAIGN_DRY_RUN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "adsta_campaign_dry_run", default=False
)


def parse_flag(value: Any) -> bool:
    """Interprets a dry run flag read from configuration.

    The config sheet sync stores a real boolean, but a hand-edited Firestore
    document may hold a string or a number. Unrecognised values count as
    False, matching how ADSTA_DRY_RUN is parsed.

    Args:
        value: The stored value, for example True, "true", "Y" or 1.

    Returns:
        True if the value means "dry run".
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_VALUES
    return False


def deployment_dry_run() -> bool:
    """Reports whether ADSTA_DRY_RUN puts the whole deployment in dry run.

    Read at call time rather than import time so that tests, and any future
    per-request override, can change the setting without reloading modules.

    Returns:
        True if the dry run environment variable holds a truthy value.
    """
    return os.environ.get(DRY_RUN_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES


def dry_run_source() -> Optional[str]:
    """Names what put the current call in dry run, if anything.

    Returns:
        SOURCE_DEPLOYMENT when ADSTA_DRY_RUN is set (it takes precedence),
        SOURCE_CAMPAIGN inside a campaign_dry_run(True) scope, else None.
    """
    if deployment_dry_run():
        return SOURCE_DEPLOYMENT
    if _CAMPAIGN_DRY_RUN.get():
        return SOURCE_CAMPAIGN
    return None


def is_dry_run() -> bool:
    """Reports whether advertising platform writes must be suppressed now.

    This is the single check every mutating tool makes. It is True when the
    deployment is in dry run (ADSTA_DRY_RUN) or when the call belongs to a
    campaign running in dry run (see campaign_dry_run).

    Returns:
        True if the current write must not reach the platform.
    """
    return dry_run_source() is not None


@contextlib.contextmanager
def campaign_dry_run(enabled: bool) -> Iterator[None]:
    """Scopes the campaign-level dry run flag to a block of code.

    The previous value is restored on exit, even if the block raises. That
    matters because worker threads are reused: a flag left set would carry
    over to the next campaign that thread processes.

    Args:
        enabled: Whether the campaign runs in dry run. False still opens a
            scope, which shields the block from any value set further out.

    Yields:
        None.
    """
    token = _CAMPAIGN_DRY_RUN.set(bool(enabled))
    try:
        yield
    finally:
        _CAMPAIGN_DRY_RUN.reset(token)


def dry_run_response(action: str, details: Dict[str, Any]) -> Dict[str, Any]:
    """Builds the standard response for a suppressed mutation.

    The payload reports ``success`` as False. A suppressed write did not
    happen, and saying otherwise would recreate precisely the ambiguity this
    module exists to remove: a caller, human or model, must not be able to
    read the result as confirmation that the account changed. The message
    tells the model not to retry, since a retry cannot succeed either.

    Args:
        action: The name of the suppressed operation, for the audit trail.
        details: The fully resolved change that would have been applied.

    Returns:
        A result dictionary describing the suppressed mutation.
    """
    source = dry_run_source() or SOURCE_DEPLOYMENT
    logger.warning(
        "DRY RUN (%s): suppressed %s",
        source,
        action,
        extra={
            "dry_run": True,
            "dry_run_source": source,
            "action_suppressed": action,
            "would_have": details,
        },
    )
    return {
        "success": False,
        "dry_run": True,
        "dry_run_source": source,
        "action_suppressed": action,
        "message": (
            f"DRY RUN: '{action}' was NOT applied. No change was made to the "
            "account. Do not retry and do not attempt an alternative tool; "
            "record this as the action you intended to take and continue."
        ),
        "would_have": details,
    }
