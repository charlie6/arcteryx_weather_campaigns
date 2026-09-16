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
"""Tests for the Performance Max asset group toolset."""

import asyncio
import unittest
from unittest import mock

from agentic_dsta.tools.google_ads import google_ads_asset_groups


MagicMock = mock.MagicMock
patch = mock.patch

# Representative names taken from the Arc'teryx configuration workbook.
COLD_GROUP = "ARC_Weather_Cold_VAN_ENG_Neutral_Null_FW25"
SNOW_GROUP = "ARC_Weather_Snow_VAN_ENG_Neutral_Null_FW25"
ALWAYS_ON_GROUP = "ARC_Always_On_VAN_ENG_Neutral_Null_FW25"


def _mock_row(asset_group_id, name, status_name, campaign_id="111"):
  """Builds a mock GoogleAdsRow for an asset group query."""
  row = MagicMock()
  row.asset_group.id = asset_group_id
  row.asset_group.name = name
  row.asset_group.status.name = status_name
  row.campaign.id = campaign_id
  row.campaign.name = "ARC_Performance_Weather_PMax_CAN_CA_ENG_VAN_Brand_AO"
  return row


class TestAlwaysOnDetection(unittest.TestCase):
  """The always-on guard is the safety-critical part of this toolset."""

  def test_weather_groups_are_not_always_on(self):
    self.assertFalse(
        google_ads_asset_groups.is_always_on_asset_group(COLD_GROUP)
    )
    self.assertFalse(
        google_ads_asset_groups.is_always_on_asset_group(SNOW_GROUP)
    )

  def test_always_on_group_is_detected(self):
    self.assertTrue(
        google_ads_asset_groups.is_always_on_asset_group(ALWAYS_ON_GROUP)
    )

  def test_detection_is_case_insensitive(self):
    self.assertTrue(
        google_ads_asset_groups.is_always_on_asset_group("arc_always_on_van")
    )

  def test_alternate_always_on_spellings(self):
    for name in ("ARC_AlwaysOn_VAN", "ARC-ALWAYS-ON-VAN", "ARC_AO_VAN"):
      with self.subTest(name=name):
        self.assertTrue(
            google_ads_asset_groups.is_always_on_asset_group(name)
        )

  def test_empty_name_is_not_always_on(self):
    self.assertFalse(google_ads_asset_groups.is_always_on_asset_group(""))

  @patch.dict(
      "os.environ",
      {"ADSTA_ALWAYS_ON_ASSET_GROUP_PATTERNS": "PERMANENT"},
      clear=False,
  )
  def test_patterns_are_configurable(self):
    self.assertTrue(
        google_ads_asset_groups.is_always_on_asset_group("ARC_Permanent_VAN")
    )
    # With the default overridden, the stock marker no longer matches.
    self.assertFalse(
        google_ads_asset_groups.is_always_on_asset_group(ALWAYS_ON_GROUP)
    )


class TestPauseGuard(unittest.TestCase):

  def test_managed_weather_group_may_be_paused(self):
    allowed, reason = google_ads_asset_groups._check_pause_allowed(COLD_GROUP)
    self.assertTrue(allowed)
    self.assertIsNone(reason)

  def test_always_on_group_may_not_be_paused(self):
    allowed, reason = google_ads_asset_groups._check_pause_allowed(
        ALWAYS_ON_GROUP
    )
    self.assertFalse(allowed)
    self.assertIn("always-on", reason)

  def test_unmanaged_group_may_not_be_paused(self):
    allowed, reason = google_ads_asset_groups._check_pause_allowed(
        "ARC_Brand_Generic_VAN"
    )
    self.assertFalse(allowed)
    self.assertIn("not a managed weather asset", reason)

  @patch.dict(
      "os.environ", {"ADSTA_ASSET_GROUP_MANAGED_PATTERN": ""}, clear=False
  )
  def test_empty_managed_pattern_disables_allow_list(self):
    allowed, _ = google_ads_asset_groups._check_pause_allowed(
        "ARC_Brand_Generic_VAN"
    )
    self.assertTrue(allowed)


