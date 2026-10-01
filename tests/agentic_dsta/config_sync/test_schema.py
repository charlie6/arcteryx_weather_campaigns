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
"""Tests for parsing and validating the configuration sheet."""

from typing import Any, Dict, List

from agentic_dsta.config_sync import schema

CITIES = {"Vancouver BC", "Toronto ON"}

ACCOUNT_HEADER = [c.header for c in schema.ACCOUNT_COLUMNS]
CAMPAIGN_HEADER = [c.header for c in schema.CAMPAIGN_COLUMNS]
CITY_HEADER = [c.header for c in schema.CITY_COLUMNS]


def account_row(**overrides: Any) -> List[Any]:
    values = {
        "Customer ID": "534-111-4500",
        "Account label": "Canada",
        "Timezone": "America/Vancouver",
        "Runs per day": 2,
        "Look-ahead hours": 24,
        "Trailing window hours": 24,
        "Change guard enabled": "Y",
        "Change guard max fraction": 0.25,
        "Cold token": "_cold",
        "Rain token": "_RAIN",
        "Snow token": "_SNOW",
        "Sunny token": "_SUN",
    }
    values.update(overrides)
    return [values[h] for h in ACCOUNT_HEADER]


def campaign_row(**overrides: Any) -> List[Any]:
    values = {
        "Customer ID": 5341114500,
        "Campaign ID": 24252893412,
        "Active": "Y",
        "City": "Vancouver BC",
        "Geo": "VAN",
        "Campaign name contains": "ADSTA",
        "Asset groups": "Y",
        "Severe budget": "Y",
        "Normal daily budget": 10,
        "Budget bump %": 50,
        "Max daily budget": "$20.00",
    }
    values.update(overrides)
    return [values[h] for h in CAMPAIGN_HEADER]


def city_row(**overrides: Any) -> List[Any]:
    values = {
        "City": "Vancouver BC",
        "Cold below C": 8,
        "Rain rate mm/h": 0.3,
        "Sunny min hours": 4,
        "Latitude (ref)": 49.28,
        "Longitude (ref)": -123.12,
        "Severe rain mm (ref)": 30,
        "Severe snow mm SWE (ref)": 10,
        "Severe cold C (ref)": -5,
    }
    values.update(overrides)
    return [values[h] for h in CITY_HEADER]


def tabs(accounts=None, campaigns=None, cities=None) -> Dict[str, List[List[Any]]]:
    return {
        schema.ACCOUNTS_TAB: [ACCOUNT_HEADER] + (accounts if accounts is not None else [account_row()]),
        schema.CAMPAIGNS_TAB: [CAMPAIGN_HEADER] + (campaigns if campaigns is not None else [campaign_row()]),
        schema.CITIES_TAB: [CITY_HEADER] + (cities if cities is not None else [city_row()]),
    }


def messages(config: schema.SheetConfig) -> str:
    return "\n".join(str(i) for i in config.issues)


class TestValidSheet:
    def test_parses_every_tab(self):
        config = schema.parse_sheet(tabs(), CITIES)
        assert not config.errors, messages(config)
        account = config.accounts["5341114500"]
        assert account.tokens == {"Cold": "_COLD", "Rain": "_RAIN", "Snow": "_SNOW", "Sunny": "_SUN"}
        assert account.guard_enabled is True
        campaign = config.campaigns[0]
        assert campaign.campaign_id == 24252893412
        assert campaign.playbooks == ["weather_asset_groups", "severe_budget"]
        assert campaign.normal_budget_micros == 10_000_000
        assert campaign.max_budget_micros == 20_000_000
        assert config.cities["Vancouver BC"].activation == {
            "coldBelowC": 8.0, "rainRateMmPerH": 0.3, "sunnyMinHours": 4,
        }

    def test_columns_are_matched_by_header_not_position(self):
        sheet = tabs()
        sheet[schema.CITIES_TAB] = [
            ["sunny MIN hours", "Notes", "city", "rain rate (mm/h)", "cold below °C"],
            [6, "anything", "Toronto ON", 0.2, 5],
        ]
        config = schema.parse_sheet(sheet, CITIES)
        assert not config.errors, messages(config)
        assert config.cities["Toronto ON"].activation == {
            "coldBelowC": 5.0, "rainRateMmPerH": 0.2, "sunnyMinHours": 6,
        }

    def test_blank_rows_are_skipped_and_blank_thresholds_unset(self):
        config = schema.parse_sheet(
            tabs(cities=[["", "", ""], city_row(**{"Rain rate mm/h": ""})]), CITIES
        )
        assert not config.errors, messages(config)
        assert "rainRateMmPerH" not in config.cities["Vancouver BC"].activation
        assert any("playbook default applies" in str(w) for w in config.warnings)

    def test_budgets_round_to_the_cent(self):
        assert schema.units_to_micros(12.345) == 12_350_000
        assert schema.units_to_micros(0.01) == 10_000


