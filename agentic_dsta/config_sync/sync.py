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
"""Syncs the ADSTA configuration sheet into Firestore.

The sheet is the operator's source of truth for three things:

* per-city asset group activation thresholds -> ``ClimateBaselines/{city}.activation``
* account and campaign settings              -> ``GoogleAdsConfig/{customerId}``
* each campaign's normal daily budget        -> ``CampaignBudgetState`` and,
  when no severe-weather increase is active, the live Google Ads budget

A sync is planned first (read-only: sheet, Firestore and live budgets) and then
applied. The plan is all-or-nothing on validation: one invalid row and nothing
is written, so runs keep using the last good configuration.

Budget conflicts
----------------
ADSTA itself changes budgets (severe-weather increases) and adopts manual
changes made in Google Ads while an increase is active. The sheet must not
silently undo a human decision made in Google Ads, so a budget is only pushed
when Google Ads still shows the value ADSTA last knew about. Otherwise the
campaign is reported as a conflict and skipped until the sheet is updated to
match (or Google Ads is changed back). ``CampaignBudgetState.sheetNormalBudgetMicros``
records the last value the sync wrote, which is how a change made outside the
sheet is detected.

Audit trail
-----------
Every applied sync writes one ``ConfigChangeLog/{syncId}`` document listing
each field change, budget action and conflict. Budget pushes also write a
``ChangeLog`` row (with a ``sync_`` run id, so they are not counted by the
change volume guard of the decision run) and a ``mutation_applied`` event.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import logging
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Set

from agentic_dsta.config_sync import schema
from agentic_dsta.core import activation as activation_lib
from agentic_dsta.core import telemetry
from agentic_dsta.core.dry_run import is_dry_run

logger = logging.getLogger(__name__)

# Environment variable holding the spreadsheet id. When unset, scheduled runs
# skip the sync and use Firestore as it is.
CONFIG_SHEET_ID_ENV = "CONFIG_SHEET_ID"

EVENT_CONFIG_SYNC = "config_sync"

OUTCOME_APPLIED = "applied"
OUTCOME_NOOP = "noop"
OUTCOME_INVALID = "invalid"
OUTCOME_ERROR = "error"
OUTCOME_PARTIAL = "partial"
OUTCOME_PLANNED = "planned"

# Budget actions.
BUDGET_PUSH = "push"                # write Google Ads + state
BUDGET_SET_NORMAL = "set_normal"    # write state only (increase active)
BUDGET_RECORD = "record"            # bookkeeping only, nothing visible changes
BUDGET_CONFLICT = "conflict"        # changed outside the sheet; skipped
BUDGET_SKIP = "skip"                # cannot act (read failure, shared budget)

_FALLBACK_ACTIVATION = {"coldBelowC": 9.0, "rainRateMmPerH": 0.2, "sunnyMinHours": 3}


# --- Interfaces -------------------------------------------------------------


class BudgetGateway(Protocol):
    """Reads and writes a campaign's live Google Ads budget."""

    def get_campaign_budget(self, customer_id: str, campaign_id: str) -> Dict[str, Any]:
        """Returns amountMicros, resourceName, explicitlyShared and campaignName.

        Raises:
            Exception: If the budget cannot be read.
        """

    def set_campaign_budget(self, customer_id: str, campaign_id: str, micros: int) -> Dict[str, Any]:
        """Sets the budget. Returns the updater tool's result dict."""


class GoogleAdsBudgetGateway:
    """BudgetGateway backed by the Google Ads API."""

    def get_campaign_budget(self, customer_id: str, campaign_id: str) -> Dict[str, Any]:
        """Reads the campaign's budget with one GAQL query.

        Args:
            customer_id: Google Ads customer ID, digits only.
            campaign_id: Campaign ID.

        Returns:
            Mapping with amountMicros, resourceName, explicitlyShared and
            campaignName.

        Raises:
            RuntimeError: If the client is unavailable or the campaign has no
                budget.
        """
        # Imported lazily: the Google Ads SDK is heavy and unit tests and the
        # export command do not need it.
        from agentic_dsta.tools.google_ads.google_ads_client import get_google_ads_client  # pylint: disable=import-outside-toplevel

        client = get_google_ads_client(customer_id)
        if not client:
            raise RuntimeError("Failed to get Google Ads client.")
        query = (
            "SELECT campaign.name, campaign_budget.resource_name, "
            "campaign_budget.amount_micros, campaign_budget.explicitly_shared "
            f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
        )
        service = client.get_service("GoogleAdsService")
        for batch in service.search_stream(customer_id=customer_id, query=query):
            for row in batch.results:
                return {
                    "amountMicros": int(row.campaign_budget.amount_micros),
                    "resourceName": row.campaign_budget.resource_name,
                    "explicitlyShared": bool(row.campaign_budget.explicitly_shared),
                    "campaignName": row.campaign.name,
                }
        raise RuntimeError(f"Campaign {campaign_id} not found or has no budget.")

    def set_campaign_budget(self, customer_id: str, campaign_id: str, micros: int) -> Dict[str, Any]:
        """Sets the budget through the same updater the agent uses.

        The updater honours ADSTA_DRY_RUN, so a dry run never changes the
        account even if this is called.
        """
        from agentic_dsta.tools.google_ads.google_ads_updater import update_google_ads_campaign_budget  # pylint: disable=import-outside-toplevel

        return update_google_ads_campaign_budget(customer_id, str(campaign_id), int(micros))


# --- Plan model -------------------------------------------------------------