class TestUpdateAssetGroupStatus(unittest.TestCase):

  def _client_returning(self, rows):
    """Builds a mock client whose search() yields the given rows."""
    client = MagicMock()
    ga_service = MagicMock()
    ga_service.search.return_value = iter(rows)
    asset_group_service = MagicMock()
    asset_group_service.mutate_asset_groups.return_value = MagicMock(
        results=[MagicMock(resource_name="customers/1/assetGroups/2")]
    )

    def get_service(name):
      return ga_service if name == "GoogleAdsService" else asset_group_service

    client.get_service.side_effect = get_service
    return client, asset_group_service

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_enable_weather_group(self, mock_get_client):
    client, asset_group_service = self._client_returning(
        [_mock_row(2, COLD_GROUP, "PAUSED")]
    )
    mock_get_client.return_value = client

    result = google_ads_asset_groups.update_google_ads_asset_group_status(
        "12345", "2", "ENABLED"
    )

    self.assertTrue(result["success"])
    self.assertEqual(result["previous_status"], "PAUSED")
    self.assertEqual(result["new_status"], "ENABLED")
    asset_group_service.mutate_asset_groups.assert_called_once()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_pause_always_on_group_is_blocked(self, mock_get_client):
    client, asset_group_service = self._client_returning(
        [_mock_row(3, ALWAYS_ON_GROUP, "ENABLED")]
    )
    mock_get_client.return_value = client

    result = google_ads_asset_groups.update_google_ads_asset_group_status(
        "12345", "3", "PAUSED"
    )

    self.assertFalse(result["success"])
    self.assertTrue(result["blocked_by_guard"])
    # The critical assertion: no mutation reached the API.
    asset_group_service.mutate_asset_groups.assert_not_called()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_enabling_always_on_group_is_permitted(self, mock_get_client):
    """Enabling can never violate the always-on requirement."""
    client, asset_group_service = self._client_returning(
        [_mock_row(3, ALWAYS_ON_GROUP, "PAUSED")]
    )
    mock_get_client.return_value = client

    result = google_ads_asset_groups.update_google_ads_asset_group_status(
        "12345", "3", "ENABLED"
    )

    self.assertTrue(result["success"])
    asset_group_service.mutate_asset_groups.assert_called_once()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_no_op_when_already_in_target_status(self, mock_get_client):
    client, asset_group_service = self._client_returning(
        [_mock_row(2, COLD_GROUP, "ENABLED")]
    )
    mock_get_client.return_value = client

    result = google_ads_asset_groups.update_google_ads_asset_group_status(
        "12345", "2", "ENABLED"
    )

    self.assertTrue(result["success"])
    self.assertTrue(result["skipped"])
    asset_group_service.mutate_asset_groups.assert_not_called()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_missing_asset_group_reports_error(self, mock_get_client):
    client, asset_group_service = self._client_returning([])
    mock_get_client.return_value = client

    result = google_ads_asset_groups.update_google_ads_asset_group_status(
        "12345", "999", "PAUSED"
    )

    self.assertFalse(result["success"])
    self.assertIn("not found", result["error"])
    asset_group_service.mutate_asset_groups.assert_not_called()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_invalid_status_rejected_before_api_call(self, mock_get_client):
    result = google_ads_asset_groups.update_google_ads_asset_group_status(
        "12345", "2", "REMOVED"
    )
    self.assertFalse(result["success"])
    self.assertIn("Invalid status", result["error"])
    mock_get_client.assert_not_called()


