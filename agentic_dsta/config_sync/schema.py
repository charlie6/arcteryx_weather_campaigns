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
"""Layout, parsing and validation of the ADSTA configuration sheet.

The sheet has three operator tabs and one log tab:

* ``Accounts``  - one row per Google Ads customer: schedule, windows, change
  guard and asset group tokens (the account-level GoogleAdsConfig fields).
* ``Campaigns`` - one row per campaign: city, playbooks, budget levers, the
  normal daily budget and an optional per-campaign dry run switch.
* ``Cities``    - one row per weather location: the asset group activation
  thresholds, plus read-only reference columns (coordinates and severe
  thresholds) that the sync never reads.
* ``SyncLog``   - written by the sync, one row per run.

Columns are matched by header text, not position, so operators can reorder
columns or add their own (for example a Notes column); unknown headers are
ignored. Validation is all-or-nothing: a sync that has any error concerning
it writes nothing, and Firestore keeps the last configuration that passed.
Each issue records the account(s) it belongs to, so a sync scoped to one
account (a scheduled run) is not blocked by another account's rows, while an
unscoped sync (the CLI without --customer_id) is blocked by every error.
Issues in the Cities tab, tab-level problems and rows whose Customer ID cannot
be read concern every account.

This module is pure: no network or Firestore access, which keeps it fully
unit-testable.
"""

from __future__ import annotations

import dataclasses
import decimal
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import zoneinfo

from agentic_dsta.core import activation as activation_lib

ACCOUNTS_TAB = "Accounts"
CAMPAIGNS_TAB = "Campaigns"
CITIES_TAB = "Cities"
SYNC_LOG_TAB = "SyncLog"

ERROR = "error"
WARNING = "warning"

# Asset group conditions, in the order the sheet shows their token columns.
CONDITIONS = ("Cold", "Rain", "Snow", "Sunny")

# Google Ads requires budget amounts in multiples of 10,000 micros (one cent).
_BUDGET_MICROS_STEP = 10_000
_MICROS_PER_UNIT = 1_000_000


@dataclasses.dataclass(frozen=True)
class Column:
    """One sheet column.

    Attributes:
        key: Internal field name.
        header: Header text written by export and matched (loosely) on read.
        required: Whether the header must be present for the tab to parse.
        reference: Read-only column, written by export and ignored on read.
    """

    key: str
    header: str
    required: bool = False
    reference: bool = False


ACCOUNT_COLUMNS: Tuple[Column, ...] = (
    Column("customerId", "Customer ID", required=True),
    Column("account", "Account label"),
    Column("timezone", "Timezone", required=True),
    Column("runsPerDay", "Runs per day"),
    Column("lookAheadHours", "Look-ahead hours"),
    Column("trailingWindowHours", "Trailing window hours"),
    Column("guardEnabled", "Change guard enabled"),
    Column("guardMaxFraction", "Change guard max fraction"),
    *(Column(f"token{c}", f"{c} token", required=True) for c in CONDITIONS),
)

CAMPAIGN_COLUMNS: Tuple[Column, ...] = (
    Column("customerId", "Customer ID", required=True),
    Column("campaignId", "Campaign ID", required=True),
    Column("active", "Active"),
    Column("city", "City", required=True),
    Column("geo", "Geo"),
    Column("campaignNameContains", "Campaign name contains"),
    Column("assetGroups", "Asset groups"),
    Column("severeBudget", "Severe budget"),
    Column("normalDailyBudget", "Normal daily budget"),
    Column("budgetBumpPct", "Budget bump %"),
    Column("maxDailyBudget", "Max daily budget"),
    # Optional. Appended last so adding it does not shift existing columns.
    Column("dryRun", "Dry run"),
)

CITY_COLUMNS: Tuple[Column, ...] = (
    Column("city", "City", required=True),
    Column("coldBelowC", "Cold below C", required=True),
    Column("rainRateMmPerH", "Rain rate mm/h", required=True),
    Column("sunnyMinHours", "Sunny min hours", required=True),
    Column("latitude", "Latitude (ref)", reference=True),
    Column("longitude", "Longitude (ref)", reference=True),
    Column("severeRainMm", "Severe rain mm (ref)", reference=True),
    Column("severeSnowMmSwe", "Severe snow mm SWE (ref)", reference=True),
    Column("severeColdC", "Severe cold C (ref)", reference=True),
)