@dataclasses.dataclass
class Change:
    """One field-level configuration change, for display and the audit log."""

    collection: str
    document_id: str
    field: str
    old: Any
    new: Any

    def __str__(self) -> str:
        return f"{self.collection}/{self.document_id} {self.field}: {self.old!r} -> {self.new!r}"

    def to_dict(self) -> Dict[str, Any]:
        """Firestore-safe representation."""
        return {
            "collection": self.collection,
            "documentId": self.document_id,
            "field": self.field,
            "old": _firestore_safe(self.old),
            "new": _firestore_safe(self.new),
        }


@dataclasses.dataclass
class DocWrite:
    """A Firestore write. ``mode`` is 'set' (replace) or 'update' (fields)."""

    collection: str
    document_id: str
    data: Dict[str, Any]
    mode: str = "set"


@dataclasses.dataclass
class BudgetAction:
    """What the sync will do with one campaign's normal daily budget."""

    customer_id: str
    campaign_id: int
    city: str
    action: str
    sheet_micros: int
    message: str
    live_micros: Optional[int] = None
    normal_micros: Optional[int] = None
    increase_active: bool = False
    campaign_name: Optional[str] = None
    state_update: Dict[str, Any] = dataclasses.field(default_factory=dict)
    state_exists: bool = True

    def __str__(self) -> str:
        return f"{self.action.upper()}: campaign {self.campaign_id}: {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        """Firestore-safe representation."""
        return {
            "customerId": self.customer_id,
            "campaignId": str(self.campaign_id),
            "action": self.action,
            "sheetMicros": self.sheet_micros,
            "liveMicros": self.live_micros,
            "normalMicros": self.normal_micros,
            "increaseActive": self.increase_active,
            "message": self.message,
        }


@dataclasses.dataclass
class SyncPlan:
    """Everything a sync would do, computed without writing anything."""

    sync_id: str
    customer_filter: Optional[str]
    issues: List[schema.Issue] = dataclasses.field(default_factory=list)
    writes: List[DocWrite] = dataclasses.field(default_factory=list)
    changes: List[Change] = dataclasses.field(default_factory=list)
    budget_actions: List[BudgetAction] = dataclasses.field(default_factory=list)

    @property
    def errors(self) -> List[schema.Issue]:
        """Blocking validation errors."""
        return [i for i in self.issues if i.severity == schema.ERROR]

    @property
    def conflicts(self) -> List[BudgetAction]:
        """Budgets changed outside the sheet, or that cannot be acted on."""
        return [a for a in self.budget_actions if a.action in (BUDGET_CONFLICT, BUDGET_SKIP)]

    @property
    def visible_budget_actions(self) -> List[BudgetAction]:
        """Budget actions that change something an operator would notice."""
        return [a for a in self.budget_actions if a.action in (BUDGET_PUSH, BUDGET_SET_NORMAL)]

    def describe(self) -> str:
        """Multi-line, human readable summary of the plan."""
        lines = [f"Config sheet sync plan {self.sync_id}"]
        if self.customer_filter:
            lines.append(f"  scope: customer {self.customer_filter} (cities: all)")
        for issue in self.issues:
            lines.append(f"  {issue}")
        if self.errors:
            lines.append(f"  {len(self.errors)} error(s): NOTHING will be written.")
            return "\n".join(lines)
        if not self.changes and not self.budget_actions:
            lines.append("  No changes: Firestore already matches the sheet.")
        for change in self.changes:
            lines.append(f"  CHANGE {change}")
        for action in self.budget_actions:
            if action.action != BUDGET_RECORD:
                lines.append(f"  BUDGET {action}")
        return "\n".join(lines)


@dataclasses.dataclass
class SyncResult:
    """Outcome of applying (or attempting) a sync."""

    sync_id: str
    outcome: str
    source: str
    changes: int = 0
    budget_pushes: int = 0
    conflicts: int = 0
    errors: List[str] = dataclasses.field(default_factory=list)
    plan: Optional[SyncPlan] = None

    @property
    def needs_attention(self) -> bool:
        """True when an operator should look at the sheet or the logs."""
        return self.outcome in (OUTCOME_INVALID, OUTCOME_ERROR, OUTCOME_PARTIAL) or self.conflicts > 0


@dataclasses.dataclass
class FirestoreState:
    """The Firestore documents a sync compares the sheet against."""

    baselines: Dict[str, Dict[str, Any]]
    ads_configs: Dict[str, Optional[Dict[str, Any]]]
    budget_states: Dict[str, Optional[Dict[str, Any]]]
    instructions_present: Dict[str, bool]


# --- Helpers ----------------------------------------------------------------


def _firestore_safe(value: Any) -> Any:
    """Makes a value storable in a Firestore document field."""
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def new_sync_id(now: Optional[datetime.datetime] = None) -> str:
    """Returns an id such as 'sync_2026-10-01T11:00:00Z_1a2b3c4d'."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return f"sync_{now.strftime('%Y-%m-%dT%H:%M:%SZ')}_{uuid.uuid4().hex[:8]}"


def budget_state_id(customer_id: str, campaign_id: Any) -> str:
    """The CampaignBudgetState document id for a campaign."""
    return f"{customer_id}_{campaign_id}"


