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
"""Tests for planning, applying and exporting the configuration sheet sync."""

import copy
import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from google.api_core import exceptions as gexc
from google.cloud import firestore
import pytest

from agentic_dsta.config_sync import schema
from agentic_dsta.config_sync import sync
from test_schema import (  # pylint: disable=import-error
    account_row,
    campaign_row,
    city_row,
    tabs,
)

NOW = datetime.datetime(2026, 10, 1, 13, 0, tzinfo=datetime.timezone.utc)
CUSTOMER = "5341114500"
CAMPAIGN = 24252893412
STATE_ID = f"{CUSTOMER}_{CAMPAIGN}"


# --- Fakes --------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, doc_id: str, data: Optional[Dict[str, Any]]):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class FakeDoc:
    def __init__(self, store, key: Tuple[str, str]):
        self._store = store
        self._key = key

    def get(self):
        return FakeSnapshot(self._key[1], self._store.get(self._key))

    def set(self, data, merge: bool = False):
        data = copy.deepcopy(data)
        if merge and self._key in self._store:
            self._store[self._key].update(data)
        else:
            self._store[self._key] = data

    def update(self, data):
        if self._key not in self._store:
            raise KeyError(self._key)
        for field, value in data.items():
            if value is firestore.DELETE_FIELD:
                self._store[self._key].pop(field, None)
            else:
                self._store[self._key][field] = copy.deepcopy(value)

    def create(self, data):
        if self._key in self._store:
            raise gexc.AlreadyExists(f"{self._key} already exists")
        self._store[self._key] = copy.deepcopy(data)


class FakeCollection:
    def __init__(self, store, name: str):
        self._store = store
        self._name = name

    def document(self, doc_id: str) -> FakeDoc:
        return FakeDoc(self._store, (self._name, str(doc_id)))

    def stream(self):
        return [FakeSnapshot(k[1], v) for k, v in sorted(self._store.items()) if k[0] == self._name]


class FakeDb:
    def __init__(self, store: Optional[Dict[Tuple[str, str], Any]] = None):
        self.store = store if store is not None else {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.store, name)

    def get(self, collection: str, doc_id: str):
        return self.store.get((collection, doc_id))


class FakeBudgets:
    def __init__(self, live: Optional[Dict[str, int]] = None, shared: bool = False, fail_set: bool = False):
        self.live = live if live is not None else {}
        self.shared = shared
        self.fail_set = fail_set
        self.calls: List[Tuple[str, str, int]] = []

    def get_campaign_budget(self, customer_id, campaign_id):
        if campaign_id not in self.live:
            raise RuntimeError("campaign not found")
        return {
            "amountMicros": self.live[campaign_id],
            "resourceName": f"customers/{customer_id}/campaignBudgets/1",
            "explicitlyShared": self.shared,
            "campaignName": "ADSTA Weather PMax Test",
        }

    def set_campaign_budget(self, customer_id, campaign_id, micros):
        self.calls.append((customer_id, campaign_id, micros))
        if self.fail_set:
            return {"success": False, "error": "Failed to update campaign budget: quota"}
        self.live[campaign_id] = micros
        return {"success": True}


def seeded_db() -> FakeDb:
    return FakeDb({
        ("ClimateBaselines", "Vancouver BC"): {"latitude": 49.28, "longitude": -123.12,
                                               "severeThresholds": {"rainMm": 30}},
        ("ClimateBaselines", "Toronto ON"): {"latitude": 43.65, "longitude": -79.38},
        ("CustomerInstructions", CUSTOMER): {"instruction": "x"},
        ("Playbooks", "weather_asset_groups"): {"defaults": {"activation": {
            "coldBelowC": 9.0, "rainRateMmPerH": 0.2, "sunnyMinHours": 3}}},
        ("GoogleAdsConfig", CUSTOMER): {
            "account": "Canada",
            "schedule": {"timezone": "America/Vancouver", "runsPerDay": 2},
            "changeVolumeGuard": {"enabled": True, "maxFractionOfEligibleCampaigns": 0.25},
            "lookAheadHours": 24,
            "trailingWindowHours": 24,
            "assetGroupTokens": {"Cold": "_COLD", "Rain": "_RAIN", "Snow": "_SNOW", "Sunny": "_SUN"},
            "campaigns": [{
                "campaignId": CAMPAIGN,
                "playbooks": ["weather_asset_groups", "severe_budget"],
                "params": {
                    "city": "Vancouver BC", "geo": "VAN", "campaignNameContains": "ADSTA",
                    "hemisphere": "northern", "activation": {"coldBelowC": 1.0},
                    "severeModifiers": {"budgetBumpPct": 50, "maxDailyBudgetMicros": 20_000_000},
                },
            }],
        },
        ("CampaignBudgetState", STATE_ID): {
            "normalBudgetMicros": 10_000_000, "increaseActive": False,
            "lastAppliedBudgetMicros": None, "increasedDays": [],
        },
    })


