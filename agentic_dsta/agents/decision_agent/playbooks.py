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
"""Playbook resolution for the decision agent.

A playbook is a city-agnostic instruction template stored once in the Firestore
``Playbooks`` collection. Each campaign references one or more playbooks by id
and supplies only the values that differ between cities (``params``). This
module turns that pair into the concrete instruction text handed to an agent.

The motivation is operational rather than aesthetic. Previously each campaign
carried a full copy of its instruction prose with the latitude, longitude and
campaign name written into it, so adding a city meant duplicating ~2,000 words
of logic that then drifted independently. With 38 mapped locations across three
accounts that does not scale, and the copies had already diverged.

Placeholders use ``{{token}}`` rather than ``str.format`` braces because the
instruction prose legitimately contains single braces and percent signs, and a
formatting error in a prompt is not something we want surfacing at runtime.

Resolution is deliberately strict: an unresolved placeholder raises rather than
passing through. A prompt containing the literal text ``{{city}}`` would
otherwise instruct the agent to look up a Firestore document by that name, and
the failure would be silent and late.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
import zoneinfo

logger = logging.getLogger(__name__)

# Matches {{token}} and {{ token }}, where token may be dotted for nested
# params (e.g. {{severeModifiers.budgetBumpPct}}).
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

# Context keys the runner owns. A campaign's params may not shadow these: the
# identity of the campaign being acted on must come from the runner, not from
# a config field that could disagree with the document it was read from.
_RESERVED_CONTEXT_KEYS = frozenset({
    "customerId",
    "campaignId",
    "account",
    "runId",
    "playbookId",
    "budgetStateDocId",
    "assetGroupStateDocId",
})

# Northern-hemisphere meteorological seasons. Every market in scope (US,
# Canada, Europe) is northern; `params.hemisphere` overrides for future markets.
_NORTHERN_SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}

_SOUTHERN_SEASONS = {
    12: "summer", 1: "summer", 2: "summer",
    3: "autumn", 4: "autumn", 5: "autumn",
    6: "winter", 7: "winter", 8: "winter",
    9: "spring", 10: "spring", 11: "spring",
}


class PlaybookResolutionError(Exception):
    """Raised when a playbook cannot be rendered into a usable instruction.

    Callers are expected to skip the affected playbook and continue, rather
    than abort the run. A campaign missing its Severe Modifiers params should
    still get its asset groups toggled, per section 7 of the Arc'teryx spec.
    """


def season_for_month(month: int, hemisphere: str = "northern") -> str:
    """Returns the meteorological season for a calendar month.

    Args:
        month: Calendar month number, 1 to 12.
        hemisphere: Either "northern" or "southern". Defaults to northern.

    Returns:
        One of "winter", "spring", "summer" or "autumn".

    Raises:
        ValueError: If month is outside 1-12.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid month: {month!r}. Expected 1 to 12.")
    table = _SOUTHERN_SEASONS if hemisphere == "southern" else _NORTHERN_SEASONS
    return table[month]