def diff_values(old: Any, new: Any, path: str = "") -> List[tuple]:
    """Lists field-level differences between two document values.

    Dicts are compared key by key. The ``campaigns`` list is compared by
    campaignId so that reordering rows does not show as a change. Any other
    list is compared as a whole.

    Args:
        old: The current value.
        new: The desired value.
        path: Dotted path of the value, used in the output.

    Returns:
        A list of (path, old, new) tuples.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        out: List[tuple] = []
        for key in sorted(set(old) | set(new), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in new:
                out.append((child, old[key], None))
            elif key not in old:
                out.append((child, None, new[key]))
            else:
                out.extend(diff_values(old[key], new[key], child))
        return out
    if path.endswith("campaigns") and isinstance(old, list) and isinstance(new, list):
        old_by_id = {str(c.get("campaignId")): c for c in old if isinstance(c, dict)}
        new_by_id = {str(c.get("campaignId")): c for c in new if isinstance(c, dict)}
        out = []
        for cid in sorted(set(old_by_id) | set(new_by_id)):
            child = f"{path}[{cid}]"
            if cid not in new_by_id:
                out.append((child, "present", "removed"))
            elif cid not in old_by_id:
                out.append((child, None, "added"))
            else:
                out.extend(diff_values(old_by_id[cid], new_by_id[cid], child))
        return out
    if old != new:
        return [(path, old, new)]
    return []


def desired_ads_config(
    account: schema.AccountRow,
    campaigns: Iterable[schema.CampaignRow],
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Builds the GoogleAdsConfig document the sheet describes.

    Fields the sheet does not manage are carried over from the existing
    document, both at account level and inside each campaign's params (for
    example ``hemisphere``), so the sheet never erases settings it cannot show.
    The retired per-campaign ``params.activation`` is dropped.

    Args:
        account: The account row.
        campaigns: That account's campaign rows.
        existing: The current GoogleAdsConfig data, or None.

    Returns:
        The full desired document.
    """
    doc = copy.deepcopy(existing or {})
    doc.pop("weatherConditionsId", None)
    if account.account:
        doc["account"] = account.account
    schedule = dict(doc.get("schedule") or {})
    schedule["timezone"] = account.timezone
    schedule["runsPerDay"] = account.runs_per_day
    doc["schedule"] = schedule
    doc["changeVolumeGuard"] = {
        "enabled": account.guard_enabled,
        "maxFractionOfEligibleCampaigns": account.guard_max_fraction,
    }
    doc["lookAheadHours"] = account.look_ahead_hours
    doc["trailingWindowHours"] = account.trailing_window_hours
    doc["assetGroupTokens"] = dict(account.tokens)

    previous = {
        str(c.get("campaignId")): c
        for c in (existing or {}).get("campaigns", []) or []
        if isinstance(c, dict)
    }
    entries: List[Dict[str, Any]] = []
    for row in campaigns:
        if not row.active:
            continue
        entry = copy.deepcopy(previous.get(str(row.campaign_id), {}))
        entry.pop("playbook", None)
        entry.pop("instruction", None)
        entry["campaignId"] = row.campaign_id
        entry["playbooks"] = row.playbooks
        params = dict(entry.get("params") or {})
        params.pop("activation", None)
        params["city"] = row.city
        for key, value in (("geo", row.geo), ("campaignNameContains", row.campaign_name_contains)):
            if value:
                params[key] = value
            else:
                params.pop(key, None)
        if row.severe_budget:
            params["severeModifiers"] = {
                "budgetBumpPct": int(row.budget_bump_pct)
                if float(row.budget_bump_pct).is_integer()
                else row.budget_bump_pct,
                "maxDailyBudgetMicros": row.max_budget_micros,
            }
        else:
            params.pop("severeModifiers", None)
        entry["params"] = params
        entries.append(entry)
    doc["campaigns"] = entries
    return doc