SYNC_LOG_HEADERS = (
    "Time (UTC)", "Source", "Outcome", "Changes", "Conflicts", "Errors", "Details",
)

TAB_COLUMNS: Dict[str, Tuple[Column, ...]] = {
    ACCOUNTS_TAB: ACCOUNT_COLUMNS,
    CAMPAIGNS_TAB: CAMPAIGN_COLUMNS,
    CITIES_TAB: CITY_COLUMNS,
}

# Plausible ranges. Anything outside is almost certainly a typo, and a typo in
# a threshold or budget is exactly what this validation exists to catch.
_RUNS_PER_DAY = (1, 24)
_LOOK_AHEAD_HOURS = (1, 120)
_TRAILING_WINDOW_HOURS = (0, 168)
_GUARD_FRACTION = (0.01, 1.0)
_BUMP_PCT = (1, 500)
_BUDGET_UNITS = (0.01, 1_000_000)

_TRUE = {"y", "yes", "true", "1", "on"}
_FALSE = {"n", "no", "false", "0", "off"}


@dataclasses.dataclass(frozen=True)
class Issue:
    """A validation finding tied to a sheet cell.

    Attributes:
        severity: ERROR (blocks the sync) or WARNING (reported only).
        tab: Sheet tab name.
        row: 1-based sheet row number (the header is row 1), or 0 for a
            tab-level problem.
        column: Column header, or "" for a row- or tab-level problem.
        message: Human readable explanation.
        customer_ids: The account(s) whose rows the issue is about. Empty
            means it concerns every account: Cities rows, tab-level problems,
            and rows whose Customer ID cell cannot be read (the row might
            belong to any account, so guessing could drop its campaign).
    """

    severity: str
    tab: str
    row: int
    column: str
    message: str
    customer_ids: Tuple[str, ...] = ()

    def applies_to(self, customer_id: Optional[str]) -> bool:
        """Whether this issue concerns a sync scoped to ``customer_id``.

        Args:
            customer_id: The account a sync is limited to, or None for a sync
                of every account (which every issue concerns).

        Returns:
            True if the issue should count against that sync.
        """
        return customer_id is None or not self.customer_ids or customer_id in self.customer_ids

    def __str__(self) -> str:
        where = self.tab
        if self.row:
            where += f" row {self.row}"
        if self.column:
            where += f" [{self.column}]"
        return f"{self.severity.upper()}: {where}: {self.message}"


@dataclasses.dataclass
class AccountRow:
    """A validated Accounts row."""

    row: int
    customer_id: str
    account: str
    timezone: str
    runs_per_day: int
    look_ahead_hours: int
    trailing_window_hours: int
    guard_enabled: bool
    guard_max_fraction: float
    tokens: Dict[str, str]


@dataclasses.dataclass
class CampaignRow:
    """A validated Campaigns row. Budgets are in micros.

    ``dry_run`` is None when the sheet has no "Dry run" column. The sync then
    leaves the campaign's stored setting as it is rather than reading the
    missing column as "live".
    """

    row: int
    customer_id: str
    campaign_id: int
    active: bool
    city: str
    geo: Optional[str]
    campaign_name_contains: Optional[str]
    asset_groups: bool
    severe_budget: bool
    normal_budget_micros: Optional[int]
    budget_bump_pct: Optional[float]
    max_budget_micros: Optional[int]
    dry_run: Optional[bool] = None

    @property
    def playbooks(self) -> List[str]:
        """The playbook ids this campaign runs, in execution order."""
        selected = []
        if self.asset_groups:
            selected.append("weather_asset_groups")
        if self.severe_budget:
            selected.append("severe_budget")
        return selected


