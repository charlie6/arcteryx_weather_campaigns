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
    error_details = [
        f"{error.message} (Code: {error.error_code})"
        for error in ex.failure.errors
    ]
    error_msg = "; ".join(error_details)
    logger.error(
        "Failed to look up asset group: %s",
        error_msg,
        exc_info=True,
        extra={"customer_id": customer_id, "asset_group_id": asset_group_id},
    )
    return {
        "success": False,
        "error": f"Failed to look up asset group: {error_msg}",
        "error_details": error_details,
    }

  if existing is None:
    logger.warning(
        "Asset group not found",
        extra={"customer_id": customer_id, "asset_group_id": asset_group_id},
    )
    return {
        "success": False,
        "error": f"Asset group {asset_group_id} not found.",
    }

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
        "asset_group_name": asset_group_name,
        "previous_status": previous_status,
        "new_status": normalized_status,
    }

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
        "asset_group_name": asset_group_name,
        "campaign_id": existing["campaign_id"],
        "previous_status": previous_status,
        "new_status": normalized_status,
    }
  except GoogleAdsException as ex:
    error_details = [
        f"{error.message} (Code: {error.error_code})"
        for error in ex.failure.errors
    ]
    error_msg = "; ".join(error_details)
    logger.error(
        "Failed to update asset group status: %s",
        error_msg,
        exc_info=True,
        extra={
            "customer_id": customer_id,
            "asset_group_id": asset_group_id,
            "status": normalized_status,
        },
    )
    return {
        "success": False,
        "error": f"Failed to update asset group status: {error_msg}",
        "error_details": error_details,
    }


class GoogleAdsAssetGroupToolset(BaseToolset):
  """Toolset for managing Performance Max asset groups."""

  def __init__(self):
    super().__init__()
    self._list_asset_groups_tool = FunctionTool(
        func=list_google_ads_asset_groups,
    )
    self._update_asset_group_status_tool = FunctionTool(
        func=update_google_ads_asset_group_status,
    )

  async def get_tools(
      self, readonly_context: Optional[Any] = None
  ) -> List[FunctionTool]:
    """Returns a list of tools in this toolset."""
    return [
        self._list_asset_groups_tool,
        self._update_asset_group_status_tool,
    ]