def run(db, sheet_tabs, budgets=None, customer_filter=None, dry_run=False):
    config = schema.parse_sheet(sheet_tabs, set(sync.load_baselines(db)))
    state = sync.load_state(db, config, customer_filter)
    plan = sync.build_plan(config, state, budgets, customer_filter, sync_id="sync_test")
    return plan, sync.apply_plan(plan, db, budgets, source="test", dry_run=dry_run, now=NOW)


# --- decide_budget --------------------------------------------------------------


def _row(normal_units: float = 12.0) -> schema.CampaignRow:
    return schema.CampaignRow(
        row=2, customer_id=CUSTOMER, campaign_id=CAMPAIGN, active=True, city="Vancouver BC",
        geo=None, campaign_name_contains=None, asset_groups=True, severe_budget=False,
        normal_budget_micros=schema.units_to_micros(normal_units), budget_bump_pct=None,
        max_budget_micros=None,
    )


def _live(micros: int, shared: bool = False) -> Dict[str, Any]:
    return {"amountMicros": micros, "explicitlyShared": shared, "campaignName": "c"}


M = 1_000_000


class TestDecideBudget:
    def test_new_state_is_created_and_pushed(self):
        action = sync.decide_budget(_row(12), None, _live(10 * M))
        assert action.action == sync.BUDGET_PUSH
        assert action.state_exists is False
        assert action.state_update["normalBudgetMicros"] == 12 * M
        assert action.state_update["lastAppliedBudgetMicros"] == 12 * M

    def test_new_state_matching_google_ads_is_recorded(self):
        action = sync.decide_budget(_row(12), None, _live(12 * M))
        assert action.action == sync.BUDGET_RECORD and not action.is_noop
        assert action.state_update["lastAppliedBudgetMicros"] is None  # ADSTA set nothing

    def test_first_sync_pushes_when_google_ads_matches_adsta(self):
        action = sync.decide_budget(_row(12), {"normalBudgetMicros": 10 * M}, _live(10 * M))
        assert action.action == sync.BUDGET_PUSH
        assert action.state_update == {
            "normalBudgetMicros": 12 * M, "sheetNormalBudgetMicros": 12 * M, "lastAppliedBudgetMicros": 12 * M,
        }

    def test_unchanged_records_nothing(self):
        state = {"normalBudgetMicros": 12 * M, "sheetNormalBudgetMicros": 12 * M}
        action = sync.decide_budget(_row(12), state, _live(12 * M))
        assert action.action == sync.BUDGET_RECORD and not action.state_update
        assert action.is_noop and action.message == "unchanged"

    def test_accepting_an_adopted_budget_during_an_increase_says_what_it_records(self):
        # ADSTA adopted a manual 15 and a new increase to 22.50 is running;
        # the operator accepts the 15 in the sheet.
        state = {"normalBudgetMicros": 15 * M, "sheetNormalBudgetMicros": 10 * M,
                 "increaseActive": True, "lastAppliedBudgetMicros": 22_500_000}
        action = sync.decide_budget(_row(15), state, _live(22_500_000))
        assert action.action == sync.BUDGET_RECORD and not action.is_noop
        assert action.message == "ADSTA's normal budget is already 15000000; recording the sheet value"
        # lastAppliedBudgetMicros stays the elevated amount the playbook checks.
        assert action.state_update == {"normalBudgetMicros": 15 * M, "sheetNormalBudgetMicros": 15 * M}

    def test_adsta_adopted_manual_change_and_sheet_is_stale(self):
        # Sheet last wrote 10; ADSTA adopted a manual 15 (case A); sheet still 10.
        state = {"normalBudgetMicros": 15 * M, "sheetNormalBudgetMicros": 10 * M}
        action = sync.decide_budget(_row(10), state, _live(15 * M))
        assert action.action == sync.BUDGET_CONFLICT
        assert "15.00" in action.message

    def test_editing_the_sheet_after_a_conflict_wins(self):
        state = {"normalBudgetMicros": 15 * M, "sheetNormalBudgetMicros": 10 * M}
        action = sync.decide_budget(_row(18), state, _live(15 * M))
        assert action.action == sync.BUDGET_PUSH

    def test_accepting_the_manual_value_resolves_the_conflict(self):
        state = {"normalBudgetMicros": 15 * M, "sheetNormalBudgetMicros": 10 * M}
        action = sync.decide_budget(_row(15), state, _live(15 * M))
        assert action.action == sync.BUDGET_RECORD
        assert action.state_update["sheetNormalBudgetMicros"] == 15 * M

    def test_unadopted_manual_change_in_google_ads_is_a_conflict(self):
        state = {"normalBudgetMicros": 10 * M, "sheetNormalBudgetMicros": 10 * M}
        action = sync.decide_budget(_row(10), state, _live(14 * M))
        assert action.action == sync.BUDGET_CONFLICT
        assert "changed by hand" in action.message

    def test_unadopted_manual_change_is_overridden_once_the_sheet_is_edited(self):
        state = {"normalBudgetMicros": 10 * M, "sheetNormalBudgetMicros": 10 * M}
        action = sync.decide_budget(_row(11), state, _live(14 * M))
        assert action.action == sync.BUDGET_PUSH

    def test_active_increase_only_changes_the_normal_budget(self):
        state = {"normalBudgetMicros": 10 * M, "sheetNormalBudgetMicros": 10 * M,
                 "increaseActive": True, "lastAppliedBudgetMicros": 15 * M}
        action = sync.decide_budget(_row(12), state, _live(15 * M))
        assert action.action == sync.BUDGET_SET_NORMAL
        assert action.state_update == {"normalBudgetMicros": 12 * M, "sheetNormalBudgetMicros": 12 * M}

    def test_manual_change_during_increase_is_a_conflict(self):
        state = {"normalBudgetMicros": 10 * M, "increaseActive": True, "lastAppliedBudgetMicros": 15 * M}
        action = sync.decide_budget(_row(12), state, _live(13 * M))
        assert action.action == sync.BUDGET_CONFLICT

    def test_shared_budget_is_skipped(self):
        action = sync.decide_budget(_row(12), None, _live(10 * M, shared=True))
        assert action.action == sync.BUDGET_SKIP

    def test_unreadable_budget_is_skipped(self):
        action = sync.decide_budget(_row(12), None, None, "permission denied")
        assert action.action == sync.BUDGET_SKIP
        assert "permission denied" in action.message