@dataclasses.dataclass
class CityRow:
    """A validated Cities row. ``activation`` holds only the set thresholds."""

    row: int
    city: str
    activation: Dict[str, Any]


@dataclasses.dataclass
class SheetConfig:
    """The parsed sheet and every issue found while parsing it."""

    accounts: Dict[str, AccountRow] = dataclasses.field(default_factory=dict)
    campaigns: List[CampaignRow] = dataclasses.field(default_factory=list)
    cities: Dict[str, CityRow] = dataclasses.field(default_factory=dict)
    issues: List[Issue] = dataclasses.field(default_factory=list)

    @property
    def errors(self) -> List[Issue]:
        """Issues that block the sync."""
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> List[Issue]:
        """Issues that are reported but do not block the sync."""
        return [i for i in self.issues if i.severity == WARNING]

    def campaigns_for(self, customer_id: str) -> List[CampaignRow]:
        """Returns the campaign rows belonging to one customer."""
        return [c for c in self.campaigns if c.customer_id == customer_id]


# --- Cell helpers -------------------------------------------------------------


def normalise_header(text: Any) -> str:
    """Reduces a header to lower-case letters and digits for loose matching.

    "Budget bump %", "budget bump" and "BUDGET_BUMP" all match each other.

    Args:
        text: The raw header cell.

    Returns:
        The normalised header.
    """
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def is_blank(value: Any) -> bool:
    """True for an empty or whitespace-only cell."""
    return value is None or (isinstance(value, str) and not value.strip())


def micros_to_units(micros: Optional[int]) -> Optional[float]:
    """Converts micros to account currency units for display in the sheet."""
    if micros is None:
        return None
    return round(int(micros) / _MICROS_PER_UNIT, 2)


def units_to_micros(units: float) -> int:
    """Converts a currency amount to micros, rounded to the nearest cent.

    Args:
        units: Amount in account currency, for example 25.5.

    Returns:
        Micros, a multiple of 10,000 as Google Ads requires.
    """
    cents = decimal.Decimal(str(units)).quantize(
        decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP
    )
    return int(cents * 100) * _BUDGET_MICROS_STEP