def flatten_params(params: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flattens a nested params dict into dot-notation keys.

    ``{"severeModifiers": {"budgetBumpPct": 50}}`` becomes
    ``{"severeModifiers.budgetBumpPct": 50}``, so a template can reference the
    leaf directly without the renderer needing to understand the structure.
    Intermediate dicts are retained under their own key as well, which lets a
    playbook test for the presence of a whole block.

    Args:
        params: The nested parameter mapping.
        prefix: Key prefix used during recursion. Callers pass nothing.

    Returns:
        A flat mapping of dotted key to value.
    """
    flat: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        dotted = f"{prefix}{key}"
        flat[dotted] = value
        if isinstance(value, dict):
            flat.update(flatten_params(value, prefix=f"{dotted}."))
    return flat


def _format_value(value: Any) -> str:
    """Renders a param value as prompt text.

    Args:
        value: The value to render.

    Returns:
        A string suitable for substitution into instruction prose.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def find_placeholders(template: str) -> List[str]:
    """Returns the distinct placeholder tokens used by a template.

    Args:
        template: The raw playbook template text.

    Returns:
        A sorted list of token names, without the surrounding braces.
    """
    return sorted({match.group(1) for match in _PLACEHOLDER_PATTERN.finditer(template or "")})


def render_template(template: str, values: Dict[str, Any]) -> str:
    """Substitutes ``{{token}}`` placeholders in a template.

    Args:
        template: The raw playbook template text.
        values: Flat mapping of token name to value.

    Returns:
        The rendered instruction text.

    Raises:
        PlaybookResolutionError: If any placeholder has no corresponding value.
            Rendering is all-or-nothing so a partially substituted prompt can
            never reach the model.
    """
    missing = [token for token in find_placeholders(template) if token not in values]
    if missing:
        raise PlaybookResolutionError(
            f"Unresolved placeholder(s): {', '.join(missing)}"
        )

    def _substitute(match: re.Match) -> str:
        return _format_value(values[match.group(1)])

    return _PLACEHOLDER_PATTERN.sub(_substitute, template)


def build_context(
    customer_id: str,
    campaign_id: Any,
    playbook_id: str,
    run_id: str,
    account: str,
    timezone_name: Optional[str] = None,
    now_utc: Optional[datetime.datetime] = None,
    hemisphere: str = "northern",
) -> Dict[str, Any]:
    """Builds the runner-owned context values available to every playbook.

    These are the values a playbook must never have to hardcode: who is being
    acted on, when, and where the state and log documents live.

    Args:
        customer_id: The normalised Google Ads customer ID.
        campaign_id: The campaign ID being processed.
        playbook_id: The playbook being rendered.
        run_id: Identifier shared by every decision in this run, so the change
            log can be reassembled afterwards.
        account: Account label used in the change log ("US", "Canada", "Europe").
        timezone_name: IANA timezone for the account, used to derive the local
            timestamp the change log requires alongside UTC.
        now_utc: Override for the current time. Injected by tests.
        hemisphere: Passed through to season derivation.

    Returns:
        A flat mapping of context token to value.
    """
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)

    local_now = now
    resolved_timezone = timezone_name or "UTC"
    if timezone_name:
        try:
            local_now = now.astimezone(zoneinfo.ZoneInfo(timezone_name))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            # A bad timezone string must not take the run down; the change log
            # simply records UTC twice and the warning explains why.
            logger.warning(
                "Unknown timezone %r; local timestamps will fall back to UTC.",
                timezone_name,
                extra={"customer_id": str(customer_id), "campaign_id": str(campaign_id)},
            )
            resolved_timezone = "UTC"

    return {
        "customerId": str(customer_id),
        "campaignId": str(campaign_id),
        "playbookId": playbook_id,
        "runId": run_id,
        "account": account,
        "timezone": resolved_timezone,
        "today": now.strftime("%Y-%m-%d"),
        "todayLocal": local_now.strftime("%Y-%m-%d"),
        "nowIsoUtc": now.isoformat(),
        "nowIsoLocal": local_now.isoformat(),
        "currentYear": now.year,
        "currentMonth": now.month,
        "season": season_for_month(now.month, hemisphere=hemisphere),
        "budgetStateDocId": f"{customer_id}_{campaign_id}",
        "assetGroupStateDocId": f"{customer_id}_{campaign_id}",
    }


def resolve_playbook_ids(campaign: Dict[str, Any]) -> List[str]:
    """Returns the playbook ids a campaign should run, in order.

    Accepts either ``playbooks`` (list) or ``playbook`` (single string) so that
    the common one-playbook case stays terse in config.

    Args:
        campaign: A single entry from the config's ``campaigns`` list.

    Returns:
        A list of playbook ids, possibly empty.
    """
    declared = campaign.get("playbooks")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [str(item) for item in declared if item]

    single = campaign.get("playbook")
    if isinstance(single, str) and single:
        return [single]
    return []