# --- build_plan / apply_plan ------------------------------------------------------


class TestApply:
    def test_full_sync_writes_config_thresholds_and_budget(self):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        plan, result = run(db, tabs(), budgets)

        assert result.outcome == sync.OUTCOME_APPLIED, plan.describe()
        assert db.get("ClimateBaselines", "Vancouver BC")["activation"] == {
            "coldBelowC": 8.0, "rainRateMmPerH": 0.3, "sunnyMinHours": 4}
        # Code-owned baseline fields are untouched.
        assert db.get("ClimateBaselines", "Vancouver BC")["severeThresholds"] == {"rainMm": 30}

        params = db.get("GoogleAdsConfig", CUSTOMER)["campaigns"][0]["params"]
        assert "activation" not in params  # retired per-campaign override
        assert params["hemisphere"] == "northern"  # unmanaged field preserved
        assert params["severeModifiers"] == {"budgetBumpPct": 50, "maxDailyBudgetMicros": 20 * M}

        assert budgets.calls == []  # Google Ads already matches the sheet
        state = db.get("CampaignBudgetState", STATE_ID)
        assert state["sheetNormalBudgetMicros"] == 10 * M

        log = db.get("ConfigChangeLog", "sync_test")
        fields = {c["field"] for c in log["changes"]}
        assert "activation.coldBelowC" in fields
        assert any("activation" in f for f in fields if f.startswith("campaigns"))

    def test_budget_push_writes_audit_rows(self, caplog):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        sheet = tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5})])
        with caplog.at_level(logging.INFO):
            _, result = run(db, sheet, budgets)

        assert budgets.calls == [(CUSTOMER, str(CAMPAIGN), 12_500_000)]
        assert result.budget_pushes == 1
        assert db.get("CampaignBudgetState", STATE_ID)["normalBudgetMicros"] == 12_500_000
        assert db.get("CampaignBudgetState", STATE_ID)["lastAppliedBudgetMicros"] == 12_500_000
        changelog = [v for (c, _), v in db.store.items() if c == "ChangeLog"]
        assert changelog[0]["budgetBeforeMicros"] == 10 * M
        assert changelog[0]["budgetAfterMicros"] == 12_500_000
        assert changelog[0]["runId"] == "sync_test"  # not counted by the run's change guard
        events = [r for r in caplog.records if getattr(r, "event", None) == "mutation_applied"]
        assert events and events[0].source == "config_sheet"

    def test_second_sync_is_a_noop(self):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        run(db, tabs(), budgets)
        before = copy.deepcopy(db.store)
        plan, result = run(db, tabs(), budgets)
        assert result.outcome == sync.OUTCOME_NOOP, plan.describe()
        assert db.store == before

    def test_invalid_sheet_writes_nothing(self):
        db = seeded_db()
        before = copy.deepcopy(db.store)
        sheet = tabs(cities=[city_row(**{"Cold below C": "nine"})])
        plan, result = run(db, sheet, FakeBudgets({str(CAMPAIGN): 10 * M}))
        assert result.outcome == sync.OUTCOME_INVALID
        assert result.needs_attention
        assert db.store == before
        assert "NOTHING will be written" in plan.describe()

    def test_dry_run_skips_budget_pushes_and_their_state(self):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        sheet = tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5})])
        _, result = run(db, sheet, budgets, dry_run=True)
        assert budgets.calls == []
        assert db.get("CampaignBudgetState", STATE_ID)["normalBudgetMicros"] == 10 * M
        assert db.get("CampaignBudgetState", STATE_ID)["lastAppliedBudgetMicros"] is None
        # Configuration still syncs in dry run.
        assert "activation" in db.get("ClimateBaselines", "Vancouver BC")
        log = db.get("ConfigChangeLog", "sync_test")
        assert log["dryRun"] is True
        assert log["budgets"][0]["result"].startswith("dry_run")

    def test_failed_push_is_partial(self):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M}, fail_set=True)
        sheet = tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5})])
        _, result = run(db, sheet, budgets)
        assert result.outcome == sync.OUTCOME_PARTIAL
        assert db.get("CampaignBudgetState", STATE_ID)["normalBudgetMicros"] == 10 * M

    def test_conflict_is_reported_and_skipped(self):
        db = seeded_db()
        db.store[("CampaignBudgetState", STATE_ID)].update(
            {"normalBudgetMicros": 15 * M, "sheetNormalBudgetMicros": 10 * M})
        budgets = FakeBudgets({str(CAMPAIGN): 15 * M})
        _, result = run(db, tabs(), budgets)
        assert result.conflicts == 1
        assert result.needs_attention
        assert budgets.calls == []
        assert db.get("CampaignBudgetState", STATE_ID)["normalBudgetMicros"] == 15 * M

    def test_inactive_campaign_is_removed_from_config(self):
        db = seeded_db()
        _, result = run(db, tabs(campaigns=[campaign_row(Active="N")]), FakeBudgets({str(CAMPAIGN): 10 * M}))
        assert db.get("GoogleAdsConfig", CUSTOMER)["campaigns"] == []

    def test_blank_city_thresholds_remove_the_field(self):
        db = seeded_db()
        db.store[("ClimateBaselines", "Toronto ON")]["activation"] = {"coldBelowC": 3.0}
        sheet = tabs(cities=[city_row(City="Toronto ON", **{
            "Cold below C": "", "Rain rate mm/h": "", "Sunny min hours": ""})])
        run(db, sheet, None)
        assert "activation" not in db.get("ClimateBaselines", "Toronto ON")

    def test_customer_filter_limits_accounts_but_not_cities(self):
        db = seeded_db()
        before_config = copy.deepcopy(db.get("GoogleAdsConfig", CUSTOMER))
        run(db, tabs(), None, customer_filter="9999999999")
        assert db.get("GoogleAdsConfig", CUSTOMER) == before_config
        assert "activation" in db.get("ClimateBaselines", "Vancouver BC")

    def test_new_account_creates_its_config(self):
        db = seeded_db()
        other = "1112223333"
        sheet = tabs(
            accounts=[account_row(), account_row(**{"Customer ID": other})],
            campaigns=[campaign_row(), campaign_row(**{"Customer ID": other, "Campaign ID": 42,
                                                       "Normal daily budget": ""})],
        )
        plan, _ = run(db, sheet, None)
        assert db.get("GoogleAdsConfig", other)["campaigns"][0]["campaignId"] == 42
        assert any("CustomerInstructions/1112223333" in str(i) for i in plan.issues)


