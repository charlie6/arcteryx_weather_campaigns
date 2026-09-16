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
"""

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Set to a truthy value to suppress every advertising platform write.
DRY_RUN_ENV_VAR = "ADSTA_DRY_RUN"

_TRUTHY_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})


def is_dry_run() -> bool:
    """Reports whether dry run mode is currently enabled.

    Read at call time rather than import time so that tests, and any future
    per-request override, can change the setting without reloading modules.

    Returns:
        True if the dry run environment variable holds a truthy value.
    """
    return os.environ.get(DRY_RUN_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES


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
    logger.warning(
        "DRY RUN: suppressed %s",
        action,
        extra={"dry_run": True, "action_suppressed": action, "would_have": details},
    )
    return {
        "success": False,
        "dry_run": True,
        "action_suppressed": action,
        "message": (
            f"DRY RUN: '{action}' was NOT applied. No change was made to the "
            "account. Do not retry and do not attempt an alternative tool; "
            "record this as the action you intended to take and continue."
        ),
        "would_have": details,
    }