def render_campaign_playbook(
    playbook_id: str,
    playbook: Dict[str, Any],
    campaign: Dict[str, Any],
    context: Dict[str, Any],
    shared_values: Optional[Dict[str, Any]] = None,
) -> str:
    """Renders one playbook for one campaign.

    Precedence, lowest to highest: playbook ``defaults``, shared values (e.g.
    the account's asset group tokens and windows), campaign ``params``, runner context.
    Context wins outright so a config cannot misreport the campaign it is
    acting on.

    Args:
        playbook_id: The playbook's document id, used in error messages.
        playbook: The playbook document data, containing at least ``template``.
        campaign: The campaign config entry supplying ``params``.
        context: Runner-owned values from :func:`build_context`.
        shared_values: Account-wide or global values available to all playbooks.

    Returns:
        The rendered instruction text.

    Raises:
        PlaybookResolutionError: If the playbook has no template, if a declared
            required param is absent, or if any placeholder is unresolved.
    """
    template = playbook.get("template")
    if not template or not str(template).strip():
        raise PlaybookResolutionError(f"Playbook '{playbook_id}' has no 'template' field.")

    params = campaign.get("params") or {}
    flat_params = flatten_params(params)

    required: Iterable[str] = playbook.get("requiredParams") or []
    absent = [name for name in required if flat_params.get(name) is None]
    if absent:
        # This is the section 7 path: "No Severe Modifiers row for a campaign →
        # asset group toggling runs as normal. No budget increase is applied."
        # Skipping the playbook is the correct behaviour, not an error.
        raise PlaybookResolutionError(
            f"Playbook '{playbook_id}' requires param(s) {', '.join(absent)}, "
            "which the campaign does not define."
        )

    shadowed = _RESERVED_CONTEXT_KEYS.intersection(flat_params)
    if shadowed:
        logger.warning(
            "Campaign params attempt to shadow reserved context key(s) %s; "
            "the runner's values take precedence.",
            ", ".join(sorted(shadowed)),
            extra={
                "customer_id": context.get("customerId"),
                "campaign_id": context.get("campaignId"),
                "playbook_id": playbook_id,
            },
        )

    values: Dict[str, Any] = {}
    values.update(flatten_params(playbook.get("defaults") or {}))
    values.update(flatten_params(shared_values or {}))
    values.update(flat_params)
    values.update(context)

    return render_template(str(template), values)


def resolve_campaign_instructions(
    campaign: Dict[str, Any],
    playbook_library: Dict[str, Dict[str, Any]],
    context_builder,
    shared_values: Optional[Dict[str, Any]] = None,
) -> List[Tuple[str, str]]:
    """Resolves every playbook for a campaign into runnable instructions.

    A campaign that declares no playbooks but carries a literal ``instruction``
    string is still supported, so existing configs keep working while they are
    migrated.

    Args:
        campaign: The campaign config entry.
        playbook_library: Mapping of playbook id to playbook document data.
        context_builder: Callable taking a playbook id and returning the
            context mapping for it.
        shared_values: Values available to all playbooks.

    Returns:
        A list of (playbook_id, instruction) pairs. Playbooks that cannot be
        resolved are omitted, having been logged.
    """
    resolved: List[Tuple[str, str]] = []
    playbook_ids = resolve_playbook_ids(campaign)
    campaign_id = campaign.get("campaignId")

    if not playbook_ids:
        legacy = campaign.get("instruction")
        if legacy and str(legacy).strip():
            logger.info(
                "Campaign %s uses a legacy inline 'instruction'. Migrate it to a "
                "playbook reference so the logic is shared across cities.",
                campaign_id,
                extra={"campaign_id": str(campaign_id)},
            )
            return [("inline", str(legacy))]
        logger.warning(
            "Campaign %s declares neither 'playbooks' nor 'instruction'; nothing to run.",
            campaign_id,
            extra={"campaign_id": str(campaign_id)},
        )
        return []

    for playbook_id in playbook_ids:
        playbook = playbook_library.get(playbook_id)
        if not playbook:
            logger.error(
                "Campaign %s references unknown playbook '%s'. Skipping it.",
                campaign_id,
                playbook_id,
                extra={"campaign_id": str(campaign_id), "playbook_id": playbook_id},
            )
            continue

        try:
            instruction = render_campaign_playbook(
                playbook_id=playbook_id,
                playbook=playbook,
                campaign=campaign,
                context=context_builder(playbook_id),
                shared_values=shared_values,
            )
        except PlaybookResolutionError as err:
            logger.warning(
                "Skipping playbook '%s' for campaign %s: %s",
                playbook_id,
                campaign_id,
                err,
                extra={"campaign_id": str(campaign_id), "playbook_id": playbook_id},
            )
            continue

        resolved.append((playbook_id, instruction))

    return resolved