class TestValidationErrors:
    def test_unknown_city_is_an_error(self):
        config = schema.parse_sheet(tabs(campaigns=[campaign_row(City="Vancover BC")]), CITIES)
        assert any("Vancover BC" in str(e) for e in config.errors)

    def test_out_of_range_threshold_is_an_error(self):
        config = schema.parse_sheet(tabs(cities=[city_row(**{"Cold below C": 90})]), CITIES)
        assert any("Cold below C" in str(e) for e in config.errors)

    def test_bad_timezone_and_duplicate_tokens(self):
        config = schema.parse_sheet(
            tabs(accounts=[account_row(Timezone="Vancouver", **{"Snow token": "_rain"})]), CITIES
        )
        text = messages(config)
        assert "IANA" in text
        assert "also used for Rain" in text

    def test_campaign_for_unknown_account(self):
        config = schema.parse_sheet(tabs(campaigns=[campaign_row(**{"Customer ID": "1111111111"})]), CITIES)
        assert any("no valid row" in str(e) for e in config.errors)

    def test_severe_budget_requires_a_bump(self):
        config = schema.parse_sheet(tabs(campaigns=[campaign_row(**{"Budget bump %": ""})]), CITIES)
        assert any("required when Severe budget" in str(e) for e in config.errors)

    def test_max_budget_below_normal(self):
        config = schema.parse_sheet(tabs(campaigns=[campaign_row(**{"Max daily budget": 5})]), CITIES)
        assert any("at least the Normal daily budget" in str(e) for e in config.errors)

    def test_duplicate_campaign(self):
        config = schema.parse_sheet(tabs(campaigns=[campaign_row(), campaign_row()]), CITIES)
        assert any("already listed in row 2" in str(e) for e in config.errors)

    def test_active_campaign_needs_a_playbook(self):
        config = schema.parse_sheet(
            tabs(campaigns=[campaign_row(**{"Asset groups": "N", "Severe budget": "N", "Budget bump %": ""})]),
            CITIES,
        )
        assert any("must run" in str(e) for e in config.errors)

    def test_missing_required_column(self):
        sheet = tabs()
        sheet[schema.ACCOUNTS_TAB] = [["Customer ID"], ["5341114500"]]
        config = schema.parse_sheet(sheet, CITIES)
        assert any("missing required column" in str(e) for e in config.errors)

    def test_missing_tab(self):
        sheet = tabs()
        sheet[schema.CITIES_TAB] = []
        config = schema.parse_sheet(sheet, CITIES)
        assert any(e.tab == schema.CITIES_TAB for e in config.errors)

    def test_bad_flag(self):
        config = schema.parse_sheet(tabs(campaigns=[campaign_row(Active="maybe")]), CITIES)
        assert any("not Y or N" in str(e) for e in config.errors)

    def test_issue_reports_the_sheet_row(self):
        config = schema.parse_sheet(
            tabs(cities=[city_row(), city_row(City="Toronto ON", **{"Sunny min hours": -1})]), CITIES
        )
        assert [e.row for e in config.errors] == [3]