class _RowReader:
    """Reads and validates the cells of one data row, collecting issues.

    Attributes:
        customer_ids: The account the row belongs to, set by the tab parser
            once the Customer ID cell has been read successfully. Issues are
            attributed to it; until it is set (or if the cell is unreadable)
            they concern every account.
    """

    def __init__(
        self,
        tab: str,
        row_number: int,
        cells: Sequence[Any],
        index: Dict[str, int],
        columns: Dict[str, Column],
        issues: List[Issue],
    ) -> None:
        self.tab = tab
        self.row = row_number
        self._cells = cells
        self._index = index
        self._columns = columns
        self._issues = issues
        self.ok = True
        self.customer_ids: Tuple[str, ...] = ()

    def raw(self, key: str) -> Any:
        """Returns the raw cell value, or None if the column is absent."""
        position = self._index.get(key)
        if position is None or position >= len(self._cells):
            return None
        return self._cells[position]

    def has(self, key: str) -> bool:
        """Whether the tab has this column (its cell in this row may be blank)."""
        return key in self._index

    def _report(
        self, key: str, message: str, severity: str = ERROR, also_customers: Sequence[str] = ()
    ) -> None:
        if severity == ERROR:
            self.ok = False
        header = self._columns[key].header if key in self._columns else key
        customers = self.customer_ids
        if customers:
            customers += tuple(c for c in also_customers if c and c not in customers)
        self._issues.append(Issue(severity, self.tab, self.row, header, message, customers))

    def warn(self, key: str, message: str) -> None:
        """Records a non-blocking warning against a cell."""
        self._report(key, message, WARNING)

    def error(self, key: str, message: str, also_customers: Sequence[str] = ()) -> None:
        """Records a blocking error against a cell.

        Args:
            key: Column key.
            message: Explanation.
            also_customers: Other accounts the error also blocks, for problems
                that span two accounts' rows.
        """
        self._report(key, message, ERROR, also_customers)

    def text(self, key: str, required: bool = False) -> Optional[str]:
        """Reads a text cell, stripped. Numbers are converted to text."""
        value = self.raw(key)
        if is_blank(value):
            if required:
                self.error(key, "is required")
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value).strip()

    def digits(self, key: str, length: Optional[int] = None) -> Optional[str]:
        """Reads an ID cell, accepting hyphens and spaces (123-456-7890)."""
        value = self.raw(key)
        if is_blank(value):
            self.error(key, "is required")
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        text = re.sub(r"[\s-]", "", str(value))
        if not text.isdigit():
            self.error(key, f"{value!r} is not a numeric ID")
            return None
        if length and len(text) != length:
            self.error(key, f"{value!r} must have {length} digits")
            return None
        return text

    def number(
        self,
        key: str,
        limits: Tuple[float, float],
        default: Any = None,
        integer: bool = False,
        required: bool = False,
    ) -> Any:
        """Reads a number cell within inclusive limits.

        Currency symbols and thousands separators are tolerated, so "$1,250"
        reads as 1250.

        Returns:
            The number, ``default`` when blank, or None on error.
        """
        value = self.raw(key)
        if is_blank(value):
            if required:
                self.error(key, "is required")
            return default
        if isinstance(value, bool):
            self.error(key, f"{value!r} is not a number")
            return None
        try:
            number = float(re.sub(r"[,\s$€£%]", "", value) if isinstance(value, str) else value)
        except (TypeError, ValueError):
            self.error(key, f"{value!r} is not a number")
            return None
        low, high = limits
        if not low <= number <= high:
            self.error(key, f"{value!r} is outside the allowed range {low} to {high}")
            return None
        if integer:
            if not number.is_integer():
                self.error(key, f"{value!r} must be a whole number")
                return None
            return int(number)
        return number

    def flag(self, key: str, default: bool) -> Optional[bool]:
        """Reads a Y/N cell. Blank gives ``default``."""
        value = self.raw(key)
        if is_blank(value):
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        self.error(key, f"{value!r} is not Y or N")
        return None


def _iter_rows(
    tab: str,
    values: Optional[List[List[Any]]],
    columns: Tuple[Column, ...],
    issues: List[Issue],
):
    """Yields a _RowReader for each non-blank data row of a tab.

    Args:
        tab: Tab name, for issue reporting.
        values: The tab's cell grid, header row first.
        columns: The tab's column definitions.
        issues: Collector for issues.

    Yields:
        One _RowReader per non-blank data row.
    """
    if not values:
        issues.append(Issue(ERROR, tab, 0, "", "tab is empty or missing its header row"))
        return

    by_header = {normalise_header(c.header): c for c in columns if not c.reference}
    index: Dict[str, int] = {}
    for position, header in enumerate(values[0]):
        column = by_header.get(normalise_header(header))
        if column and column.key not in index:
            index[column.key] = position

    missing = [c.header for c in columns if c.required and c.key not in index]
    if missing:
        issues.append(
            Issue(ERROR, tab, 1, "", f"missing required column(s): {', '.join(missing)}")
        )
        return

    column_map = {c.key: c for c in columns}
    for offset, cells in enumerate(values[1:]):
        if all(is_blank(cell) for cell in cells):
            continue
        yield _RowReader(tab, offset + 2, cells, index, column_map, issues)


# --- Tab parsers --------------------------------------------------------------


