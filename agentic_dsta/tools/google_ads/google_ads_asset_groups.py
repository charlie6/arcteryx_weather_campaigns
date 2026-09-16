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
"""Tools for managing Performance Max asset groups in the Google Ads API.

Performance Max campaigns contain asset groups rather than ad groups, so these
tools use ``AssetGroupService`` and are unrelated to the ad group tools in
``google_ads_updater``.

Weather-triggered campaigns keep an always-on asset group that must remain
enabled at all times. Because a mistaken pause silently removes live coverage,
pausing is gated behind two configurable guards:

``ADSTA_ALWAYS_ON_ASSET_GROUP_PATTERNS``
    Comma-separated, case-insensitive substrings marking an asset group as
    always-on. Matching groups can never be paused.
    Default: ``ALWAYS_ON,ALWAYSON,ALWAYS-ON,_AO_``

``ADSTA_ASSET_GROUP_MANAGED_PATTERN``
    Case-insensitive substring that a name must contain before it may be
    paused. Acts as an allow-list so unrelated asset groups are never touched.
    Set to an empty string to disable. Default: ``_WEATHER_``

Enabling an asset group is never blocked, since that cannot violate the
always-on requirement.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

from agentic_dsta.core.dry_run import dry_run_response, is_dry_run
from agentic_dsta.tools.google_ads.google_ads_client import get_google_ads_client

logger = logging.getLogger(__name__)

_DEFAULT_ALWAYS_ON_PATTERNS = "ALWAYS_ON,ALWAYSON,ALWAYS-ON,_AO_"
_DEFAULT_MANAGED_PATTERN = "_WEATHER_"

VALID_STATUSES = ("ENABLED", "PAUSED")


def _always_on_patterns() -> List[str]:
  """Returns the configured always-on marker substrings, upper-cased."""
  raw = os.environ.get(
      "ADSTA_ALWAYS_ON_ASSET_GROUP_PATTERNS", _DEFAULT_ALWAYS_ON_PATTERNS
  )
  return [p.strip().upper() for p in raw.split(",") if p.strip()]


def _managed_pattern() -> str:
  """Returns the configured managed-asset-group substring, upper-cased."""
  return os.environ.get(
      "ADSTA_ASSET_GROUP_MANAGED_PATTERN", _DEFAULT_MANAGED_PATTERN
  ).strip().upper()


def _escape_gaql_string_literal(value: str) -> str:
  """Escapes a value for safe use inside a single-quoted GAQL literal.

  Campaign and asset group names are operator-supplied configuration, so
  backslashes and quotes are escaped to keep the query well-formed.

  Args:
      value: The raw string to embed in a query.

  Returns:
      The escaped string, without surrounding quotes.
  """
  return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def is_always_on_asset_group(asset_group_name: str) -> bool:
  """Reports whether an asset group name marks it as always-on.

  Args:
      asset_group_name: The asset group name to test.

  Returns:
      True if the name matches any configured always-on pattern.
  """
  name = (asset_group_name or "").upper()
  return any(pattern in name for pattern in _always_on_patterns())


def _check_pause_allowed(asset_group_name: str) -> Tuple[bool, Optional[str]]:
  """Determines whether an asset group may be paused.

  Args:
      asset_group_name: The asset group name to test.

  Returns:
      A tuple of (allowed, reason). When allowed is False, reason explains
      which guard rejected the request.
  """
  if is_always_on_asset_group(asset_group_name):
    return False, (
        f"Asset group '{asset_group_name}' is an always-on group and must "
        "never be paused. Adjust ADSTA_ALWAYS_ON_ASSET_GROUP_PATTERNS if this "
        "classification is wrong."
    )

  managed = _managed_pattern()
  if managed and managed not in (asset_group_name or "").upper():
    return False, (
        f"Asset group '{asset_group_name}' is not a managed weather asset "
        f"group (name does not contain '{managed}'), so it will not be "
        "paused. Adjust ADSTA_ASSET_GROUP_MANAGED_PATTERN to change this."
    )

  return True, None


def _fetch_asset_group(
    client: Any, customer_id: str, asset_group_id: str
) -> Optional[Dict[str, Any]]:
  """Fetches a single asset group's identifying fields.

  Args:
      client: An initialized GoogleAdsClient.
      customer_id: The Google Ads customer ID (without hyphens).
      asset_group_id: The ID of the asset group.

  Returns:
      A dictionary of asset group fields, or None if it does not exist.
  """
  ga_service = client.get_service("GoogleAdsService")
  query = f"""
      SELECT
        asset_group.id,
        asset_group.name,
        asset_group.status,
        campaign.id,
        campaign.name
      FROM asset_group
      WHERE asset_group.id = {asset_group_id}
      LIMIT 1"""

  for row in ga_service.search(customer_id=customer_id, query=query):
    return {
        "asset_group_id": str(row.asset_group.id),
        "asset_group_name": row.asset_group.name,
        "status": row.asset_group.status.name,
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
    }
  return None


def _fetch_asset_groups_by_name(
    client: Any,
    customer_id: str,
    campaign_name: str,
    asset_group_name: str,
) -> List[Dict[str, Any]]:
  """Finds asset groups matching an exact campaign and asset group name.

  Args:
      client: An initialized GoogleAdsClient.
      customer_id: The Google Ads customer ID (without hyphens).
      campaign_name: The exact campaign name.
      asset_group_name: The exact asset group name.

  Returns:
      A list of matching asset group records. Empty when nothing matches.
      More than one entry means the pair is ambiguous.
  """
  ga_service = client.get_service("GoogleAdsService")
  query = f"""
      SELECT
        asset_group.id,
        asset_group.name,
        asset_group.status,
        campaign.id,
        campaign.name
      FROM asset_group
      WHERE campaign.name = '{_escape_gaql_string_literal(campaign_name)}'
        AND asset_group.name = '{_escape_gaql_string_literal(asset_group_name)}'"""

  matches: List[Dict[str, Any]] = []
  for row in ga_service.search(customer_id=customer_id, query=query):
    matches.append({
        "asset_group_id": str(row.asset_group.id),
        "asset_group_name": row.asset_group.name,
        "status": row.asset_group.status.name,
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
    })
  return matches


def _google_ads_error(
    ex: GoogleAdsException, prefix: str, context: Dict[str, Any]
) -> Dict[str, Any]:
  """Formats a GoogleAdsException into the standard error response.

  Args:
      ex: The raised exception.
      prefix: Human-readable description of the failed operation.
      context: Structured fields to attach to the log record.

  Returns:
      An error response dictionary.
  """
  error_details = [
      f"{error.message} (Code: {error.error_code})"
      for error in ex.failure.errors
  ]
  error_msg = "; ".join(error_details)
  logger.error("%s: %s", prefix, error_msg, exc_info=True, extra=context)
  return {
      "success": False,
      "error": f"{prefix}: {error_msg}",
      "error_details": error_details,
  }


def _perform_status_update(
    client: Any,
    customer_id: str,
    existing: Dict[str, Any],
    normalized_status: str,
) -> Dict[str, Any]:
  """Applies a status change to an already-resolved asset group.

  This is the single place where the always-on guard is enforced and the
  mutation is issued, so every entry point shares identical protection.

  Args:
      client: An initialized GoogleAdsClient.
      customer_id: The Google Ads customer ID (without hyphens).
      existing: The resolved asset group record.
      normalized_status: Either "ENABLED" or "PAUSED", already validated.

  Returns:
      A result dictionary describing the change, skip, or rejection.
  """
  asset_group_id = existing["asset_group_id"]
  asset_group_name = existing["asset_group_name"]
  previous_status = existing["status"]

  # Guard: never pause an always-on or unmanaged asset group.
  if normalized_status == "PAUSED":
    allowed, reason = _check_pause_allowed(asset_group_name)
    if not allowed:
      logger.warning(
          "Refused to pause protected asset group: %s",
          reason,
          extra={
              "customer_id": customer_id,
              "asset_group_id": asset_group_id,
              "asset_group_name": asset_group_name,
              "campaign_id": existing["campaign_id"],
          },
      )
      return {
          "success": False,
          "blocked_by_guard": True,
          "error": reason,
          "asset_group_id": asset_group_id,
          "asset_group_name": asset_group_name,
          "current_status": previous_status,
      }

  if previous_status == normalized_status:
    logger.info(
        "Asset group already %s; no change made",
        normalized_status,
        extra={
            "customer_id": customer_id,
            "asset_group_id": asset_group_id,
            "asset_group_name": asset_group_name,
        },
    )
    return {
        "success": True,
        "skipped": True,
        "asset_group_id": asset_group_id,
        "asset_group_name": asset_group_name,
        "campaign_id": existing["campaign_id"],
        "previous_status": previous_status,
        "new_status": normalized_status,
    }

  if is_dry_run():
    return dry_run_response(
        "update_asset_group_status",
        {
            "customer_id": customer_id,
            "asset_group_id": asset_group_id,
            "asset_group_name": asset_group_name,
            "campaign_id": existing["campaign_id"],
            "previous_status": previous_status,
            "new_status": normalized_status,
        },
    )

  asset_group_service = client.get_service("AssetGroupService")
  asset_group_op = client.get_type("AssetGroupOperation")
  asset_group = asset_group_op.update
  asset_group.resource_name = asset_group_service.asset_group_path(
      customer_id, asset_group_id
  )

  AssetGroupStatusEnum = client.get_type("AssetGroupStatusEnum")
  if normalized_status == "ENABLED":
    asset_group.status = AssetGroupStatusEnum.AssetGroupStatus.ENABLED
  else:
    asset_group.status = AssetGroupStatusEnum.AssetGroupStatus.PAUSED

  client.copy_from(
      asset_group_op.update_mask, field_mask_pb2.FieldMask(paths=["status"])
  )

  try:
    response = asset_group_service.mutate_asset_groups(
        customer_id=customer_id, operations=[asset_group_op]
    )
    resource_name = response.results[0].resource_name
    logger.info(
        "Updated asset group status from %s to %s",
        previous_status,
        normalized_status,
        extra={
            "customer_id": customer_id,
            "asset_group_id": asset_group_id,
            "asset_group_name": asset_group_name,
            "campaign_id": existing["campaign_id"],
            "previous_status": previous_status,
            "new_status": normalized_status,
            "resource_name": resource_name,
        },
    )
    return {
        "success": True,
        "skipped": False,
        "resource_name": resource_name,
        "asset_group_id": asset_group_id,
        "asset_group_name": asset_group_name,
        "campaign_id": existing["campaign_id"],
        "previous_status": previous_status,
        "new_status": normalized_status,
    }
  except GoogleAdsException as ex:
    return _google_ads_error(
        ex,
        "Failed to update asset group status",
        {
            "customer_id": customer_id,
            "asset_group_id": asset_group_id,
            "status": normalized_status,
        },
    )


def list_google_ads_asset_groups(
    customer_id: str, campaign_id: str
) -> Dict[str, Any]:
  """Lists the asset groups belonging to a Performance Max campaign.

  Use this tool to discover which asset groups exist in a campaign and their
  current serving status before deciding which to enable or pause. Each result
  is annotated with whether it is protected from pausing.

  Args:
      customer_id: The Google Ads customer ID (without hyphens).
      campaign_id: The ID of the Performance Max campaign.

  Returns:
      A dictionary with a boolean 'success' key. On success, 'asset_groups'
      holds a list of asset group records, each containing asset_group_id,
      asset_group_name, status, is_always_on and can_be_paused. On failure,
      'error' describes the problem.
  """
  client = get_google_ads_client(customer_id)
  if not client:
    return {"success": False, "error": "Failed to get Google Ads client."}

  ga_service = client.get_service("GoogleAdsService")
  query = f"""
      SELECT
        asset_group.id,
        asset_group.name,
        asset_group.status,
        campaign.id,
        campaign.name
      FROM asset_group
      WHERE campaign.id = {campaign_id}"""

  try:
    asset_groups: List[Dict[str, Any]] = []
    for row in ga_service.search(customer_id=customer_id, query=query):
      name = row.asset_group.name
      allowed, reason = _check_pause_allowed(name)
      asset_groups.append({
          "asset_group_id": str(row.asset_group.id),
          "asset_group_name": name,
          "status": row.asset_group.status.name,
          "campaign_id": str(row.campaign.id),
          "campaign_name": row.campaign.name,
          "is_always_on": is_always_on_asset_group(name),
          "can_be_paused": allowed,
          "pause_block_reason": reason,
      })

    logger.info(
        "Listed %d asset groups",
        len(asset_groups),
        extra={"customer_id": customer_id, "campaign_id": campaign_id},
    )
    return {
        "success": True,
        "campaign_id": str(campaign_id),
        "asset_group_count": len(asset_groups),
        "asset_groups": asset_groups,
    }
  except GoogleAdsException as ex:
    error_details = [
        f"{error.message} (Code: {error.error_code})"
        for error in ex.failure.errors
    ]
    error_msg = "; ".join(error_details)
    logger.error(
        "Failed to list asset groups: %s",
        error_msg,
        exc_info=True,
        extra={"customer_id": customer_id, "campaign_id": campaign_id},
    )
    return {
        "success": False,
        "error": f"Failed to list asset groups: {error_msg}",
        "error_details": error_details,
    }


def update_google_ads_asset_group_status(
    customer_id: str, asset_group_id: str, status: str
) -> Dict[str, Any]:
  """Enables or pauses a Performance Max asset group.

  Use this tool to activate a weather asset group when its condition is
  forecast, or pause it once the condition has passed. Always-on asset groups
  are protected and will not be paused; such a request is rejected and logged
  rather than executed.

  Args:
      customer_id: The Google Ads customer ID (without hyphens).
      asset_group_id: The ID of the asset group to update.
      status: The desired status, either "ENABLED" or "PAUSED".

  Returns:
      A dictionary with a boolean 'success' key. On success it includes the
      resource_name, asset_group_name, previous_status and new_status, plus a
      'skipped' flag when the group was already in the requested state. On
      failure, 'error' describes the problem and 'blocked_by_guard' is True if
      an always-on or managed-name guard rejected the request.
  """
  normalized_status = (status or "").strip().upper()
  if normalized_status not in VALID_STATUSES:
    return {
        "success": False,
        "error": (
            f"Invalid status '{status}'. Use one of: "
            f"{', '.join(VALID_STATUSES)}."
        ),
    }

  client = get_google_ads_client(customer_id)
  if not client:
    return {"success": False, "error": "Failed to get Google Ads client."}

  try:
    existing = _fetch_asset_group(client, customer_id, asset_group_id)
  except GoogleAdsException as ex:
    return _google_ads_error(
        ex,
        "Failed to look up asset group",
        {"customer_id": customer_id, "asset_group_id": asset_group_id},
    )

  if existing is None:
    logger.warning(
        "Asset group not found",
        extra={"customer_id": customer_id, "asset_group_id": asset_group_id},
    )
    return {
        "success": False,
        "error": f"Asset group {asset_group_id} not found.",
    }

  return _perform_status_update(
      client, customer_id, existing, normalized_status
  )


def find_google_ads_asset_group_by_name(
    customer_id: str, campaign_name: str, asset_group_name: str
) -> Dict[str, Any]:
  """Looks up a Performance Max asset group by campaign and asset group name.

  Use this tool when the configuration identifies asset groups by name rather
  than ID, to resolve the ID before acting. Names must match exactly.

  Args:
      customer_id: The Google Ads customer ID (without hyphens).
      campaign_name: The exact campaign name.
      asset_group_name: The exact asset group name.

  Returns:
      A dictionary with a boolean 'success' key. On success it contains
      asset_group_id, asset_group_name, campaign_id, status, is_always_on and
      can_be_paused. If the name pair matches more than one asset group,
      'success' is False, 'ambiguous' is True and 'matches' lists the
      candidates.
  """
  client = get_google_ads_client(customer_id)
  if not client:
    return {"success": False, "error": "Failed to get Google Ads client."}

  try:
    matches = _fetch_asset_groups_by_name(
        client, customer_id, campaign_name, asset_group_name
    )
  except GoogleAdsException as ex:
    return _google_ads_error(
        ex,
        "Failed to look up asset group by name",
        {
            "customer_id": customer_id,
            "campaign_name": campaign_name,
            "asset_group_name": asset_group_name,
        },
    )

  if not matches:
    logger.warning(
        "No asset group matched the given names",
        extra={
            "customer_id": customer_id,
            "campaign_name": campaign_name,
            "asset_group_name": asset_group_name,
        },
    )
    return {
        "success": False,
        "error": (
            f"No asset group named '{asset_group_name}' found in campaign "
            f"'{campaign_name}'."
        ),
    }

  if len(matches) > 1:
    # Refuse to guess: acting on the wrong asset group is not recoverable
    # from the agent's point of view.
    logger.error(
        "Ambiguous asset group name match (%d candidates)",
        len(matches),
        extra={
            "customer_id": customer_id,
            "campaign_name": campaign_name,
            "asset_group_name": asset_group_name,
        },
    )
    return {
        "success": False,
        "ambiguous": True,
        "error": (
            f"'{asset_group_name}' in campaign '{campaign_name}' matched "
            f"{len(matches)} asset groups. Resolve by ID instead."
        ),
        "matches": matches,
    }

  match = matches[0]
  allowed, reason = _check_pause_allowed(match["asset_group_name"])
  return {
      "success": True,
      **match,
      "is_always_on": is_always_on_asset_group(match["asset_group_name"]),
      "can_be_paused": allowed,
      "pause_block_reason": reason,
  }


def update_google_ads_asset_group_status_by_name(
    customer_id: str, campaign_name: str, asset_group_name: str, status: str
) -> Dict[str, Any]:
  """Enables or pauses a Performance Max asset group identified by name.

  Equivalent to update_google_ads_asset_group_status but resolves the asset
  group from its campaign and asset group name, for configurations that hold
  names rather than IDs. The same always-on protection applies. If the name
  pair is ambiguous, no change is made.

  Args:
      customer_id: The Google Ads customer ID (without hyphens).
      campaign_name: The exact campaign name.
      asset_group_name: The exact asset group name.
      status: The desired status, either "ENABLED" or "PAUSED".

  Returns:
      A dictionary with a boolean 'success' key, matching the shape returned
      by update_google_ads_asset_group_status.
  """
  normalized_status = (status or "").strip().upper()
  if normalized_status not in VALID_STATUSES:
    return {
        "success": False,
        "error": (
            f"Invalid status '{status}'. Use one of: "
            f"{', '.join(VALID_STATUSES)}."
        ),
    }

  client = get_google_ads_client(customer_id)
  if not client:
    return {"success": False, "error": "Failed to get Google Ads client."}

  try:
    matches = _fetch_asset_groups_by_name(
        client, customer_id, campaign_name, asset_group_name
    )
  except GoogleAdsException as ex:
    return _google_ads_error(
        ex,
        "Failed to look up asset group by name",
        {
            "customer_id": customer_id,
            "campaign_name": campaign_name,
            "asset_group_name": asset_group_name,
        },
    )

  if not matches:
    logger.warning(
        "No asset group matched the given names; no change made",
        extra={
            "customer_id": customer_id,
            "campaign_name": campaign_name,
            "asset_group_name": asset_group_name,
        },
    )
    return {
        "success": False,
        "error": (
            f"No asset group named '{asset_group_name}' found in campaign "
            f"'{campaign_name}'."
        ),
    }

  if len(matches) > 1:
    logger.error(
        "Ambiguous asset group name match (%d candidates); no change made",
        len(matches),
        extra={
            "customer_id": customer_id,
            "campaign_name": campaign_name,
            "asset_group_name": asset_group_name,
        },
    )
    return {
        "success": False,
        "ambiguous": True,
        "error": (
            f"'{asset_group_name}' in campaign '{campaign_name}' matched "
            f"{len(matches)} asset groups. No change made; resolve by ID."
        ),
        "matches": matches,
    }

  return _perform_status_update(
      client, customer_id, matches[0], normalized_status
  )


class GoogleAdsAssetGroupToolset(BaseToolset):
  """Toolset for managing Performance Max asset groups."""

  def __init__(self):
    super().__init__()
    self._list_asset_groups_tool = FunctionTool(
        func=list_google_ads_asset_groups,
    )
    self._find_asset_group_by_name_tool = FunctionTool(
        func=find_google_ads_asset_group_by_name,
    )
    self._update_asset_group_status_tool = FunctionTool(
        func=update_google_ads_asset_group_status,
    )
    self._update_asset_group_status_by_name_tool = FunctionTool(
        func=update_google_ads_asset_group_status_by_name,
    )

  async def get_tools(
      self, readonly_context: Optional[Any] = None
  ) -> List[FunctionTool]:
    """Returns a list of tools in this toolset."""
    return [
        self._list_asset_groups_tool,
        self._find_asset_group_by_name_tool,
        self._update_asset_group_status_tool,
        self._update_asset_group_status_by_name_tool,
    ]