class TestListAssetGroups(unittest.TestCase):

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_list_annotates_protection(self, mock_get_client):
    client = MagicMock()
    ga_service = MagicMock()
    ga_service.search.return_value = iter([
        _mock_row(2, COLD_GROUP, "PAUSED"),
        _mock_row(3, ALWAYS_ON_GROUP, "ENABLED"),
    ])
    client.get_service.return_value = ga_service
    mock_get_client.return_value = client

    result = google_ads_asset_groups.list_google_ads_asset_groups(
        "12345", "111"
    )

    self.assertTrue(result["success"])
    self.assertEqual(result["asset_group_count"], 2)

    by_name = {g["asset_group_name"]: g for g in result["asset_groups"]}
    self.assertTrue(by_name[COLD_GROUP]["can_be_paused"])
    self.assertFalse(by_name[COLD_GROUP]["is_always_on"])
    self.assertFalse(by_name[ALWAYS_ON_GROUP]["can_be_paused"])
    self.assertTrue(by_name[ALWAYS_ON_GROUP]["is_always_on"])

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client",
      return_value=None,
  )
  def test_client_failure_is_reported(self, _mock_get_client):
    result = google_ads_asset_groups.list_google_ads_asset_groups(
        "12345", "111"
    )
    self.assertFalse(result["success"])


class TestGaqlEscaping(unittest.TestCase):
  """Names come from operator-managed config, so they must be escaped."""

  def test_plain_name_is_unchanged(self):
    self.assertEqual(
        google_ads_asset_groups._escape_gaql_string_literal(COLD_GROUP),
        COLD_GROUP,
    )

  def test_single_quote_is_escaped(self):
    self.assertEqual(
        google_ads_asset_groups._escape_gaql_string_literal("Arc'teryx"),
        "Arc\\'teryx",
    )

  def test_backslash_is_escaped_before_quotes(self):
    # The backslash must be doubled first, otherwise the escape character
    # introduced for the quote would itself be re-escaped.
    self.assertEqual(
        google_ads_asset_groups._escape_gaql_string_literal("a\\b'c"),
        "a\\\\b\\'c",
    )

  def test_none_is_tolerated(self):
    self.assertEqual(
        google_ads_asset_groups._escape_gaql_string_literal(None), ""
    )


class TestFindAssetGroupByName(unittest.TestCase):

  def _client_returning(self, rows):
    client = MagicMock()
    ga_service = MagicMock()
    ga_service.search.return_value = iter(rows)
    client.get_service.return_value = ga_service
    return client

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_single_match_is_resolved(self, mock_get_client):
    mock_get_client.return_value = self._client_returning(
        [_mock_row(2, COLD_GROUP, "PAUSED")]
    )

    result = google_ads_asset_groups.find_google_ads_asset_group_by_name(
        "12345", "ARC_Campaign", COLD_GROUP
    )

    self.assertTrue(result["success"])
    self.assertEqual(result["asset_group_id"], "2")
    self.assertFalse(result["is_always_on"])
    self.assertTrue(result["can_be_paused"])

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_no_match_reports_error(self, mock_get_client):
    mock_get_client.return_value = self._client_returning([])

    result = google_ads_asset_groups.find_google_ads_asset_group_by_name(
        "12345", "ARC_Campaign", COLD_GROUP
    )

    self.assertFalse(result["success"])
    self.assertIn("No asset group named", result["error"])

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_ambiguous_match_is_reported(self, mock_get_client):
    mock_get_client.return_value = self._client_returning([
        _mock_row(2, COLD_GROUP, "PAUSED"),
        _mock_row(4, COLD_GROUP, "ENABLED"),
    ])

    result = google_ads_asset_groups.find_google_ads_asset_group_by_name(
        "12345", "ARC_Campaign", COLD_GROUP
    )

    self.assertFalse(result["success"])
    self.assertTrue(result["ambiguous"])
    self.assertEqual(len(result["matches"]), 2)

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_always_on_group_is_flagged(self, mock_get_client):
    mock_get_client.return_value = self._client_returning(
        [_mock_row(3, ALWAYS_ON_GROUP, "ENABLED")]
    )

    result = google_ads_asset_groups.find_google_ads_asset_group_by_name(
        "12345", "ARC_Campaign", ALWAYS_ON_GROUP
    )

    self.assertTrue(result["success"])
    self.assertTrue(result["is_always_on"])
    self.assertFalse(result["can_be_paused"])
    self.assertIn("always-on", result["pause_block_reason"])