def decide_budget(
    row: schema.CampaignRow,
    state: Optional[Dict[str, Any]],
    live: Optional[Dict[str, Any]],
    live_error: Optional[str] = None,
) -> BudgetAction:
    """Decides what to do with one campaign's normal daily budget.

    Args:
        row: The campaign row (normal_budget_micros must be set).
        state: The CampaignBudgetState data, or None if it does not exist.
        live: The live budget from Google Ads, or None if it could not be read.
        live_error: Why the live budget could not be read.

    Returns:
        The BudgetAction, including the state fields to write.
    """
    sheet = int(row.normal_budget_micros)
    base = dict(customer_id=row.customer_id, campaign_id=row.campaign_id, city=row.city, sheet_micros=sheet)
    live_micros = live.get("amountMicros") if live else None
    campaign_name = live.get("campaignName") if live else None

    if live is None:
        return BudgetAction(
            action=BUDGET_SKIP,
            message=f"could not read the live Google Ads budget ({live_error or 'unknown error'}); budget not synced",
            **base,
        )
    if live.get("explicitlyShared"):
        return BudgetAction(
            action=BUDGET_SKIP,
            live_micros=live_micros,
            campaign_name=campaign_name,
            message="the campaign uses a shared budget; ADSTA only manages per-campaign budgets",
            **base,
        )

    if state is None:
        update = {
            "customerId": row.customer_id,
            "campaignId": str(row.campaign_id),
            "weatherLocation": row.city,
            "normalBudgetMicros": sheet,
            "sheetNormalBudgetMicros": sheet,
            "increaseActive": False,
            "increaseStartDate": None,
            "increaseDayNumber": 0,
            "lastAppliedBudgetMicros": None,
            "increasedDays": [],
            "lastRunDate": None,
        }
        action = BUDGET_RECORD if live_micros == sheet else BUDGET_PUSH
        return BudgetAction(
            action=action,
            live_micros=live_micros,
            campaign_name=campaign_name,
            state_update=update,
            state_exists=False,
            message=(
                f"new budget state; normal budget {sheet}"
                + ("" if action == BUDGET_RECORD else f", Google Ads {live_micros} -> {sheet}")
            ),
            **base,
        )

    normal = state.get("normalBudgetMicros")
    last_synced = state.get("sheetNormalBudgetMicros")
    increase_active = bool(state.get("increaseActive"))
    last_applied = state.get("lastAppliedBudgetMicros")
    common = dict(
        live_micros=live_micros,
        normal_micros=normal,
        increase_active=increase_active,
        campaign_name=campaign_name,
        **base,
    )
    # The budget moved since the sheet last wrote it (ADSTA adopted a manual
    # Google Ads change). That is only a conflict while the sheet still shows
    # its old value: once an operator edits the cell, the edit is the newer,
    # deliberate decision and the sheet wins.
    changed_outside_sheet = last_synced is not None and normal != last_synced
    sheet_edited = last_synced is not None and sheet != last_synced
    stale_sheet = changed_outside_sheet and not sheet_edited
    record = {} if (normal == sheet and last_synced == sheet) else {
        "normalBudgetMicros": sheet,
        "sheetNormalBudgetMicros": sheet,
    }
    accept_hint = "Set the sheet to {:.2f} to accept it, or enter a new value to override it."

    if increase_active:
        if last_applied is not None and live_micros != last_applied:
            return BudgetAction(
                action=BUDGET_CONFLICT,
                message=(
                    f"Google Ads budget {live_micros} was changed by hand during a severe-weather "
                    f"increase (ADSTA set {last_applied}); ADSTA adopts it on its next run. "
                    "Update the sheet once that has happened."
                ),
                **common,
            )
        if normal == sheet:
            return BudgetAction(action=BUDGET_RECORD, state_update=record, message="unchanged", **common)
        if stale_sheet:
            return BudgetAction(
                action=BUDGET_CONFLICT,
                message=(
                    f"ADSTA's normal budget is {normal} but the sheet still shows {last_synced}, the value "
                    "it last set; the budget was changed outside the sheet. "
                    + accept_hint.format(normal / 1e6)
                ),
                **common,
            )
        return BudgetAction(
            action=BUDGET_SET_NORMAL,
            state_update=record,
            message=(
                f"normal budget {normal} -> {sheet}; a severe-weather increase is active, so Google Ads "
                "keeps the elevated budget and returns to the new normal when the increase ends"
            ),
            **common,
        )

    if live_micros == sheet:
        return BudgetAction(
            action=BUDGET_RECORD,
            state_update=record,
            message="unchanged" if not record else f"Google Ads already at {sheet}; recording it as the normal budget",
            **common,
        )
    push = BudgetAction(
        action=BUDGET_PUSH,
        state_update={"normalBudgetMicros": sheet, "sheetNormalBudgetMicros": sheet},
        message=f"Google Ads budget {live_micros} -> {sheet}",
        **common,
    )
    if live_micros == normal:
        if stale_sheet:
            return BudgetAction(
                action=BUDGET_CONFLICT,
                message=(
                    f"Google Ads and ADSTA show {normal}, but the sheet still shows {last_synced}, the value "
                    "it last set; the budget was changed outside the sheet. "
                    + accept_hint.format(normal / 1e6)
                ),
                **common,
            )
        return push
    # Google Ads matches neither the sheet nor ADSTA's normal budget: someone
    # changed it by hand and ADSTA has not adopted it (it only does so during an
    # increase). Push only if the operator has edited the sheet since then.
    if sheet_edited:
        push.message += " (sheet edited; overriding a budget changed by hand in Google Ads)"
        return push
    return BudgetAction(
        action=BUDGET_CONFLICT,
        message=(
            f"Google Ads budget is {live_micros}, which matches neither the sheet ({sheet}) nor ADSTA's "
            f"normal budget ({normal}); it was probably changed by hand in Google Ads. "
            + accept_hint.format(live_micros / 1e6)
        ),
        **common,
    )


# --- Firestore and sheet I/O -------------------------------------------------


def load_state(db: Any, sheet: schema.SheetConfig, customer_filter: Optional[str]) -> FirestoreState:
    """Reads the Firestore documents the plan needs.

    Args:
        db: A google.cloud.firestore.Client.
        sheet: The parsed sheet.
        customer_filter: Restrict accounts and campaigns to this customer.

    Returns:
        The FirestoreState.
    """
    customers = [c for c in sheet.accounts if not customer_filter or c == customer_filter]
    ads_configs: Dict[str, Optional[Dict[str, Any]]] = {}
    instructions: Dict[str, bool] = {}
    for customer_id in customers:
        snap = db.collection("GoogleAdsConfig").document(customer_id).get()
        ads_configs[customer_id] = snap.to_dict() if snap.exists else None
        instructions[customer_id] = db.collection("CustomerInstructions").document(customer_id).get().exists

    budget_states: Dict[str, Optional[Dict[str, Any]]] = {}
    for row in sheet.campaigns:
        if row.customer_id in customers and row.active and row.normal_budget_micros is not None:
            doc_id = budget_state_id(row.customer_id, row.campaign_id)
            snap = db.collection("CampaignBudgetState").document(doc_id).get()
            budget_states[doc_id] = snap.to_dict() if snap.exists else None

    return FirestoreState(
        baselines=load_baselines(db),
        ads_configs=ads_configs,
        budget_states=budget_states,
        instructions_present=instructions,
    )


def load_baselines(db: Any) -> Dict[str, Dict[str, Any]]:
    """Returns every ClimateBaselines document keyed by city."""
    return {snap.id: snap.to_dict() or {} for snap in db.collection("ClimateBaselines").stream()}