# --- Normal budgets recorded without a push --------------------------------------


def _synced_db(budgets: FakeBudgets) -> FakeDb:
    """A db whose configuration and budget state already match tabs()."""
    db = seeded_db()
    run(db, tabs(), budgets)
    return db


class TestBudgetRecords:
    """A RECORD that writes the normal budget is a change, though Google Ads is untouched."""

    def test_recording_a_new_normal_budget_is_applied_and_reported(self, caplog):
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        db = _synced_db(budgets)
        budgets.live[str(CAMPAIGN)] = 12 * M  # changed to 12 in Google Ads...
        sheet = tabs(campaigns=[campaign_row(**{"Normal daily budget": 12})])  # ...and in the sheet
        with caplog.at_level(logging.INFO):
            plan, result = run(db, sheet, budgets)
            sync.log_result(result, CUSTOMER)

        assert plan.changes == []
        assert result.outcome == sync.OUTCOME_APPLIED, plan.describe()
        assert (result.budget_pushes, result.budget_records) == (0, 1)
        assert budgets.calls == []
        state = db.get("CampaignBudgetState", STATE_ID)
        assert (state["normalBudgetMicros"], state["sheetNormalBudgetMicros"]) == (12 * M, 12 * M)
        assert db.get("ConfigChangeLog", "sync_test")["budgets"][0]["result"] == "recorded"

        text = plan.describe()
        assert "BUDGET RECORD: campaign 24252893412: Google Ads already at 12000000" in text
        assert "No changes" not in text
        event = [r for r in caplog.records if getattr(r, "event", None) == sync.EVENT_CONFIG_SYNC][-1]
        assert (event.outcome, event.budget_records) == (sync.OUTCOME_APPLIED, 1)

        service = FakeSheets()
        sync.append_sync_log(service, "sheet", result, now=NOW, customer_id=CUSTOMER)
        assert service.rows[0][2:4] == [sync.OUTCOME_APPLIED, 1]
        assert "RECORD: campaign 24252893412" in service.rows[0][6]

    def test_budget_already_in_sync_is_a_silent_noop(self):
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        db = _synced_db(budgets)
        plan, result = run(db, tabs(), budgets)

        assert [a.is_noop for a in plan.budget_actions] == [True]
        assert (result.outcome, result.budget_records) == (sync.OUTCOME_NOOP, 0)
        text = plan.describe()
        assert "No changes: Firestore already matches the sheet." in text
        assert "BUDGET" not in text
        service = FakeSheets()
        sync.append_sync_log(service, "sheet", result, now=NOW)
        assert service.rows == []

    def test_new_normal_during_an_increase_is_recorded_not_pushed(self):
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        db = _synced_db(budgets)
        db.store[("CampaignBudgetState", STATE_ID)].update(
            {"increaseActive": True, "lastAppliedBudgetMicros": 15 * M})
        budgets.live[str(CAMPAIGN)] = 15 * M
        plan, result = run(db, tabs(campaigns=[campaign_row(**{"Normal daily budget": 12})]), budgets)

        assert [a.action for a in plan.budget_actions] == [sync.BUDGET_SET_NORMAL]
        assert result.outcome == sync.OUTCOME_APPLIED
        assert (result.budget_pushes, result.budget_records) == (0, 1)
        assert budgets.calls == []
        state = db.get("CampaignBudgetState", STATE_ID)
        assert state["normalBudgetMicros"] == 12 * M
        assert state["lastAppliedBudgetMicros"] == 15 * M  # still the elevated amount ADSTA set