def _parse_accounts(values, issues: List[Issue]) -> Dict[str, AccountRow]:
    accounts: Dict[str, AccountRow] = {}
    for reader in _iter_rows(ACCOUNTS_TAB, values, ACCOUNT_COLUMNS, issues):
        customer_id = reader.digits("customerId", length=10)
        if customer_id:
            reader.customer_ids = (customer_id,)
        timezone = reader.text("timezone", required=True)
        if timezone:
            try:
                zoneinfo.ZoneInfo(timezone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                reader.error("timezone", f"{timezone!r} is not an IANA timezone such as America/Vancouver")

        tokens: Dict[str, str] = {}
        for condition in CONDITIONS:
            token = reader.text(f"token{condition}", required=True)
            if token:
                tokens[condition] = token.upper()
        seen: Dict[str, str] = {}
        for condition, token in tokens.items():
            if token in seen:
                reader.error(
                    f"token{condition}",
                    f"token {token!r} is also used for {seen[token]}; each condition needs its own",
                )
            seen[token] = condition

        row = AccountRow(
            row=reader.row,
            customer_id=customer_id or "",
            account=reader.text("account") or "",
            timezone=timezone or "",
            runs_per_day=reader.number("runsPerDay", _RUNS_PER_DAY, default=2, integer=True),
            look_ahead_hours=reader.number("lookAheadHours", _LOOK_AHEAD_HOURS, default=24, integer=True),
            trailing_window_hours=reader.number(
                "trailingWindowHours", _TRAILING_WINDOW_HOURS, default=24, integer=True
            ),
            guard_enabled=reader.flag("guardEnabled", default=False),
            guard_max_fraction=reader.number("guardMaxFraction", _GUARD_FRACTION, default=0.25),
            tokens=tokens,
        )
        if customer_id and customer_id in accounts:
            reader.error("customerId", f"customer {customer_id} is listed more than once")
        if reader.ok and customer_id:
            accounts[customer_id] = row
    return accounts


def _parse_campaigns(
    values,
    issues: List[Issue],
    accounts: Dict[str, AccountRow],
    known_cities: Set[str],
) -> List[CampaignRow]:
    campaigns: List[CampaignRow] = []
    seen_ids: Dict[int, Tuple[int, str]] = {}
    for reader in _iter_rows(CAMPAIGNS_TAB, values, CAMPAIGN_COLUMNS, issues):
        customer_id = reader.digits("customerId", length=10)
        if customer_id:
            reader.customer_ids = (customer_id,)
        if customer_id and customer_id not in accounts:
            reader.error("customerId", f"customer {customer_id} has no valid row in the {ACCOUNTS_TAB} tab")

        campaign_digits = reader.digits("campaignId")
        campaign_id = int(campaign_digits) if campaign_digits else 0
        if campaign_id and campaign_id in seen_ids:
            first_row, first_customer = seen_ids[campaign_id]
            if first_customer and customer_id and first_customer != customer_id:
                # Campaign IDs are unique across Google Ads, so one of the two
                # rows names the wrong account. Hold both accounts' syncs.
                reader.error(
                    "campaignId",
                    f"campaign {campaign_id} is already listed in row {first_row} under customer "
                    f"{first_customer}; a campaign belongs to one account",
                    also_customers=(first_customer,),
                )
            else:
                reader.error("campaignId", f"campaign {campaign_id} is already listed in row {first_row}")
        elif campaign_id:
            seen_ids[campaign_id] = (reader.row, customer_id or "")

        city = reader.text("city", required=True)
        if city and city not in known_cities:
            reader.error(
                "city",
                f"{city!r} has no ClimateBaselines document. Use a name from the {CITIES_TAB} tab "
                "(exact spelling), or add the city to cities.json and rebuild the baselines.",
            )

        active = reader.flag("active", default=True)
        asset_groups = reader.flag("assetGroups", default=True)
        severe_budget = reader.flag("severeBudget", default=False)
        bump = reader.number("budgetBumpPct", _BUMP_PCT)
        normal_units = reader.number("normalDailyBudget", _BUDGET_UNITS)
        max_units = reader.number("maxDailyBudget", _BUDGET_UNITS)
        # Tri-state: None (no column) leaves the stored setting unchanged.
        dry_run = reader.flag("dryRun", default=False) if reader.has("dryRun") else None

        if active and asset_groups is False and severe_budget is False:
            reader.error("assetGroups", "an active campaign must run Asset groups, Severe budget, or both")
        if severe_budget and is_blank(reader.raw("budgetBumpPct")):
            reader.error("budgetBumpPct", "is required when Severe budget is Y")
        if severe_budget is False and bump is not None:
            reader.warn("budgetBumpPct", "is ignored because Severe budget is N")
        if normal_units is not None and max_units is not None and max_units < normal_units:
            reader.error("maxDailyBudget", "must be at least the Normal daily budget")
        if dry_run and active is False:
            reader.warn("dryRun", "is ignored because Active is N")

        row = CampaignRow(
            row=reader.row,
            customer_id=customer_id or "",
            campaign_id=campaign_id,
            active=bool(active),
            city=city or "",
            geo=reader.text("geo"),
            campaign_name_contains=reader.text("campaignNameContains"),
            asset_groups=bool(asset_groups),
            severe_budget=bool(severe_budget),
            normal_budget_micros=units_to_micros(normal_units) if normal_units is not None else None,
            budget_bump_pct=bump,
            max_budget_micros=units_to_micros(max_units) if max_units is not None else None,
            dry_run=dry_run,
        )
        if reader.ok:
            campaigns.append(row)
    return campaigns


def _parse_cities(
    values, issues: List[Issue], known_cities: Set[str]
) -> Tuple[Dict[str, CityRow], Set[str]]:
    """Parses the Cities tab.

    Args:
        values: The tab's cell grid, header row first.
        issues: Validation issues are appended here.
        known_cities: Document ids of the ClimateBaselines collection.

    Returns:
        The valid rows keyed by city, and the names of cities that have a row
        with an error.
    """
    cities: Dict[str, CityRow] = {}
    invalid: Set[str] = set()
    for reader in _iter_rows(CITIES_TAB, values, CITY_COLUMNS, issues):
        city = reader.text("city", required=True)
        if city and city not in known_cities:
            reader.error(
                "city",
                f"{city!r} has no ClimateBaselines document. Cities are added via cities.json "
                "and build_climate_baselines.py, not in the sheet.",
            )
        if city and city in cities:
            reader.error("city", f"{city!r} is listed more than once")

        activation: Dict[str, Any] = {}
        for key, limits in activation_lib.ACTIVATION_LIMITS.items():
            value = reader.number(key, limits)
            if value is not None:
                activation[key] = activation_lib.normalise_activation_value(key, value)
        if reader.ok and city:
            cities[city] = CityRow(row=reader.row, city=city, activation=activation)
        elif city:
            invalid.add(city)
    return cities, invalid


def parse_sheet(tabs: Dict[str, Optional[List[List[Any]]]], known_cities: Set[str]) -> SheetConfig:
    """Parses and validates the whole configuration sheet.

    Args:
        tabs: Cell grid per tab name (header row first), as returned by the
            Sheets API with UNFORMATTED_VALUE rendering.
        known_cities: Document ids of the ClimateBaselines collection. Every
            city in the sheet must be one of them.

    Returns:
        The parsed configuration. Check ``errors`` before using it: rows with
        errors are excluded, and a sync refuses to write anything if at least
        one error concerns it (see Issue.applies_to).
    """
    issues: List[Issue] = []
    accounts = _parse_accounts(tabs.get(ACCOUNTS_TAB), issues)
    campaigns = _parse_campaigns(tabs.get(CAMPAIGNS_TAB), issues, accounts, known_cities)
    cities, invalid_cities = _parse_cities(tabs.get(CITIES_TAB), issues, known_cities)

    for campaign in campaigns:
        if not (campaign.active and campaign.asset_groups):
            continue
        city_row = cities.get(campaign.city)
        if city_row is None and campaign.city in invalid_cities:
            # The row's own error is reported and blocks every sync, so the
            # stored thresholds stay in force: "the default applies" is wrong.
            continue
        unset = [k for k in activation_lib.ACTIVATION_LIMITS if not city_row or k not in city_row.activation]
        if unset:
            issues.append(
                Issue(
                    WARNING,
                    CAMPAIGNS_TAB,
                    campaign.row,
                    "City",
                    f"{campaign.city} has no {', '.join(unset)} in the {CITIES_TAB} tab; "
                    "the playbook default applies",
                    (campaign.customer_id,),
                )
            )

    return SheetConfig(accounts=accounts, campaigns=campaigns, cities=cities, issues=issues)
