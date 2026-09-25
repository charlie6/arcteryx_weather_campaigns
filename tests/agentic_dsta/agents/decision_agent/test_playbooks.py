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
"""Tests for playbook resolution in the decision agent."""

import datetime
import json
import pathlib
from typing import Any, Dict

import pytest

from agentic_dsta.agents.decision_agent import playbooks


_SAMPLE_CONFIG = (
    pathlib.Path(__file__).resolve().parents[4]
    / "infra"
    / "config"
    / "samples"
    / "arcteryx_test_firestore_config.json"
)


def _context(playbook_id: str = "test_playbook") -> Dict[str, Any]:
    """Builds a deterministic context for tests."""
    return playbooks.build_context(
        customer_id="1234567890",
        campaign_id="1111111111",
        playbook_id=playbook_id,
        run_id="2026-01-15T06:00:00Z_abcd1234",
        account="Canada",
        timezone_name="America/Vancouver",
        now_utc=datetime.datetime(2026, 1, 15, 14, 0, tzinfo=datetime.timezone.utc),
    )


class TestFlattenParams:
    def test_flattens_nested_dicts_to_dotted_keys(self):
        flat = playbooks.flatten_params(
            {"city": "Vancouver BC", "severeModifiers": {"budgetBumpPct": 45.0}}
        )
        assert flat["city"] == "Vancouver BC"
        assert flat["severeModifiers.budgetBumpPct"] == 45.0

    def test_retains_the_intermediate_dict_under_its_own_key(self):
        flat = playbooks.flatten_params({"severeModifiers": {"a": 1}})
        assert flat["severeModifiers"] == {"a": 1}

    def test_handles_empty_input(self):
        assert playbooks.flatten_params({}) == {}


class TestRenderTemplate:
    def test_substitutes_simple_and_dotted_tokens(self):
        rendered = playbooks.render_template(
            "City {{city}} bump {{severeModifiers.budgetBumpPct}} pct",
            {"city": "Vancouver BC", "severeModifiers.budgetBumpPct": 50},
        )
        assert rendered == "City Vancouver BC bump 50 pct"

    def test_tolerates_whitespace_inside_braces(self):
        assert playbooks.render_template("{{ city }}", {"city": "Calgary AB"}) == "Calgary AB"

    def test_renders_none_as_null_so_optional_values_read_naturally(self):
        assert playbooks.render_template("{{ceiling}}", {"ceiling": None}) == "null"

    def test_renders_booleans_lowercase(self):
        assert playbooks.render_template("{{flag}}", {"flag": True}) == "true"

    def test_raises_rather_than_leaking_an_unresolved_placeholder(self):
        # A prompt containing the literal "{{city}}" would tell the agent to look
        # up a Firestore document by that name. Failing loudly is the point.
        with pytest.raises(playbooks.PlaybookResolutionError) as err:
            playbooks.render_template("{{city}} and {{geo}}", {"city": "X"})
        assert "geo" in str(err.value)

    def test_leaves_single_braces_and_percent_signs_alone(self):
        text = "Increase by 50% and write {not a placeholder}"
        assert playbooks.render_template(text, {}) == text


class TestSeasonForMonth:
    @pytest.mark.parametrize(
        "month,expected",
        [(1, "winter"), (4, "spring"), (7, "summer"), (10, "autumn"), (12, "winter")],
    )
    def test_northern_hemisphere_seasons(self, month, expected):
        assert playbooks.season_for_month(month) == expected

    def test_southern_hemisphere_is_inverted(self):
        assert playbooks.season_for_month(1, hemisphere="southern") == "summer"

    def test_rejects_an_invalid_month(self):
        with pytest.raises(ValueError):
            playbooks.season_for_month(13)


class TestBuildContext:
    def test_derives_state_doc_ids_from_identity(self):
        context = _context()
        assert context["budgetStateDocId"] == "1234567890_1111111111"
        assert context["assetGroupStateDocId"] == "1234567890_1111111111"

    def test_provides_both_utc_and_account_local_timestamps(self):
        # Section 6 of the spec requires the change log to carry both.
        context = _context()
        assert context["today"] == "2026-01-15"
        assert context["nowIsoUtc"].startswith("2026-01-15T14:00:00")
        # 14:00 UTC is 06:00 in Vancouver in January.
        assert context["nowIsoLocal"].startswith("2026-01-15T06:00:00")

    def test_derives_the_season_from_the_current_month(self):
        assert _context()["season"] == "winter"

    def test_falls_back_to_utc_on_an_unknown_timezone(self):
        context = playbooks.build_context(
            customer_id="1",
            campaign_id="2",
            playbook_id="p",
            run_id="r",
            account="Canada",
            timezone_name="Not/AZone",
            now_utc=datetime.datetime(2026, 1, 15, 14, 0, tzinfo=datetime.timezone.utc),
        )
        assert context["timezone"] == "UTC"
        assert context["nowIsoLocal"] == context["nowIsoUtc"]