# --- Per-campaign dry run ----------------------------------------------------------


def _without_dry_run_column(sheet: Dict[str, List[List[Any]]]) -> Dict[str, List[List[Any]]]:
    """The sheet as it was before the Dry run column existed (it is last)."""
    sheet[schema.CAMPAIGNS_TAB] = [row[:-1] for row in sheet[schema.CAMPAIGNS_TAB]]
    return sheet


def _stored_campaign(db: FakeDb) -> Dict[str, Any]:
    return db.get("GoogleAdsConfig", CUSTOMER)["campaigns"][0]


class TestCampaignDryRun:
    def test_flag_is_stored_on_the_campaign_not_in_params(self):
        # Params are rendered into the playbook prompt; the model must not
        # know it is in dry run, so the flag stays out of them.
        db = seeded_db()
        run(db, tabs(campaigns=[campaign_row(**{"Dry run": "Y"})]), FakeBudgets({str(CAMPAIGN): 10 * M}))
        campaign = _stored_campaign(db)
        assert campaign["dryRun"] is True
        assert "dryRun" not in campaign["params"]

    @pytest.mark.parametrize("cell", ["N", ""])
    def test_n_or_blank_removes_the_flag(self, cell):
        db = seeded_db()
        _stored_campaign(db)["dryRun"] = True
        run(db, tabs(campaigns=[campaign_row(**{"Dry run": cell})]), FakeBudgets({str(CAMPAIGN): 10 * M}))
        assert "dryRun" not in _stored_campaign(db)

    def test_live_campaign_gets_no_flag(self):
        db = seeded_db()
        run(db, tabs(), FakeBudgets({str(CAMPAIGN): 10 * M}))
        assert "dryRun" not in _stored_campaign(db)

    def test_sheet_without_the_column_keeps_the_stored_flag(self):
        db = seeded_db()
        _stored_campaign(db)["dryRun"] = True
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        sheet = _without_dry_run_column(tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5})]))
        run(db, sheet, budgets)
        assert _stored_campaign(db)["dryRun"] is True
        assert budgets.calls == []  # still in dry run, so the push is held back

    def test_budget_push_is_held_back_for_a_dry_run_campaign(self, caplog):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        sheet = tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5, "Dry run": "Y"})])
        with caplog.at_level(logging.INFO):
            plan, result = run(db, sheet, budgets)

        assert budgets.calls == []
        assert result.budget_pushes == 0
        # No state is written, so the push is still pending when dry run ends.
        assert db.get("CampaignBudgetState", STATE_ID)["normalBudgetMicros"] == 10 * M
        assert not [k for k in db.store if k[0] == "ChangeLog"]
        assert "[campaign in dry run: not applied]" in plan.describe()
        log = db.get("ConfigChangeLog", "sync_test")
        assert log["dryRun"] is False  # the deployment is live
        assert log["budgets"][0]["result"] == "campaign dry run: not applied"
        assert log["budgets"][0]["dryRun"] is True
        assert not [r for r in caplog.records if getattr(r, "event", None) == "mutation_applied"]

    def test_turning_dry_run_off_releases_the_pending_push(self):
        db = seeded_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        run(db, tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5, "Dry run": "Y"})]), budgets)
        _, result = run(db, tabs(campaigns=[campaign_row(**{"Normal daily budget": 12.5, "Dry run": "N"})]), budgets)
        assert budgets.calls == [(CUSTOMER, str(CAMPAIGN), 12_500_000)]
        assert result.budget_pushes == 1
        assert db.get("CampaignBudgetState", STATE_ID)["normalBudgetMicros"] == 12_500_000

    def test_other_campaigns_still_push(self):
        db = two_account_db()
        db.store[("CampaignBudgetState", f"{OTHER}_42")] = {"normalBudgetMicros": 5 * M, "increaseActive": False}
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M, "42": 5 * M})
        sheet = tabs(
            accounts=[account_row(), account_row(**{"Customer ID": OTHER})],
            campaigns=[
                campaign_row(**{"Normal daily budget": 12.5, "Dry run": "Y"}),
                campaign_row(**{"Customer ID": OTHER, "Campaign ID": 42, "Normal daily budget": 6}),
            ],
        )
        run(db, sheet, budgets)
        assert budgets.calls == [(OTHER, "42", 6_000_000)]

    def test_export_shows_the_flag_and_round_trips(self):
        db = seeded_db()
        del _stored_campaign(db)["params"]["activation"]
        assert sync.build_export_rows(db)[schema.CAMPAIGNS_TAB][1][-1] == "N"

        _stored_campaign(db)["dryRun"] = True
        rows = sync.build_export_rows(db)
        assert rows[schema.CAMPAIGNS_TAB][0][-1] == "Dry run"
        assert rows[schema.CAMPAIGNS_TAB][1][-1] == "Y"
        config = schema.parse_sheet(rows, set(sync.load_baselines(db)))
        assert not config.errors, [str(e) for e in config.errors]
        plan = sync.build_plan(config, sync.load_state(db, config, None), None, sync_id="s")
        assert {w.collection for w in plan.writes} == {"ClimateBaselines"}


