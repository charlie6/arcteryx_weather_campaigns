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
"""Tests for the enforced dry run control.

The property under test is not that a refusal is returned, but that no
write reaches the platform. A control that reports a refusal after issuing
the mutation would be worse than none, so every case here asserts on
whether the underlying service was called.
"""

import unittest
from unittest import mock

from agentic_dsta.core import dry_run
from agentic_dsta.tools.google_ads import google_ads_asset_groups
from agentic_dsta.tools.google_ads import google_ads_updater


MagicMock = mock.MagicMock
patch = mock.patch

WEATHER_GROUP = "ARC_Weather_Snow_VAN_ENG_Neutral_Null_FW25"


def _enabled(value):
  """Builds an environment patch for the dry run variable."""
  return patch.dict("os.environ", {dry_run.DRY_RUN_ENV_VAR: value})


class TestIsDryRun(unittest.TestCase):
  """Parsing of the environment variable."""

  def test_unset_is_disabled(self):
    with patch.dict("os.environ", {}, clear=True):
      self.assertFalse(dry_run.is_dry_run())

  def test_empty_is_disabled(self):
    with _enabled(""):
      self.assertFalse(dry_run.is_dry_run())

  def test_false_is_disabled(self):
    with _enabled("false"):
      self.assertFalse(dry_run.is_dry_run())

  def test_zero_is_disabled(self):
    with _enabled("0"):
      self.assertFalse(dry_run.is_dry_run())

  def test_arbitrary_string_is_disabled(self):
    """Anything unrecognised must not silently enable dry run."""
    with _enabled("maybe"):
      self.assertFalse(dry_run.is_dry_run())

  def test_true_variants_are_enabled(self):
    for value in ("true", "TRUE", "True", "1", "yes", "y", "on", "t"):
      with self.subTest(value=value), _enabled(value):
        self.assertTrue(dry_run.is_dry_run())

  def test_surrounding_whitespace_is_tolerated(self):
    with _enabled("  true  "):
      self.assertTrue(dry_run.is_dry_run())

  def test_read_at_call_time_not_import_time(self):
    """Toggling the variable must take effect without reimporting."""
    with _enabled("true"):
      self.assertTrue(dry_run.is_dry_run())
    with _enabled("false"):
      self.assertFalse(dry_run.is_dry_run())


class TestDryRunResponse(unittest.TestCase):
  """Shape of the suppressed-mutation payload."""

  def setUp(self):
    self.result = dry_run.dry_run_response(
        "update_campaign_budget", {"campaign_id": "123", "new_budget_micros": 5}
    )

  def test_reports_failure_not_success(self):
    """A suppressed write did not happen and must not read as if it did."""
    self.assertFalse(self.result["success"])

  def test_flags_dry_run(self):
    self.assertTrue(self.result["dry_run"])

  def test_names_the_suppressed_action(self):
    self.assertEqual(self.result["action_suppressed"], "update_campaign_budget")

  def test_preserves_the_intended_change(self):
    self.assertEqual(
        self.result["would_have"],
        {"campaign_id": "123", "new_budget_micros": 5},
    )

  def test_message_discourages_retry(self):
    """The model must not treat the refusal as a prompt to find a way round."""
    self.assertIn("Do not retry", self.result["message"])


class TestAssetGroupStatusIsSuppressed(unittest.TestCase):
  """The path that was actually violated during live verification."""

  def _run(self, dry_run_value, status="PAUSED", current="ENABLED"):
    client = MagicMock()
    existing = {
        "asset_group_id": "6747753318",
        "asset_group_name": WEATHER_GROUP,
        "campaign_id": "24252893412",
        "status": current,
    }
    with _enabled(dry_run_value):
      result = google_ads_asset_groups._perform_status_update(
          client, "5341114500", existing, status
      )
    mutated = client.get_service.return_value.mutate_asset_groups.called
    return result, mutated

  def test_pause_is_suppressed(self):
    result, mutated = self._run("true")
    self.assertTrue(result["dry_run"])
    self.assertFalse(result["success"])
    self.assertFalse(mutated, "dry run must not issue the mutation")

  def test_enable_is_suppressed(self):
    """Dry run suppresses every write, not only the destructive direction."""
    result, mutated = self._run("true", status="ENABLED", current="PAUSED")
    self.assertTrue(result["dry_run"])
    self.assertFalse(mutated)

  def test_records_the_intended_transition(self):
    result, _ = self._run("true")
    self.assertEqual(
        result["would_have"]["asset_group_name"], WEATHER_GROUP
    )
    self.assertEqual(result["would_have"]["previous_status"], "ENABLED")
    self.assertEqual(result["would_have"]["new_status"], "PAUSED")

  def test_disabled_allows_the_mutation(self):
    """The control must not block writes when it is switched off."""
    result, mutated = self._run("false")
    self.assertNotIn("dry_run", result)
    self.assertTrue(mutated)

  def test_guard_takes_precedence_over_dry_run(self):
    """A protected group reports the guard, not the dry run.

    Both refuse, so the account is safe either way, but the operator needs
    to know the pause was forbidden rather than merely deferred.
    """
    client = MagicMock()
    existing = {
        "asset_group_id": "1",
        "asset_group_name": "ARC_Always_On_VAN_ENG_Neutral_Null_FW25",
        "campaign_id": "111",
        "status": "ENABLED",
    }
    with _enabled("true"):
      result = google_ads_asset_groups._perform_status_update(
          client, "123", existing, "PAUSED"
      )
    self.assertTrue(result.get("blocked_by_guard"))
    self.assertFalse(client.get_service.return_value.mutate_asset_groups.called)


class TestCampaignStatusIsSuppressed(unittest.TestCase):
  """Campaign level writes are suppressed alongside asset group writes."""

  def test_status_change_is_suppressed(self):
    client = MagicMock()
    with _enabled("true"), patch.object(
        google_ads_updater, "get_google_ads_client", return_value=client
    ):
      result = google_ads_updater.update_google_ads_campaign_status(
          "5341114500", "24252893412", "PAUSED"
      )

    self.assertTrue(result["dry_run"])
    self.assertFalse(result["success"])
    self.assertFalse(
        client.get_service.return_value.mutate_campaigns.called,
        "dry run must not issue the campaign mutation",
    )
    self.assertEqual(result["would_have"]["new_status"], "PAUSED")


if __name__ == "__main__":
  unittest.main()