class TestResolvePlaybookIds:
    def test_accepts_a_list(self):
        assert playbooks.resolve_playbook_ids({"playbooks": ["a", "b"]}) == ["a", "b"]

    def test_accepts_a_single_string_under_either_key(self):
        assert playbooks.resolve_playbook_ids({"playbooks": "a"}) == ["a"]
        assert playbooks.resolve_playbook_ids({"playbook": "b"}) == ["b"]

    def test_returns_empty_when_none_declared(self):
        assert playbooks.resolve_playbook_ids({"campaignId": 1}) == []


class TestRenderCampaignPlaybook:
    def test_renders_params_and_context_together(self):
        result = playbooks.render_campaign_playbook(
            playbook_id="p",
            playbook={"template": "Campaign {{campaignId}} in {{city}}"},
            campaign={"params": {"city": "Vancouver BC"}},
            context=_context(),
        )
        assert result == "Campaign 1111111111 in Vancouver BC"

    def test_campaign_params_override_playbook_defaults(self):
        result = playbooks.render_campaign_playbook(
            playbook_id="p",
            playbook={"template": "{{days}}", "defaults": {"days": 5}},
            campaign={"params": {"days": 7}},
            context=_context(),
        )
        assert result == "7"

    def test_defaults_supply_optional_values_the_campaign_omits(self):
        # The optional budget ceiling must render as "null" rather than causing
        # the whole playbook to be skipped.
        result = playbooks.render_campaign_playbook(
            playbook_id="p",
            playbook={
                "template": "ceiling {{severeModifiers.maxDailyBudgetMicros}}",
                "defaults": {"severeModifiers": {"maxDailyBudgetMicros": None}},
            },
            campaign={"params": {"severeModifiers": {"budgetBumpPct": 45.0}}},
            context=_context(),
        )
        assert result == "ceiling null"

    def test_runner_context_wins_over_a_params_attempt_to_shadow_it(self):
        result = playbooks.render_campaign_playbook(
            playbook_id="p",
            playbook={"template": "{{campaignId}}"},
            campaign={"params": {"campaignId": "9999999999"}},
            context=_context(),
        )
        assert result == "1111111111"

    def test_missing_required_param_raises_so_the_playbook_is_skipped(self):
        # Spec section 7: no Severe Modifiers row means no budget increase, but
        # asset group toggling must still run. Raising here lets the caller skip
        # just this playbook.
        with pytest.raises(playbooks.PlaybookResolutionError) as err:
            playbooks.render_campaign_playbook(
                playbook_id="severe_budget",
                playbook={
                    "template": "{{city}}",
                    "requiredParams": ["severeModifiers.budgetBumpPct"],
                },
                campaign={"params": {"city": "Toronto ON"}},
                context=_context(),
            )
        assert "severeModifiers.budgetBumpPct" in str(err.value)

    def test_empty_template_raises(self):
        with pytest.raises(playbooks.PlaybookResolutionError):
            playbooks.render_campaign_playbook(
                playbook_id="p", playbook={"template": "  "}, campaign={}, context=_context()
            )