# --- Several accounts in one sheet ------------------------------------------------

OTHER = "1112223333"
THIRD = "4445556666"


def two_account_tabs(other_campaign: Optional[Dict[str, Any]] = None, **sheet: Any):
    """The seeded account plus OTHER with one campaign (42); overrides apply to OTHER's row."""
    campaign = {"Customer ID": OTHER, "Campaign ID": 42, "Normal daily budget": ""}
    campaign.update(other_campaign or {})
    return tabs(
        accounts=[account_row(), account_row(**{"Customer ID": OTHER})],
        campaigns=[campaign_row(), campaign_row(**campaign)],
        **sheet,
    )


def two_account_db() -> FakeDb:
    db = seeded_db()
    db.store[("CustomerInstructions", OTHER)] = {"instruction": "x"}
    return db


class FakeSheets:
    """Serves the operator tabs to read_sheet and records SyncLog appends."""

    def __init__(self, sheet_tabs: Optional[Dict[str, List[List[Any]]]] = None):
        self.tabs = sheet_tabs or {}
        self.rows: List[List[Any]] = []
        self._response: Dict[str, Any] = {}

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def batchGet(self, spreadsheetId, ranges, valueRenderOption):  # pylint: disable=invalid-name,unused-argument
        self._response = {"valueRanges": [{"values": self.tabs.get(r.strip("'"), [])} for r in ranges]}
        return self

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):  # pylint: disable=invalid-name,redefined-builtin,unused-argument
        self.rows.extend(body["values"])
        self._response = {}
        return self

    def execute(self):
        return self._response