def read_sheet(service: Any, spreadsheet_id: str) -> Dict[str, List[List[Any]]]:
    """Reads the three operator tabs in one batch call.

    Args:
        service: A Sheets API v4 service.
        spreadsheet_id: The spreadsheet id (from its URL).

    Returns:
        Cell grid per tab name.

    Raises:
        googleapiclient.errors.HttpError: If the sheet or a tab is missing, or
            the caller has no access.
    """
    tabs = [schema.ACCOUNTS_TAB, schema.CAMPAIGNS_TAB, schema.CITIES_TAB]
    response = (
        service.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[f"'{tab}'" for tab in tabs],
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    ranges = response.get("valueRanges", [])
    return {tab: (ranges[i].get("values", []) if i < len(ranges) else []) for i, tab in enumerate(tabs)}


# --- Plan -------------------------------------------------------------------


def build_plan(
    sheet: schema.SheetConfig,
    state: FirestoreState,
    budgets: Optional[BudgetGateway],
    customer_filter: Optional[str] = None,
    sync_id: Optional[str] = None,
) -> SyncPlan:
    """Computes every write the sheet implies, without writing anything.

    Args:
        sheet: The parsed sheet.
        state: Current Firestore documents.
        budgets: Reads live budgets. None skips budget syncing entirely.
        customer_filter: Restrict accounts and campaigns to one customer.
            Cities are always synced because they are shared by all accounts.
        sync_id: Override the generated id (tests).

    Returns:
        The SyncPlan. If it has errors it contains no writes.
    """
    plan = SyncPlan(sync_id=sync_id or new_sync_id(), customer_filter=customer_filter, issues=list(sheet.issues))
    if customer_filter and customer_filter not in sheet.accounts and not plan.errors:
        plan.issues.append(
            schema.Issue(
                schema.WARNING, schema.ACCOUNTS_TAB, 0, "",
                f"customer {customer_filter} is not in the sheet; its account and campaigns were not synced",
            )
        )
    if plan.errors:
        return plan

    # Cities: activation thresholds.
    for city, row in sorted(sheet.cities.items()):
        current = (state.baselines.get(city) or {}).get("activation")
        desired = dict(row.activation) or None
        if current == desired:
            continue
        for key in activation_lib.ACTIVATION_LIMITS:
            old = (current or {}).get(key) if isinstance(current, dict) else None
            new = (desired or {}).get(key)
            if old != new:
                plan.changes.append(Change("ClimateBaselines", city, f"activation.{key}", old, new))
        plan.writes.append(DocWrite("ClimateBaselines", city, {"activation": desired}, mode="update"))

    # Accounts and campaigns.
    for customer_id, account in sorted(sheet.accounts.items()):
        if customer_filter and customer_id != customer_filter:
            continue
        existing = state.ads_configs.get(customer_id)
        desired = desired_ads_config(account, sheet.campaigns_for(customer_id), existing)
        if not state.instructions_present.get(customer_id, True):
            plan.issues.append(
                schema.Issue(
                    schema.WARNING, schema.ACCOUNTS_TAB, account.row, "Customer ID",
                    f"CustomerInstructions/{customer_id} does not exist, so runs for this account abort. "
                    "Seed it with upload_config.py.",
                )
            )
        if existing == desired:
            continue
        if existing is None:
            plan.changes.append(Change("GoogleAdsConfig", customer_id, "(document)", None, "created"))
        else:
            for path, old, new in diff_values(existing, desired):
                plan.changes.append(Change("GoogleAdsConfig", customer_id, path, old, new))
        plan.writes.append(DocWrite("GoogleAdsConfig", customer_id, desired, mode="set"))

    # Budgets.
    if budgets is None:
        return plan
    for row in sheet.campaigns:
        if customer_filter and row.customer_id != customer_filter:
            continue
        if not row.active or row.normal_budget_micros is None:
            continue
        live, live_error = None, None
        try:
            live = budgets.get_campaign_budget(row.customer_id, str(row.campaign_id))
        except Exception as err:  # pylint: disable=broad-except
            live_error = str(err)[:300]
        state_doc = state.budget_states.get(budget_state_id(row.customer_id, row.campaign_id))
        plan.budget_actions.append(decide_budget(row, state_doc, live, live_error))
    return plan


# --- Apply ------------------------------------------------------------------


def _write_budget_changelog(db: Any, plan: SyncPlan, action: BudgetAction, mode: str, now: datetime.datetime) -> None:
    """Writes the ChangeLog row for a budget push, like the playbooks do."""
    doc_id = f"{now.strftime('%Y-%m-%d')}_{action.campaign_id}_sheet_budget_{plan.sync_id}"
    db.collection("ChangeLog").document(doc_id).set({
        "runId": plan.sync_id,
        "timestampUtc": now.isoformat(),
        "customerId": action.customer_id,
        "campaignId": str(action.campaign_id),
        "campaignName": action.campaign_name,
        "weatherLocation": action.city,
        "condition": "None",
        "assetGroupAction": "no change",
        "severityMet": False,
        "triggerDetail": "normal daily budget changed in the config sheet",
        "budgetBeforeMicros": action.live_micros,
        "budgetAfterMicros": action.sheet_micros,
        "mode": mode,
        "notes": "config sheet sync",
        "source": "config_sheet",
    })


def apply_plan(
    plan: SyncPlan,
    db: Any,
    budgets: Optional[BudgetGateway],
    source: str,
    dry_run: Optional[bool] = None,
    now: Optional[datetime.datetime] = None,
) -> SyncResult:
    """Applies a plan built by :func:`build_plan`.

    Configuration writes always happen (they are ADSTA's own state). Budget
    pushes respect dry run: with ADSTA_DRY_RUN on they are reported and
    skipped, and their state is not written either, so the next live sync
    still sees the difference and pushes it.

    Args:
        plan: The plan.
        db: A google.cloud.firestore.Client.
        budgets: The budget gateway used to build the plan.
        source: Who triggered the sync, e.g. 'scheduled_run' or 'cli'.
        dry_run: Override the ADSTA_DRY_RUN check (tests).
        now: Override the current time (tests).

    Returns:
        The SyncResult.
    """
    dry_run = is_dry_run() if dry_run is None else dry_run
    now = now or datetime.datetime.now(datetime.timezone.utc)
    result = SyncResult(sync_id=plan.sync_id, outcome=OUTCOME_NOOP, source=source, plan=plan,
                        conflicts=len(plan.conflicts))
    if plan.errors:
        result.outcome = OUTCOME_INVALID
        result.errors = [str(i) for i in plan.errors]
        return result

    from google.cloud import firestore as firestore_module  # pylint: disable=import-outside-toplevel

    for write in plan.writes:
        ref = db.collection(write.collection).document(write.document_id)
        if write.mode == "update":
            data = {
                k: (firestore_module.DELETE_FIELD if v is None else v)
                for k, v in write.data.items()
            }
            ref.update(data)
        else:
            ref.set(write.data)
    for change in plan.changes:
        logger.info(
            "Config sheet change %s", change,
            extra={"sync_id": plan.sync_id, "collection": change.collection, "document_id": change.document_id},
        )
    result.changes = len(plan.changes)

    budget_log: List[Dict[str, Any]] = []
    failed = False
    for action in plan.budget_actions:
        entry = action.to_dict()
        state_ref = db.collection("CampaignBudgetState").document(
            budget_state_id(action.customer_id, action.campaign_id)
        )
        if action.action == BUDGET_PUSH:
            if dry_run:
                entry["result"] = "dry_run: not applied"
                logger.info("Config sheet budget push suppressed (dry run): %s", action,
                            extra={"sync_id": plan.sync_id, "campaign_id": str(action.campaign_id)})
                budget_log.append(entry)
                continue
            try:
                response = budgets.set_campaign_budget(action.customer_id, str(action.campaign_id), action.sheet_micros)
            except Exception as err:  # pylint: disable=broad-except
                response = {"success": False, "error": str(err)}
            if response.get("dry_run"):
                entry["result"] = "dry_run: not applied"
                budget_log.append(entry)
                continue
            if response.get("error") or response.get("success") is False:
                failed = True
                error = str(response.get("error") or response)[:500]
                entry["result"] = f"failed: {error}"
                result.errors.append(f"campaign {action.campaign_id}: budget push failed: {error}")
                logger.error(
                    "Config sheet budget push failed for campaign %s [%s]: %s",
                    action.campaign_id, telemetry.classify_error(error), error,
                    extra=telemetry.event_fields(
                        telemetry.EVENT_TOOL_ERROR,
                        tool="update_google_ads_campaign_budget",
                        dependency="google_ads",
                        error_class=telemetry.classify_error(error),
                        mutating="true",
                        customer_id=action.customer_id,
                        campaign_id=str(action.campaign_id),
                        sync_id=plan.sync_id,
                    ),
                )
                budget_log.append(entry)
                continue
            _write_state(state_ref, action)
            _write_budget_changelog(db, plan, action, "live", now)
            result.budget_pushes += 1
            entry["result"] = "applied"
            logger.info(
                "Audit: applied budget via config sheet on campaign %s: %s -> %s micros",
                action.campaign_id, action.live_micros, action.sheet_micros,
                extra=telemetry.event_fields(
                    telemetry.EVENT_MUTATION_APPLIED,
                    tool="update_google_ads_campaign_budget",
                    action="budget",
                    dependency="google_ads",
                    customer_id=action.customer_id,
                    campaign_id=str(action.campaign_id),
                    dry_run="false",
                    source="config_sheet",
                    sync_id=plan.sync_id,
                ),
            )
        elif action.action in (BUDGET_SET_NORMAL, BUDGET_RECORD):
            if action.state_update:
                _write_state(state_ref, action)
            entry["result"] = "recorded"
        else:
            entry["result"] = "skipped"
            logger.warning(
                "Config sheet budget %s for campaign %s: %s",
                action.action, action.campaign_id, action.message,
                extra={"sync_id": plan.sync_id, "customer_id": action.customer_id,
                       "campaign_id": str(action.campaign_id)},
            )
        if action.action != BUDGET_RECORD or action.state_update:
            budget_log.append(entry)

    if result.changes or budget_log or plan.conflicts:
        db.collection("ConfigChangeLog").document(plan.sync_id).set({
            "syncId": plan.sync_id,
            "source": source,
            "timestampUtc": now.isoformat(),
            "customerFilter": plan.customer_filter,
            "dryRun": dry_run,
            "changes": [c.to_dict() for c in plan.changes],
            "budgets": budget_log,
            "warnings": [str(i) for i in plan.issues],
        })

    if failed:
        result.outcome = OUTCOME_PARTIAL
    elif result.changes or result.budget_pushes or any(
        a.action == BUDGET_SET_NORMAL for a in plan.budget_actions
    ):
        result.outcome = OUTCOME_APPLIED
    return result


def _write_state(state_ref: Any, action: BudgetAction) -> None:
    """Writes a budget action's state fields (creating the doc if needed)."""
    if action.state_exists:
        state_ref.set(action.state_update, merge=True)
    else:
        state_ref.set(action.state_update)


# --- Logging and the sheet's SyncLog tab -------------------------------------


def log_result(result: SyncResult, customer_id: Optional[str] = None) -> None:
    """Emits the config_sync monitoring event for a sync."""
    level = logging.ERROR if result.outcome in (OUTCOME_INVALID, OUTCOME_ERROR, OUTCOME_PARTIAL) else (
        logging.WARNING if result.conflicts else logging.INFO
    )
    details = "; ".join(result.errors[:5])
    if result.plan and result.plan.conflicts:
        details = "; ".join(filter(None, [details] + [str(a) for a in result.plan.conflicts[:5]]))
    logger.log(
        level,
        "Config sheet sync %s (%s): outcome=%s, changes=%d, budget pushes=%d, conflicts=%d%s",
        result.sync_id, result.source, result.outcome, result.changes, result.budget_pushes,
        result.conflicts, f". {details}" if details else "",
        extra=telemetry.event_fields(
            EVENT_CONFIG_SYNC,
            sync_id=result.sync_id,
            source=result.source,
            outcome=result.outcome,
            customer_id=customer_id,
            changes=result.changes,
            budget_pushes=result.budget_pushes,
            conflicts=result.conflicts,
            error_count=len(result.errors),
            needs_attention=str(result.needs_attention).lower(),
        ),
    )


def append_sync_log(service: Any, spreadsheet_id: str, result: SyncResult,
                    now: Optional[datetime.datetime] = None) -> None:
    """Appends one row to the sheet's SyncLog tab. Best effort.

    Failure (no edit access, tab missing) is logged and otherwise ignored:
    the authoritative record is ConfigChangeLog in Firestore.
    """
    if result.outcome == OUTCOME_NOOP and not result.conflicts:
        return  # Keep the tab readable: twice-daily no-op syncs add nothing.
    now = now or datetime.datetime.now(datetime.timezone.utc)
    details: List[str] = list(result.errors[:10])
    if result.plan:
        details += [str(c) for c in result.plan.changes[:20]]
        details += [str(a) for a in result.plan.budget_actions if a.action != BUDGET_RECORD][:20]
    row = [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        result.source,
        result.outcome,
        result.changes + result.budget_pushes,
        result.conflicts,
        len(result.errors),
        "\n".join(details)[:45000],
    ]
    try:
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{schema.SYNC_LOG_TAB}'!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as err:  # pylint: disable=broad-except
        logger.warning("Could not append to the %s tab: %s", schema.SYNC_LOG_TAB, err)


# --- Entry points -----------------------------------------------------------


def plan_from_sheet(
    service: Any,
    spreadsheet_id: str,
    db: Any,
    budgets: Optional[BudgetGateway],
    customer_filter: Optional[str] = None,
) -> SyncPlan:
    """Reads the sheet and Firestore and builds a plan."""
    known_cities = set(load_baselines(db))
    sheet = schema.parse_sheet(read_sheet(service, spreadsheet_id), known_cities)
    state = load_state(db, sheet, customer_filter)
    return build_plan(sheet, state, budgets, customer_filter)


def run_sheet_sync(
    spreadsheet_id: str,
    db: Any,
    customer_id: Optional[str] = None,
    source: str = "scheduled_run",
    service_factory: Optional[Callable[[], Any]] = None,
    budgets: Optional[BudgetGateway] = None,
) -> SyncResult:
    """Syncs the sheet into Firestore. Never raises.

    Used at the start of each scheduled run. Any failure (sheet unreachable,
    invalid rows, Firestore error) leaves Firestore as it was, so the run goes
    ahead on the last good configuration, and emits a config_sync event with
    needs_attention=true for the alert.

    Args:
        spreadsheet_id: The configuration spreadsheet id.
        db: A google.cloud.firestore.Client.
        customer_id: Limit accounts, campaigns and budgets to this customer.
        source: Recorded in the audit log.
        service_factory: Returns a Sheets service (tests). Defaults to the
            shared Sheets client.
        budgets: Budget gateway (tests). Defaults to Google Ads.

    Returns:
        The SyncResult.
    """
    service = None
    try:
        if service_factory is None:
            from agentic_dsta.tools.sa360.sa360_utils import get_sheets_service  # pylint: disable=import-outside-toplevel
            service_factory = get_sheets_service
        service = service_factory()
        if service is None:
            raise RuntimeError("Failed to obtain credentials for Google Sheets")
        budgets = budgets or GoogleAdsBudgetGateway()
        plan = plan_from_sheet(service, spreadsheet_id, db, budgets, customer_id)
        result = apply_plan(plan, db, budgets, source)
    except Exception as err:  # pylint: disable=broad-except
        logger.exception("Config sheet sync failed: %s", err)
        result = SyncResult(sync_id=new_sync_id(), outcome=OUTCOME_ERROR, source=source, errors=[str(err)[:500]])
    log_result(result, customer_id)
    if service is not None:
        append_sync_log(service, spreadsheet_id, result)
    return result


# --- Export (bootstrap the sheet from Firestore) -------------------------------


def _default_activation(db: Any) -> Dict[str, Any]:
    """The weather_asset_groups playbook's fallback activation thresholds."""
    snap = db.collection("Playbooks").document("weather_asset_groups").get()
    defaults = ((snap.to_dict() or {}).get("defaults") or {}).get("activation") if snap.exists else None
    return dict(defaults or _FALLBACK_ACTIVATION)


def build_export_rows(db: Any, customer_ids: Optional[Set[str]] = None) -> Dict[str, List[List[Any]]]:
    """Builds sheet rows from the current Firestore configuration.

    Cities show their effective thresholds (the city's value, else the playbook
    default), so operators see the numbers actually in force.

    Args:
        db: A google.cloud.firestore.Client.
        customer_ids: Limit Accounts and Campaigns to these customers.

    Returns:
        Cell grid per tab name, header row first.
    """
    accounts = [[c.header for c in schema.ACCOUNT_COLUMNS]]
    campaigns = [[c.header for c in schema.CAMPAIGN_COLUMNS]]
    for snap in db.collection("GoogleAdsConfig").stream():
        if customer_ids and snap.id not in customer_ids:
            continue
        cfg = snap.to_dict() or {}
        schedule = cfg.get("schedule") or {}
        guard = cfg.get("changeVolumeGuard") or {}
        tokens = cfg.get("assetGroupTokens") or {}
        accounts.append([
            snap.id,
            cfg.get("account", ""),
            schedule.get("timezone", ""),
            schedule.get("runsPerDay", 2),
            cfg.get("lookAheadHours", 24),
            cfg.get("trailingWindowHours", 24),
            "Y" if guard.get("enabled") else "N",
            guard.get("maxFractionOfEligibleCampaigns", 0.25),
            *(tokens.get(c, "") for c in schema.CONDITIONS),
        ])
        for campaign in cfg.get("campaigns", []) or []:
            params = campaign.get("params") or {}
            playbook_ids = campaign.get("playbooks") or ([campaign["playbook"]] if campaign.get("playbook") else [])
            severe = params.get("severeModifiers") or {}
            state_snap = db.collection("CampaignBudgetState").document(
                budget_state_id(snap.id, campaign.get("campaignId"))
            ).get()
            normal = (state_snap.to_dict() or {}).get("normalBudgetMicros") if state_snap.exists else None
            campaigns.append([
                snap.id,
                str(campaign.get("campaignId", "")),
                "Y",
                params.get("city", ""),
                params.get("geo", ""),
                params.get("campaignNameContains") or "",
                "Y" if "weather_asset_groups" in playbook_ids else "N",
                "Y" if "severe_budget" in playbook_ids else "N",
                schema.micros_to_units(normal) if normal is not None else "",
                severe.get("budgetBumpPct", "") if "severe_budget" in playbook_ids else "",
                schema.micros_to_units(severe.get("maxDailyBudgetMicros")) or "",
            ])

    defaults = _default_activation(db)
    cities = [[c.header for c in schema.CITY_COLUMNS]]
    for city, data in sorted(load_baselines(db).items()):
        activation = data.get("activation") if isinstance(data.get("activation"), dict) else {}
        severe = data.get("severeThresholds") or {}
        cities.append([
            city,
            activation.get("coldBelowC", defaults.get("coldBelowC")),
            activation.get("rainRateMmPerH", defaults.get("rainRateMmPerH")),
            activation.get("sunnyMinHours", defaults.get("sunnyMinHours")),
            data.get("latitude", ""),
            data.get("longitude", ""),
            severe.get("rainMm", ""),
            severe.get("snowMmSwe", ""),
            severe.get("coldC", ""),
        ])
    return {
        schema.ACCOUNTS_TAB: accounts,
        schema.CAMPAIGNS_TAB: campaigns,
        schema.CITIES_TAB: cities,
        schema.SYNC_LOG_TAB: [list(schema.SYNC_LOG_HEADERS)],
    }


def export_to_sheet(
    service: Any,
    spreadsheet_id: str,
    rows_by_tab: Dict[str, List[List[Any]]],
    tabs: Optional[Iterable[str]] = None,
    force: bool = False,
) -> List[str]:
    """Writes exported rows to the sheet, creating missing tabs.

    Refuses to overwrite a tab that already has data rows unless ``force`` is
    set, so a stray export cannot wipe operator edits. SyncLog is only written
    when it is created.

    Args:
        service: A Sheets API v4 service.
        spreadsheet_id: Target spreadsheet id.
        rows_by_tab: Output of :func:`build_export_rows`.
        tabs: Limit to these tabs. Defaults to all.
        force: Overwrite tabs that already contain data.

    Returns:
        The tabs written.

    Raises:
        RuntimeError: If a tab has data and force is not set.
    """
    selected = list(tabs or rows_by_tab)
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}

    missing = [t for t in selected if t not in existing]
    if missing:
        reply = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]},
        ).execute()
        for added in reply.get("replies", []):
            props = added.get("addSheet", {}).get("properties", {})
            existing[props.get("title")] = props.get("sheetId")

    written: List[str] = []
    for tab in selected:
        if tab == schema.SYNC_LOG_TAB and tab not in missing:
            continue
        if tab not in missing and not force:
            current = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=f"'{tab}'"
            ).execute().get("values", [])
            if len(current) > 1:
                raise RuntimeError(
                    f"Tab '{tab}' already has {len(current) - 1} data row(s). "
                    "Re-run with --force to overwrite it, or --tabs to export other tabs only."
                )
        service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=f"'{tab}'").execute()
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": rows_by_tab[tab]},
        ).execute()
        written.append(tab)

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": existing[t], "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }}
                for t in written if existing.get(t) is not None
            ] + [
                {"repeatCell": {
                    "range": {"sheetId": existing[t], "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }}
                for t in written if existing.get(t) is not None
            ]},
        ).execute()
    except Exception as err:  # pylint: disable=broad-except
        logger.warning("Could not format header rows: %s", err)
    return written