class TestResolveCampaignInstructions:
    def _library(self) -> Dict[str, Dict[str, Any]]:
        return {
            "weather_asset_groups": {"template": "toggle {{city}}", "requiredParams": ["city"]},
            "severe_budget": {
                "template": "budget {{city}} {{severeModifiers.budgetBumpPct}}",
                "requiredParams": ["severeModifiers.budgetBumpPct"],
            },
        }

    def test_resolves_multiple_playbooks_in_order(self):
        resolved = playbooks.resolve_campaign_instructions(
            campaign={
                "campaignId": 1111111111,
                "playbooks": ["weather_asset_groups", "severe_budget"],
                "params": {"city": "Vancouver BC", "severeModifiers": {"budgetBumpPct": 45.0}},
            },
            playbook_library=self._library(),
            context_builder=lambda pid: _context(pid),
        )
        assert [pid for pid, _ in resolved] == ["weather_asset_groups", "severe_budget"]

    def test_skips_only_the_playbook_whose_params_are_missing(self):
        # The campaign has no severeModifiers, so budget is skipped but toggling
        # still runs. This is the behaviour spec section 7 requires.
        resolved = playbooks.resolve_campaign_instructions(
            campaign={
                "campaignId": 4444444444,
                "playbooks": ["weather_asset_groups", "severe_budget"],
                "params": {"city": "Toronto ON"},
            },
            playbook_library=self._library(),
            context_builder=lambda pid: _context(pid),
        )
        assert [pid for pid, _ in resolved] == ["weather_asset_groups"]

    def test_skips_an_unknown_playbook_reference(self):
        resolved = playbooks.resolve_campaign_instructions(
            campaign={"campaignId": 1, "playbooks": ["nope"], "params": {}},
            playbook_library=self._library(),
            context_builder=lambda pid: _context(pid),
        )
        assert resolved == []

    def test_falls_back_to_a_legacy_inline_instruction(self):
        resolved = playbooks.resolve_campaign_instructions(
            campaign={"campaignId": 1, "instruction": "do the thing"},
            playbook_library={},
            context_builder=lambda pid: _context(pid),
        )
        assert resolved == [("inline", "do the thing")]

    def test_returns_nothing_when_a_campaign_declares_no_logic(self):
        resolved = playbooks.resolve_campaign_instructions(
            campaign={"campaignId": 1},
            playbook_library={},
            context_builder=lambda pid: _context(pid),
        )
        assert resolved == []