class TestAccountScopedSync:
    """A scheduled run syncs only its own account's rows (plus the shared Cities)."""

    def test_other_accounts_errors_do_not_block_a_scoped_sync(self):
        db = two_account_db()
        budgets = FakeBudgets({str(CAMPAIGN): 10 * M})
        plan, result = run(db, two_account_tabs({"City": "Vancover BC"}), budgets, customer_filter=CUSTOMER)

        assert result.outcome == sync.OUTCOME_APPLIED, plan.describe()
        assert not plan.errors and not result.errors
        assert len(result.other_account_errors) == 1
        assert "Vancover BC" in result.other_account_errors[0]
        assert "(customer 1112223333 only; does not block this sync)" in result.other_account_errors[0]
        assert result.needs_attention
        assert "does not block this sync" in plan.describe()
        assert "NOTHING will be written" not in plan.describe()
        # This account and the shared Cities synced; the broken account was not touched.
        assert "activation" not in db.get("GoogleAdsConfig", CUSTOMER)["campaigns"][0]["params"]
        assert "activation" in db.get("ClimateBaselines", "Vancouver BC")
        assert db.get("GoogleAdsConfig", OTHER) is None

    def test_sync_of_the_broken_account_writes_nothing(self):
        db = two_account_db()
        before = copy.deepcopy(db.store)
        plan, result = run(db, two_account_tabs({"City": "Vancover BC"}), None, customer_filter=OTHER)
        assert result.outcome == sync.OUTCOME_INVALID
        assert result.other_account_errors == []
        assert db.store == before  # not even the shared Cities
        assert "NOTHING will be written" in plan.describe()

    def test_unscoped_sync_is_blocked_by_any_account(self):
        db = two_account_db()
        before = copy.deepcopy(db.store)
        _, result = run(db, two_account_tabs({"City": "Vancover BC"}), None, customer_filter=None)
        assert result.outcome == sync.OUTCOME_INVALID
        assert db.store == before

    def test_cities_error_blocks_every_account(self):
        db = two_account_db()
        before = copy.deepcopy(db.store)
        sheet = two_account_tabs(cities=[city_row(**{"Cold below C": "nine"})])
        for customer in (CUSTOMER, OTHER):
            _, result = run(db, sheet, None, customer_filter=customer)
            assert result.outcome == sync.OUTCOME_INVALID, customer
        assert db.store == before

    def test_unreadable_customer_id_blocks_every_account(self):
        # The row could be one of this account's campaigns: syncing without it
        # would drop that campaign from the account's configuration.
        db = two_account_db()
        before = copy.deepcopy(db.store)
        sheet = two_account_tabs({"Customer ID": "534-111-450"})
        for customer in (CUSTOMER, OTHER):
            _, result = run(db, sheet, None, customer_filter=customer)
            assert result.outcome == sync.OUTCOME_INVALID, customer
        assert db.store == before

    def test_campaign_listed_under_two_accounts_blocks_both_but_not_a_third(self):
        db = two_account_db()
        db.store[("CustomerInstructions", THIRD)] = {"instruction": "x"}
        sheet = tabs(
            accounts=[account_row(), account_row(**{"Customer ID": OTHER}), account_row(**{"Customer ID": THIRD})],
            campaigns=[
                campaign_row(),
                campaign_row(**{"Customer ID": OTHER, "Normal daily budget": ""}),  # same campaign ID
                campaign_row(**{"Customer ID": THIRD, "Campaign ID": 77, "Normal daily budget": ""}),
            ],
        )
        for customer in (CUSTOMER, OTHER):
            _, result = run(db, sheet, None, customer_filter=customer)
            assert result.outcome == sync.OUTCOME_INVALID, customer
        _, result = run(db, sheet, None, customer_filter=THIRD)
        assert result.outcome == sync.OUTCOME_APPLIED
        assert len(result.other_account_errors) == 1
        assert db.get("GoogleAdsConfig", THIRD)["campaigns"][0]["campaignId"] == 77
        assert db.get("GoogleAdsConfig", OTHER) is None

    def test_invalid_sync_lists_its_own_and_other_accounts_errors_separately(self):
        db = two_account_db()
        sheet = tabs(
            accounts=[account_row(), account_row(**{"Customer ID": OTHER})],
            campaigns=[
                campaign_row(City="Vancover BC"),
                campaign_row(**{"Customer ID": OTHER, "Campaign ID": 42, "Active": "maybe"}),
            ],
        )
        _, result = run(db, sheet, None, customer_filter=CUSTOMER)
        assert result.outcome == sync.OUTCOME_INVALID
        assert len(result.errors) == 1 and "Vancover BC" in result.errors[0]
        assert len(result.other_account_errors) == 1 and "not Y or N" in result.other_account_errors[0]

    def test_new_account_without_instructions_points_to_seed_account(self):
        db = seeded_db()  # no CustomerInstructions for OTHER
        plan, _ = run(db, two_account_tabs(), None, customer_filter=OTHER)
        warnings = [i for i in plan.issues if "CustomerInstructions/1112223333" in i.message]
        assert len(warnings) == 1
        assert "seed-account --customer_id 1112223333" in warnings[0].message
        assert warnings[0].customer_ids == (OTHER,)
        assert db.get("GoogleAdsConfig", OTHER)["campaigns"][0]["campaignId"] == 42

    def test_alert_event_counts_other_accounts_errors(self, caplog):
        _, result = run(two_account_db(), two_account_tabs({"Active": "maybe"}), None, customer_filter=CUSTOMER)
        with caplog.at_level(logging.INFO):
            sync.log_result(result, CUSTOMER)
        events = [r for r in caplog.records if getattr(r, "event", None) == sync.EVENT_CONFIG_SYNC]
        assert len(events) == 1
        assert events[0].levelno == logging.WARNING
        assert events[0].needs_attention == "true"
        assert events[0].customer_id == CUSTOMER
        assert (events[0].error_count, events[0].other_account_error_count) == (0, 1)
        assert "does not block this sync" in events[0].getMessage()


class TestSyncLog:
    def test_noop_with_other_accounts_errors_is_logged_with_its_scope(self):
        db = two_account_db()
        sheet = two_account_tabs({"Active": "maybe"})
        run(db, sheet, None, customer_filter=CUSTOMER)
        _, result = run(db, sheet, None, customer_filter=CUSTOMER)
        assert result.outcome == sync.OUTCOME_NOOP

        service = FakeSheets()
        sync.append_sync_log(service, "sheet", result, now=NOW, customer_id=CUSTOMER)
        assert len(service.rows) == 1
        row = service.rows[0]
        assert row[:6] == ["2026-10-01 13:00:00", "test (5341114500)", sync.OUTCOME_NOOP, 0, 0, 1]
        assert "does not block this sync" in row[6]

    def test_clean_noop_is_not_logged(self):
        db = seeded_db()
        run(db, tabs(), None, customer_filter=CUSTOMER)
        _, result = run(db, tabs(), None, customer_filter=CUSTOMER)
        service = FakeSheets()
        sync.append_sync_log(service, "sheet", result, now=NOW, customer_id=CUSTOMER)
        assert service.rows == []

    def test_unscoped_sync_keeps_the_plain_source(self):
        _, result = run(seeded_db(), tabs(), None)
        service = FakeSheets()
        sync.append_sync_log(service, "sheet", result, now=NOW)
        assert service.rows[0][1] == "test"