class TestUpdateAssetGroupStatusByName(unittest.TestCase):

  def _client_returning(self, rows):
    client = MagicMock()
    ga_service = MagicMock()
    ga_service.search.return_value = iter(rows)
    asset_group_service = MagicMock()
    asset_group_service.mutate_asset_groups.return_value = MagicMock(
        results=[MagicMock(resource_name="customers/1/assetGroups/2")]
    )

    def get_service(name):
      return ga_service if name == "GoogleAdsService" else asset_group_service

    client.get_service.side_effect = get_service
    return client, asset_group_service

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_enable_by_name(self, mock_get_client):
    client, asset_group_service = self._client_returning(
        [_mock_row(2, COLD_GROUP, "PAUSED")]
    )
    mock_get_client.return_value = client

    result = (
        google_ads_asset_groups.update_google_ads_asset_group_status_by_name(
            "12345", "ARC_Campaign", COLD_GROUP, "ENABLED"
        )
    )

    self.assertTrue(result["success"])
    self.assertEqual(result["previous_status"], "PAUSED")
    self.assertEqual(result["new_status"], "ENABLED")
    asset_group_service.mutate_asset_groups.assert_called_once()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_pause_always_on_by_name_is_blocked(self, mock_get_client):
    client, asset_group_service = self._client_returning(
        [_mock_row(3, ALWAYS_ON_GROUP, "ENABLED")]
    )
    mock_get_client.return_value = client

    result = (
        google_ads_asset_groups.update_google_ads_asset_group_status_by_name(
            "12345", "ARC_Campaign", ALWAYS_ON_GROUP, "PAUSED"
        )
    )

    self.assertFalse(result["success"])
    self.assertTrue(result["blocked_by_guard"])
    # The name path must enforce the same guard as the ID path.
    asset_group_service.mutate_asset_groups.assert_not_called()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_ambiguous_name_makes_no_change(self, mock_get_client):
    client, asset_group_service = self._client_returning([
        _mock_row(2, COLD_GROUP, "PAUSED"),
        _mock_row(4, COLD_GROUP, "PAUSED"),
    ])
    mock_get_client.return_value = client

    result = (
        google_ads_asset_groups.update_google_ads_asset_group_status_by_name(
            "12345", "ARC_Campaign", COLD_GROUP, "ENABLED"
        )
    )

    self.assertFalse(result["success"])
    self.assertTrue(result["ambiguous"])
    asset_group_service.mutate_asset_groups.assert_not_called()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_missing_name_makes_no_change(self, mock_get_client):
    client, asset_group_service = self._client_returning([])
    mock_get_client.return_value = client

    result = (
        google_ads_asset_groups.update_google_ads_asset_group_status_by_name(
            "12345", "ARC_Campaign", COLD_GROUP, "ENABLED"
        )
    )

    self.assertFalse(result["success"])
    self.assertIn("No asset group named", result["error"])
    asset_group_service.mutate_asset_groups.assert_not_called()

  @patch(
      "agentic_dsta.tools.google_ads.google_ads_asset_groups"
      ".get_google_ads_client"
  )
  def test_invalid_status_rejected_before_api_call(self, mock_get_client):
    result = (
        google_ads_asset_groups.update_google_ads_asset_group_status_by_name(
            "12345", "ARC_Campaign", COLD_GROUP, "REMOVED"
        )
    )
    self.assertFalse(result["success"])
    self.assertIn("Invalid status", result["error"])
    mock_get_client.assert_not_called()


class TestToolsetRegistration(unittest.TestCase):

  def test_toolset_exposes_all_tools(self):
    toolset = google_ads_asset_groups.GoogleAdsAssetGroupToolset()
    tools = asyncio.run(toolset.get_tools())
    self.assertEqual(len(tools), 4)

  def test_toolset_tool_names(self):
    toolset = google_ads_asset_groups.GoogleAdsAssetGroupToolset()
    tools = asyncio.run(toolset.get_tools())
    self.assertCountEqual(
        [tool.name for tool in tools],
        [
            "list_google_ads_asset_groups",
            "find_google_ads_asset_group_by_name",
            "update_google_ads_asset_group_status",
            "update_google_ads_asset_group_status_by_name",
        ],
    )


if __name__ == "__main__":
  unittest.main()