class TestSampleConfigRenders:
    """Guards the shipped sample config against placeholder rot.

    Adding a city is meant to be a few lines of params. This test fails if a
    campaign references a playbook whose placeholders those params do not
    satisfy, which is the failure mode the config shape exists to prevent.
    """

    def _load(self):
        documents = json.loads(_SAMPLE_CONFIG.read_text())
        library = {
            doc["document_id"]: doc["data"]
            for doc in documents
            if doc["collection_name"] == "Playbooks"
        }
        ads_config = next(
            doc["data"]
            for doc in documents
            if doc["collection_name"] == "GoogleAdsConfig"
        )
        return library, ads_config

    def _shared_values(self, ads_config) -> Dict[str, Any]:
        # Mirrors agent._account_weather_values: windows and tokens come from
        # the account document, not a separate WeatherConditions collection.
        return {
            "globalInstruction": "…",
            "lookAheadHours": ads_config.get("lookAheadHours", 24),
            "trailingWindowHours": ads_config.get("trailingWindowHours", 24),
            "assetGroupTokens": ads_config["assetGroupTokens"],
            "runsPerDay": 2,
            "trailingWindowRuns": 2,
        }

    def test_every_campaign_resolves_at_least_one_playbook(self):
        library, ads_config = self._load()
        for campaign in ads_config["campaigns"]:
            resolved = playbooks.resolve_campaign_instructions(
                campaign=campaign,
                playbook_library=library,
                context_builder=lambda pid, c=campaign: playbooks.build_context(
                    customer_id="1234567890",
                    campaign_id=c["campaignId"],
                    playbook_id=pid,
                    run_id="run",
                    account=ads_config["account"],
                    timezone_name=ads_config["schedule"]["timezone"],
                ),
                shared_values=self._shared_values(ads_config),
            )
            assert resolved, f"Campaign {campaign['campaignId']} resolved to nothing"

    def test_no_rendered_instruction_retains_a_placeholder(self):
        library, ads_config = self._load()
        for campaign in ads_config["campaigns"]:
            resolved = playbooks.resolve_campaign_instructions(
                campaign=campaign,
                playbook_library=library,
                context_builder=lambda pid, c=campaign: playbooks.build_context(
                    customer_id="1234567890",
                    campaign_id=c["campaignId"],
                    playbook_id=pid,
                    run_id="run",
                    account=ads_config["account"],
                    timezone_name=ads_config["schedule"]["timezone"],
                ),
                shared_values=self._shared_values(ads_config),
            )
            for playbook_id, instruction in resolved:
                assert not playbooks.find_placeholders(instruction), (
                    f"{playbook_id} for campaign {campaign['campaignId']} still "
                    "contains placeholders"
                )

    def test_a_campaign_without_severe_modifiers_still_toggles_asset_groups(self):
        library, ads_config = self._load()
        toronto = next(
            c for c in ads_config["campaigns"] if c["params"]["city"] == "Toronto ON"
        )
        assert "severeModifiers" not in toronto["params"]
        resolved = playbooks.resolve_campaign_instructions(
            campaign=toronto,
            playbook_library=library,
            context_builder=lambda pid: playbooks.build_context(
                customer_id="1234567890",
                campaign_id=toronto["campaignId"],
                playbook_id=pid,
                run_id="run",
                account="Canada",
                timezone_name="America/Toronto",
            ),
            shared_values=self._shared_values(ads_config),
        )
        assert [pid for pid, _ in resolved] == ["weather_asset_groups"]

    def test_no_playbook_template_hardcodes_a_coordinate_or_campaign_name(self):
        # The regression this whole change exists to prevent.
        library, _ = self._load()
        for playbook_id, playbook in library.items():
            template = playbook["template"]
            assert "49.2827" not in template, f"{playbook_id} hardcodes a latitude"
            assert "-123.1207" not in template, f"{playbook_id} hardcodes a longitude"
            assert "ARC_Performance" not in template, f"{playbook_id} hardcodes a campaign name"
            assert "ARC_Weather" not in template, f"{playbook_id} hardcodes an asset group name"

    def _render_asset_groups(self, campaign_params):
        library, ads_config = self._load()
        return playbooks.render_campaign_playbook(
            playbook_id="weather_asset_groups",
            playbook=library["weather_asset_groups"],
            campaign={"campaignId": 1, "params": campaign_params},
            context=playbooks.build_context(
                customer_id="1234567890",
                campaign_id=1,
                playbook_id="weather_asset_groups",
                run_id="run",
                account="Canada",
                timezone_name="America/Toronto",
            ),
            shared_values=self._shared_values(ads_config),
        )

    def test_activation_rules_name_the_exact_signal_and_default_threshold(self):
        # The old free-text rules left "temperature below 9" open to reading as
        # the mean or current value. The template must pin min for Cold and max
        # for Warm.
        rendered = self._render_asset_groups({"city": "Vancouver BC"})
        assert "min_temperature_c < 9.0" in rendered
        assert "max_temperature_c > 10.0" in rendered
        assert "max_rain_rate_mm_per_h >= 0.2" in rendered
        assert "'_COLD_'" in rendered and "'_SUN_'" in rendered

    def test_a_campaign_can_override_one_activation_threshold(self):
        rendered = self._render_asset_groups(
            {"city": "Chicago IL", "activation": {"coldBelowC": 0.0}}
        )
        assert "min_temperature_c < 0.0" in rendered
        # The other thresholds keep the playbook defaults.
        assert "max_temperature_c > 10.0" in rendered
        assert "max_rain_rate_mm_per_h >= 0.2" in rendered

    def test_missing_tokens_make_the_asset_group_playbook_unrenderable(self):
        # Matching asset groups on a guessed token is worse than skipping.
        library, ads_config = self._load()
        shared = self._shared_values(ads_config)
        del shared["assetGroupTokens"]
        with pytest.raises(playbooks.PlaybookResolutionError):
            playbooks.render_campaign_playbook(
                playbook_id="weather_asset_groups",
                playbook=library["weather_asset_groups"],
                campaign={"campaignId": 1, "params": {"city": "Vancouver BC"}},
                context=_context("weather_asset_groups"),
                shared_values=shared,
            )


class TestAccountWeatherValues:
    """agent._account_weather_values replaced the WeatherConditions collection."""

    def _fn(self):
        from agentic_dsta.agents.decision_agent import agent

        return agent._account_weather_values

    def test_reads_windows_and_tokens_from_the_account(self):
        values = self._fn()(
            {
                "lookAheadHours": 12,
                "trailingWindowHours": 36,
                "assetGroupTokens": {"Cold": "_COLD"},
            },
            "1",
        )
        assert values == {
            "lookAheadHours": 12,
            "trailingWindowHours": 36,
            "assetGroupTokens": {"Cold": "_COLD"},
        }

    def test_defaults_windows_to_24_hours(self):
        values = self._fn()({"assetGroupTokens": {"Cold": "_COLD"}}, "1")
        assert values["lookAheadHours"] == 24
        assert values["trailingWindowHours"] == 24

    def test_missing_tokens_are_left_absent_and_logged(self, caplog):
        values = self._fn()({}, "1")
        assert "assetGroupTokens" not in values
        assert "no assetGroupTokens" in caplog.text

    def test_a_stale_weather_conditions_id_is_flagged(self, caplog):
        self._fn()({"weatherConditionsId": "sandbox", "assetGroupTokens": {"a": "b"}}, "1")
        assert "no longer read" in caplog.text