class TestRunSheetSync:
    def test_never_raises_and_reports_an_error(self, caplog):
        def broken():
            raise RuntimeError("403 caller does not have permission")

        with caplog.at_level(logging.INFO):
            result = sync.run_sheet_sync("sheet", seeded_db(), CUSTOMER, service_factory=broken,
                                         budgets=FakeBudgets())
        assert result.outcome == sync.OUTCOME_ERROR
        events = [r for r in caplog.records if getattr(r, "event", None) == sync.EVENT_CONFIG_SYNC]
        assert events and events[0].needs_attention == "true"

    def test_scheduled_sync_is_scoped_to_the_runs_account(self, caplog):
        db = two_account_db()
        service = FakeSheets(two_account_tabs({"City": "Vancover BC"}))
        with caplog.at_level(logging.INFO):
            result = sync.run_sheet_sync("sheet", db, CUSTOMER, service_factory=lambda: service,
                                         budgets=FakeBudgets({str(CAMPAIGN): 10 * M}))
        assert result.outcome == sync.OUTCOME_APPLIED
        assert result.needs_attention
        assert db.get("GoogleAdsConfig", OTHER) is None
        assert service.rows[0][1] == "scheduled_run (5341114500)"
        events = [r for r in caplog.records if getattr(r, "event", None) == sync.EVENT_CONFIG_SYNC]
        assert events[0].customer_id == CUSTOMER and events[0].other_account_error_count == 1


class TestCopyCustomerInstructions:
    def test_copies_a_working_accounts_instructions(self, caplog):
        db = seeded_db()
        with caplog.at_level(logging.INFO):
            assert sync.copy_customer_instructions(db, CUSTOMER, "111-222-3333") is True
        assert db.get("CustomerInstructions", OTHER) == {"instruction": "x"}
        assert any("Audit: created CustomerInstructions/1112223333" in r.getMessage() for r in caplog.records)

    def test_never_overwrites_an_existing_document(self):
        db = seeded_db()
        db.store[("CustomerInstructions", OTHER)] = {"instruction": "custom"}
        assert sync.copy_customer_instructions(db, CUSTOMER, OTHER) is False
        assert db.get("CustomerInstructions", OTHER) == {"instruction": "custom"}

    @pytest.mark.parametrize("source, target", [
        (CUSTOMER, "534-111-4500"),  # same account
        ("9998887777", OTHER),  # source missing
    ])
    def test_rejects_an_unusable_source(self, source, target):
        db = seeded_db()
        with pytest.raises(ValueError):
            sync.copy_customer_instructions(db, source, target)
        assert db.get("CustomerInstructions", OTHER) is None

    def test_rejects_a_source_without_instruction_text(self):
        db = seeded_db()
        db.store[("CustomerInstructions", CUSTOMER)] = {"instruction": ""}
        with pytest.raises(ValueError, match="no instruction"):
            sync.copy_customer_instructions(db, CUSTOMER, OTHER)


# --- Export round trip ----------------------------------------------------------------


class TestExport:
    def test_export_parses_back_without_changes(self):
        db = seeded_db()
        del db.store[("GoogleAdsConfig", CUSTOMER)]["campaigns"][0]["params"]["activation"]
        rows = sync.build_export_rows(db)
        config = schema.parse_sheet(rows, set(sync.load_baselines(db)))
        assert not config.errors, [str(e) for e in config.errors]

        # Cities show effective thresholds (playbook defaults here).
        assert rows[schema.CITIES_TAB][1][:4] == ["Toronto ON", 9.0, 0.2, 3]
        assert rows[schema.CAMPAIGNS_TAB][1][8] == 10.0  # normal budget in currency

        state = sync.load_state(db, config, None)
        plan = sync.build_plan(config, state, None, sync_id="s")
        # Only the cities change: their effective defaults become explicit.
        assert {w.collection for w in plan.writes} == {"ClimateBaselines"}

    def test_export_refuses_to_overwrite_data(self):
        class Service:
            def spreadsheets(self):
                return self

            def get(self, **_):
                return self

            def values(self):
                return self

            def execute(self):
                return {"sheets": [{"properties": {"title": t, "sheetId": i}}
                                   for i, t in enumerate([schema.ACCOUNTS_TAB])],
                        "values": [["h"], ["data"]]}

        with pytest.raises(RuntimeError, match="already has 1 data row"):
            sync.export_to_sheet(Service(), "id", sync.build_export_rows(seeded_db()),
                                 tabs=[schema.ACCOUNTS_TAB])
