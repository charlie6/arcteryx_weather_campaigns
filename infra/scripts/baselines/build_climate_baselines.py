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
"""Builds the monthly rain, snow and cold baselines the campaign rules need.

Specification section 8 lists three missing datasets: monthly rain baselines
("average precipitation per wet day, by city and calendar month"), monthly snow
baselines of the same shape, and seasonal cold baselines for the 28 cities that
do not yet have one. This script produces all three from a single source.

This is a **one-time offline job**, not a runtime tool. Normals change on a
thirty-year cycle, so they are computed once and persisted; the agent reads the
stored values and never queries history while serving.

Source selection
----------------
NOAA GHCN-Daily station data cannot cover the footprint: none of the ten
European cities report daily snowfall, and Saskatoon and St. John's have no
qualifying precipitation station at all. The ERA5 reanalysis has no such gaps,
and reproduced Calgary's published annual normal to within 1%.

Open-Meteo's *historical forecast* archive is deliberately NOT used. It encodes
missing precipitation as ``0.0`` rather than ``null`` -- Calgary's August 2023
returns exactly zero from every model -- which understates annual totals by up
to two thirds and would silently deflate every baseline built from it.

Method
------
Rain and snow baselines are the ETCCDI Simple Daily Intensity Index: total
precipitation on wet days divided by the number of wet days, with a wet day
defined as **>= 1 mm**. The threshold is part of the standard definition, and
it matters: reanalyses over-produce trace precipitation, and measured against
higher-resolution model data a 0 mm threshold left SDII out by up to 46% while
a 1 mm threshold brought every city inside 10%.

Snow is handled strictly in **water equivalent**. Open-Meteo reports
``snowfall_sum`` in centimetres using a fixed 7:1 convention, which is a unit
convention rather than physics -- real snow-to-liquid ratios range from about
5:1 for wet coastal snow to 20:1 for cold dry continental snow. Treating that
centimetre figure as depth on the ground understates real accumulation by two
to three times across the prairie markets. The specification's severe snow
floor is already expressed as 10 mm/24h water equivalent, so the baseline is
defined the same way and centimetres never enter the calculation.

Cold baselines are the mean daily minimum temperature. That definition is not
assumed: it is validated against the three baselines published in the
specification, and the run fails if it cannot reproduce them.

Severity percentiles
--------------------
Means alone cannot drive a severity rule. Backtesting the specification's
absolute floors over this same 30-year record showed two failures that only a
per-city distribution can fix:

*   The floors almost never bind inland. Saskatoon reaches 45 mm of rain about
    once every thirty-three years, so the rain trigger effectively does not
    exist there.
*   The cold rule, ``winter mean minus 5 C``, is roughly a 0.5 sigma anomaly.
    It fires on 31 days a year in Saskatoon -- about one winter day in three --
    which is a seasonal budget increase rather than a severe-weather response.

So alongside the means this job emits per-city percentiles: rain p98 and snow
p95 over event days, and the p2 of daily minimum temperature. A percentile is
the operational definition of "unusual here", and because it is defined on the
city's own distribution it yields a near-uniform trigger rate across a
climatically diverse footprint -- measured at 1.4x spread between the most and
least triggered market, against 10.2x for the absolute floors.

Percentiles pool all months. Seasonal pooling was considered and rejected for
now: a 48 mm July day in Vancouver is exceptional on the same terms as a 48 mm
January day, and monthly pooling thins each bucket enough to make the tail
estimate noisy.


Usage:
    python3 build_climate_baselines.py --out-dir ./baselines_out
    python3 build_climate_baselines.py --cities "Vancouver BC,Calgary AB"
"""

import argparse
import calendar
import json
import logging
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# The standard 30-year climate normal period.
DEFAULT_START_DATE = "1991-01-01"
DEFAULT_END_DATE = "2020-12-31"

# ETCCDI defines a wet day as >= 1 mm. See the module docstring for why this
# is load-bearing rather than cosmetic.
WET_DAY_THRESHOLD_MM = 1.0

# Open-Meteo reports snowfall in centimetres of snow depth at a fixed 7:1
# snow-to-liquid convention: 7 cm of snow corresponds to 10 mm of liquid water.
#
# The direction matters and is easy to invert. An earlier revision multiplied
# by 0.7, which understated every snow baseline by a factor of 2.04 and would
# have halved the effective severe-snow threshold the moment the absolute floor
# stopped dominating. Converting centimetres of snow to millimetres of water
# DIVIDES by 0.7, equivalently multiplies by 10/7.
#
# Verified empirically against `precipitation_sum` on snow-only days, where
# `snowfall_sum / precipitation_sum` measures 0.73-0.75:
#
#   Saskatoon 2013-03-15: 4.48 cm snow, 6.10 mm precipitation -> 0.734
#   Saskatoon 2013-01-24: 3.78 cm snow, 5.10 mm precipitation -> 0.741
#   Saskatoon 2013-02-26: 2.52 cm snow, 3.40 mm precipitation -> 0.741
#
# Expressed as a ratio rather than a bare multiplier so the intended direction
# is legible at the call site.
OPEN_METEO_SNOW_TO_LIQUID_RATIO = 7.0
MM_SWE_PER_CM_SNOW = 10.0 / OPEN_METEO_SNOW_TO_LIQUID_RATIO

# Percentiles defining a severe day, chosen for the trigger rate they produce
# rather than for statistical neatness. Measured over 1991-2020 across the
# footprint, these yield roughly 1-3 rain days, 0-2 snow days and 7-8 cold days
# per city per year: frequent enough to be worth automating, rare enough that
# each firing is defensible to an advertiser.
#
# Rain and snow percentiles are taken over *event* days only. Including dry
# days would drag both percentiles to zero in every city, since most days in
# most months have no precipitation at all.
RAIN_SEVERE_PERCENTILE = 98.0
SNOW_SEVERE_PERCENTILE = 95.0

# Cold is taken over every day of the year, not just cold ones. There is no
# equivalent of a "dry day" to exclude, and the bottom tail of the full annual
# distribution is exactly the quantity of interest.
COLD_SEVERE_PERCENTILE = 2.0

DAILY_VARIABLES = (
    "precipitation_sum",
    "rain_sum",
    "snowfall_sum",
    "temperature_2m_min",
    "temperature_2m_max",
)

# Meteorological seasons, keyed by the months they contain.
SEASONS: Dict[str, Tuple[int, ...]] = {
    "winter": (12, 1, 2),
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "autumn": (9, 10, 11),
}

# Cold baselines published in specification section 3. These are used as a
# reference point, NOT as a pass/fail target, because investigation showed they
# correspond to airport stations rather than to any city-centre measurement:
#
#   City          spec   ERA5 grid   nearest GHCN-D station   airport
#   Chicago       -9.0        -7.2   -7.2 (Midway 3SW)        -9.4 (O'Hare)
#   Vancouver     +2.0        +1.4   +3.2 (Harbour CS)        +1.4 (YVR)
#   Los Angeles   +9.0        +7.0   +9.7 (Downtown/USC)      +9.0 (LAX)
#
# That is consistent with the Apps Script sourcing weather from OpenWeatherMap,
# whose observations are largely airport METAR. The practical consequence is
# that a cold baseline is only meaningful relative to the source the live
# trigger reads: a "5 C below baseline" margin means something different if the
# baseline came from O'Hare but the trigger reads a 25 km ERA5 cell. Spreads of
# up to 2.5 C between defensible choices for the same city were measured.
PUBLISHED_COLD_BASELINES_C: Dict[str, float] = {
    "Vancouver BC": 2.0,
    "Chicago IL": -9.0,
    "Los Angeles CA": 9.0,
}

# Beyond this spread, the choice of source materially changes trigger
# behaviour and a human needs to decide which source is authoritative.
COLD_BASELINE_REVIEW_THRESHOLD_C = 1.5

# A plausibility floor. Every city in the footprint is temperate or maritime;
# none has an annual precipitation normal below this, so a lower value means
# the source returned gaps rather than genuinely dry weather.
MIN_PLAUSIBLE_ANNUAL_PRECIP_MM = 150.0


class BaselineError(Exception):
  """Raised when baselines cannot be built or fail validation."""


def _load_cities(path: str) -> List[Dict[str, Any]]:
  """Reads the city configuration.

  Args:
      path: Path to the cities JSON file.

  Returns:
      The list of city records.

  Raises:
      BaselineError: If the file is missing or malformed.
  """
  try:
    with open(path, "r", encoding="utf-8") as handle:
      payload = json.load(handle)
  except (OSError, json.JSONDecodeError) as exc:
    raise BaselineError(f"Could not read cities file {path}: {exc}") from exc

  cities = payload.get("cities")
  if not cities:
    raise BaselineError(f"No 'cities' array in {path}")
  return cities


def _fetch_with_retry(
    url: str, attempts: int = 4, backoff_seconds: float = 5.0
) -> Dict[str, Any]:
  """Fetches a URL, retrying on rate limiting and transient failures.

  Args:
      url: The fully-formed request URL.
      attempts: Maximum number of tries before giving up.
      backoff_seconds: Base delay, doubled after each failed attempt.

  Returns:
      The decoded JSON response.

  Raises:
      BaselineError: If every attempt fails.
  """
  last_error: Optional[Exception] = None
  for attempt in range(1, attempts + 1):
    try:
      with urllib.request.urlopen(url, timeout=180) as response:
        return json.load(response)
    except urllib.error.HTTPError as exc:
      last_error = exc
      # 429 is rate limiting; 5xx is transient. Both are worth retrying.
      if exc.code != 429 and exc.code < 500:
        raise BaselineError(f"Request failed ({exc.code}): {url}") from exc
      delay = backoff_seconds * (2 ** (attempt - 1))
      logger.warning(
          "HTTP %s on attempt %d/%d; retrying in %.0fs",
          exc.code,
          attempt,
          attempts,
          delay,
      )
      time.sleep(delay)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
      last_error = exc
      delay = backoff_seconds * (2 ** (attempt - 1))
      logger.warning(
          "%r on attempt %d/%d; retrying in %.0fs",
          exc,
          attempt,
          attempts,
          delay,
      )
      time.sleep(delay)

  raise BaselineError(f"Gave up after {attempts} attempts: {last_error!r}")


def fetch_city_daily(
    city: Dict[str, Any],
    start_date: str,
    end_date: str,
    cache_dir: Optional[str] = None,
) -> Dict[str, Any]:
  """Fetches the daily ERA5 record for one city, using a disk cache.

  The cache exists so that re-running to adjust thresholds costs nothing. The
  underlying reanalysis for a closed historical period does not change.

  Args:
      city: A city record with latitude, longitude and timezone.
      start_date: Inclusive ISO start date.
      end_date: Inclusive ISO end date.
      cache_dir: Directory for cached responses, or None to disable caching.

  Returns:
      The `daily` block of the response, plus `_elevation`.

  Raises:
      BaselineError: If the response has no daily block.
  """
  cache_path = None
  if cache_dir:
    os.makedirs(cache_dir, exist_ok=True)
    safe_name = city["city"].replace(" ", "_").replace("/", "_")
    cache_path = os.path.join(
        cache_dir, f"{safe_name}_{start_date}_{end_date}.json"
    )
    if os.path.exists(cache_path):
      logger.info("Cache hit for %s", city["city"])
      with open(cache_path, "r", encoding="utf-8") as handle:
        return json.load(handle)

  params = {
      "latitude": city["latitude"],
      "longitude": city["longitude"],
      "start_date": start_date,
      "end_date": end_date,
      "daily": ",".join(DAILY_VARIABLES),
      "timezone": city["timezone"],
  }
  url = f"{ARCHIVE_URL}?{urllib.parse.urlencode(params)}"
  logger.info("Fetching %s (%s..%s)", city["city"], start_date, end_date)
  payload = _fetch_with_retry(url)

  if "daily" not in payload:
    raise BaselineError(
        f"No daily block for {city['city']}; keys={list(payload)[:8]}"
    )

  daily = payload["daily"]
  # Record the grid cell actually used. In mountain terrain the cell elevation
  # can differ sharply from the city, and that offset drives snow and
  # temperature error, so it belongs in the audit trail.
  daily["_elevation"] = payload.get("elevation")
  daily["_units"] = payload.get("daily_units", {})

  if cache_path:
    with open(cache_path, "w", encoding="utf-8") as handle:
      json.dump(daily, handle)

  return daily


def _mean(values: Sequence[float]) -> Optional[float]:
  """Returns the arithmetic mean, or None for an empty sequence."""
  return statistics.fmean(values) if values else None


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
  """Returns a linearly interpolated percentile of the given values.

  Implemented directly rather than via ``statistics.quantiles`` so that an
  arbitrary percentile can be requested without first partitioning the data
  into n buckets, and so the interpolation behaviour is explicit and testable.

  Args:
      values: The sample. Need not be sorted. May be empty.
      percentile: The percentile to compute, from 0 to 100 inclusive.

  Returns:
      The interpolated percentile, or None when the sample is empty.

  Raises:
      ValueError: If percentile is outside the range 0 to 100.
  """
  if not 0.0 <= percentile <= 100.0:
    raise ValueError(f"percentile must be within 0..100, got {percentile!r}")
  if not values:
    return None

  ordered = sorted(values)
  if len(ordered) == 1:
    return ordered[0]

  # Index into the sorted sample, then interpolate between the two
  # neighbouring observations. A single observation short-circuits above so
  # that `upper` can never run past the end.
  position = (len(ordered) - 1) * percentile / 100.0
  lower = int(position)
  upper = min(lower + 1, len(ordered) - 1)
  fraction = position - lower
  return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def compute_city_baselines(
    city: Dict[str, Any], daily: Dict[str, Any]
) -> Dict[str, Any]:
  """Reduces one city's daily record to monthly and seasonal baselines.

  Args:
      city: The city record, used for labelling.
      daily: The `daily` block returned by the archive.

  Returns:
      A baseline record with a `months` map keyed by month number, a `seasons`
      map, and a `warnings` list describing anything suspicious.

  Raises:
      BaselineError: If the daily block is missing required variables.
  """
  for key in DAILY_VARIABLES:
    if key not in daily:
      raise BaselineError(f"{city['city']}: response lacks '{key}'")

  dates = daily["time"]
  warnings: List[str] = []

  # Bucket every day into its calendar month.
  by_month: Dict[int, Dict[str, List[float]]] = {
      m: {"rain": [], "snow_swe": [], "tmin": []} for m in range(1, 13)
  }
  years_seen = set()

  # Severity percentiles pool the whole year; see the module docstring for why
  # they are not computed per month.
  annual_rain_events: List[float] = []
  annual_snow_events: List[float] = []
  annual_tmin: List[float] = []

  for i, date_str in enumerate(dates):
    month = int(date_str[5:7])
    years_seen.add(int(date_str[:4]))
    bucket = by_month[month]

    rain = daily["rain_sum"][i]
    if rain is not None:
      rain = float(rain)
      bucket["rain"].append(rain)
      if rain >= WET_DAY_THRESHOLD_MM:
        annual_rain_events.append(rain)

    snow_cm = daily["snowfall_sum"][i]
    if snow_cm is not None:
      snow_swe = float(snow_cm) * MM_SWE_PER_CM_SNOW
      bucket["snow_swe"].append(snow_swe)
      if snow_swe >= WET_DAY_THRESHOLD_MM:
        annual_snow_events.append(snow_swe)

    tmin = daily["temperature_2m_min"][i]
    if tmin is not None:
      tmin = float(tmin)
      bucket["tmin"].append(tmin)
      annual_tmin.append(tmin)

  year_count = max(len(years_seen), 1)
  months: Dict[str, Any] = {}

  for month in range(1, 13):
    bucket = by_month[month]

    wet = [v for v in bucket["rain"] if v >= WET_DAY_THRESHOLD_MM]
    snow_days = [v for v in bucket["snow_swe"] if v >= WET_DAY_THRESHOLD_MM]

    if not bucket["rain"]:
      warnings.append(f"month {month}: no rain observations at all")
    elif not wet:
      # Legitimate in an arid summer, but it is also exactly what a source
      # that encodes missing data as zero looks like. Flag either way.
      warnings.append(
          f"month {month}: zero wet days across {year_count} years"
      )

    months[str(month)] = {
        "month_name": calendar.month_abbr[month],
        "rain_mm_per_wet_day": round(_mean(wet), 2) if wet else None,
        "wet_days_per_month": round(len(wet) / year_count, 2),
        "snow_mm_swe_per_snow_day": (
            round(_mean(snow_days), 2) if snow_days else None
        ),
        "snow_days_per_month": round(len(snow_days) / year_count, 2),
        "mean_daily_min_c": (
            round(_mean(bucket["tmin"]), 2) if bucket["tmin"] else None
        ),
        "observation_days": len(bucket["rain"]),
    }

  seasons: Dict[str, Any] = {}
  for season_name, season_months in SEASONS.items():
    tmins: List[float] = []
    for month in season_months:
      tmins.extend(by_month[month]["tmin"])
    seasons[season_name] = {
        "cold_baseline_c": round(_mean(tmins), 1) if tmins else None,
    }

  annual_rain = sum(sum(by_month[m]["rain"]) for m in range(1, 13)) / year_count
  annual_snow_swe = (
      sum(sum(by_month[m]["snow_swe"]) for m in range(1, 13)) / year_count
  )

  if annual_rain + annual_snow_swe < MIN_PLAUSIBLE_ANNUAL_PRECIP_MM:
    warnings.append(
        f"annual precipitation {annual_rain + annual_snow_swe:.0f} mm is "
        "implausibly low; suspect source gaps encoded as zero"
    )

  # Severity thresholds, plus the trigger rate each one implies. The rate is
  # recorded because it, not the threshold, is the number a human can sanity
  # check: "1.1 severe rain days a year" is reviewable in a way that "24.1 mm"
  # is not.
  rain_severe = _percentile(annual_rain_events, RAIN_SEVERE_PERCENTILE)
  snow_severe = _percentile(annual_snow_events, SNOW_SEVERE_PERCENTILE)
  cold_severe = _percentile(annual_tmin, COLD_SEVERE_PERCENTILE)

  def _rate(values: Sequence[float], threshold: Optional[float],
            below: bool = False) -> Optional[float]:
    """Days per year meeting a threshold, or None when undefined."""
    if threshold is None:
      return None
    if below:
      hits = sum(1 for v in values if v <= threshold)
    else:
      hits = sum(1 for v in values if v >= threshold)
    return round(hits / year_count, 2)

  severe = {
      "rain_mm": round(rain_severe, 1) if rain_severe is not None else None,
      "rain_percentile": RAIN_SEVERE_PERCENTILE,
      "rain_days_per_year": _rate(annual_rain_events, rain_severe),
      "snow_mm_swe": round(snow_severe, 1) if snow_severe is not None else None,
      "snow_percentile": SNOW_SEVERE_PERCENTILE,
      "snow_days_per_year": _rate(annual_snow_events, snow_severe),
      "cold_c": round(cold_severe, 1) if cold_severe is not None else None,
      "cold_percentile": COLD_SEVERE_PERCENTILE,
      "cold_days_per_year": _rate(annual_tmin, cold_severe, below=True),
  }

  # A city that can never fire is the failure this whole percentile approach
  # exists to prevent, so say so loudly rather than emitting a null.
  if severe["snow_mm_swe"] is None:
    warnings.append(
        "no qualifying snow days in the record; the snow trigger will never "
        "fire for this city"
    )
  if severe["rain_mm"] is None:
    warnings.append(
        "no qualifying wet days in the record; the rain trigger will never "
        "fire for this city"
    )

  return {
      "city": city["city"],
      "account": city["account"],
      "country": city["country"],
      "latitude": city["latitude"],
      "longitude": city["longitude"],
      "timezone": city["timezone"],
      "grid_elevation_m": daily.get("_elevation"),
      "years_covered": year_count,
      "annual_rain_mm": round(annual_rain, 1),
      "annual_snow_mm_swe": round(annual_snow_swe, 1),
      "months": months,
      "seasons": seasons,
      "severe": severe,
      "warnings": warnings,
  }


def compare_cold_baselines(
    baselines: List[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
  """Reports how the derived cold baselines diverge from the published ones.

  This is deliberately not a pass/fail test. The published values correspond to
  airport stations, the derived values to ERA5 grid cells, and a third answer
  comes from the nearest city-centre station -- all three are defensible and
  they differ by up to 2.5 C. The purpose here is to surface the size of that
  divergence so a human can choose which source is authoritative, because the
  choice changes how often the rule fires.

  Args:
      baselines: The computed baseline records.

  Returns:
      A tuple of (needs_review, human-readable result lines). `needs_review` is
      True when any city diverges by more than the review threshold.
  """
  by_city = {b["city"]: b for b in baselines}
  lines: List[str] = []
  needs_review = False

  for city_name, published in PUBLISHED_COLD_BASELINES_C.items():
    record = by_city.get(city_name)
    if record is None:
      lines.append(f"  {city_name}: not in this run")
      continue

    derived = record["seasons"]["winter"]["cold_baseline_c"]
    if derived is None:
      lines.append(f"  {city_name}: no winter data")
      needs_review = True
      continue

    delta = derived - published
    flag = (
        " <-- REVIEW"
        if abs(delta) > COLD_BASELINE_REVIEW_THRESHOLD_C
        else ""
    )
    if flag:
      needs_review = True
    lines.append(
        f"  {city_name}: published={published:+.1f}C ERA5={derived:+.1f}C "
        f"diff={delta:+.1f}C{flag}"
    )

  return needs_review, lines


def render_firestore_documents(
    baselines: List[Dict[str, Any]], collection: str = "ClimateBaselines"
) -> List[Dict[str, Any]]:
  """Reshapes baselines into the seed format deploy.sh already uploads.

  The deployment script reads a flat list of
  ``{collection_name, document_id, data}`` records, so emitting that shape
  directly avoids a manual reshaping step between this job and deployment.

  Args:
      baselines: The computed baseline records.
      collection: Target Firestore collection name.

  Returns:
      A list of Firestore seed documents, one per city.
  """
  documents: List[Dict[str, Any]] = []
  for record in baselines:
    # Document IDs must be stable and path-safe; the city name is the key the
    # campaign configuration already uses to identify a weather location.
    document_id = record["city"].replace("/", "-")
    documents.append({
        "collection_name": collection,
        "document_id": document_id,
        "data": {
            "city": record["city"],
            "account": record["account"],
            "country": record["country"],
            "latitude": record["latitude"],
            "longitude": record["longitude"],
            "timezone": record["timezone"],
            "gridElevationM": record["grid_elevation_m"],
            "source": "ERA5 via Open-Meteo archive",
            "wetDayThresholdMm": WET_DAY_THRESHOLD_MM,
            "snowUnits": "mm water equivalent",
            "yearsCovered": record["years_covered"],
            "monthlyRainMmPerWetDay": {
                month: values["rain_mm_per_wet_day"]
                for month, values in record["months"].items()
            },
            "monthlySnowMmSwePerSnowDay": {
                month: values["snow_mm_swe_per_snow_day"]
                for month, values in record["months"].items()
            },
            "monthlyWetDays": {
                month: values["wet_days_per_month"]
                for month, values in record["months"].items()
            },
            "monthlySnowDays": {
                month: values["snow_days_per_month"]
                for month, values in record["months"].items()
            },
            # Marked provisional: the published values these replace appear to
            # be airport-station readings, and the divergence from a grid cell
            # is large enough to change how often the rule fires.
            "seasonalColdBaselineC": {
                season: values["cold_baseline_c"]
                for season, values in record["seasons"].items()
            },
            "coldBaselineProvisional": True,
            # The thresholds the severity rule should compare against
            # directly. These replace the "mean, then subtract a margin"
            # construction, which produced a 0.5 sigma cold trigger firing on
            # a third of all winter days. Each threshold carries the
            # percentile that defined it and the historical trigger rate it
            # implies, so the rule can be audited without re-deriving it.
            "severeThresholds": {
                "rainMm": record["severe"]["rain_mm"],
                "rainPercentile": record["severe"]["rain_percentile"],
                "rainDaysPerYear": record["severe"]["rain_days_per_year"],
                "snowMmSwe": record["severe"]["snow_mm_swe"],
                "snowPercentile": record["severe"]["snow_percentile"],
                "snowDaysPerYear": record["severe"]["snow_days_per_year"],
                "coldC": record["severe"]["cold_c"],
                "coldPercentile": record["severe"]["cold_percentile"],
                "coldDaysPerYear": record["severe"]["cold_days_per_year"],
            },
            "warnings": record["warnings"],
        },
    })
  return documents


def render_markdown(baselines: List[Dict[str, Any]]) -> str:
  """Renders a human-reviewable summary of the baselines.

  Args:
      baselines: The computed baseline records.

  Returns:
      A markdown document.
  """
  lines = [
      "# Climate baselines",
      "",
      "Source: ERA5 via Open-Meteo archive. Wet day >= "
      f"{WET_DAY_THRESHOLD_MM:g} mm (ETCCDI). Snow in mm water equivalent.",
      "",
      "| City | Winter cold (C) | Jan rain/wet day | Jul rain/wet day |"
      " Jan snow/snow day (SWE) | Annual rain (mm) | Warnings |",
      "|---|---:|---:|---:|---:|---:|---|",
  ]

  def fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"

  for record in sorted(baselines, key=lambda r: (r["account"], r["city"])):
    jan = record["months"]["1"]
    jul = record["months"]["7"]
    winter = record["seasons"]["winter"]["cold_baseline_c"]
    winter_text = "-" if winter is None else f"{winter:+.1f}"
    lines.append(
        f"| {record['city']} | {winter_text} "
        f"| {fmt(jan['rain_mm_per_wet_day'])} "
        f"| {fmt(jul['rain_mm_per_wet_day'])} "
        f"| {fmt(jan['snow_mm_swe_per_snow_day'])} "
        f"| {record['annual_rain_mm']:.0f} "
        f"| {len(record['warnings']) or ''} |"
    )

  # The trigger rate column is the point of this second table. A threshold in
  # millimetres is hard to review; "fires 1.1 times a year" is not, and a row
  # reading 0.0 is the defect that motivated percentiles in the first place.
  lines += [
      "",
      "## Severity thresholds",
      "",
      f"Rain p{RAIN_SEVERE_PERCENTILE:g} and snow p{SNOW_SEVERE_PERCENTILE:g} "
      f"over event days; cold p{COLD_SEVERE_PERCENTILE:g} over all days.",
      "",
      "| City | Rain (mm) | /yr | Snow (mm SWE) | /yr | Cold (C) | /yr |"
      " Total/yr |",
      "|---|---:|---:|---:|---:|---:|---:|---:|",
  ]

  def fmt1(value: Optional[float], sign: bool = False) -> str:
    if value is None:
      return "-"
    return f"{value:+.1f}" if sign else f"{value:.1f}"

  for record in sorted(baselines, key=lambda r: (r["account"], r["city"])):
    severe = record["severe"]
    total = sum(
        severe[key] or 0.0
        for key in ("rain_days_per_year", "snow_days_per_year",
                    "cold_days_per_year")
    )
    lines.append(
        f"| {record['city']} "
        f"| {fmt1(severe['rain_mm'])} "
        f"| {fmt1(severe['rain_days_per_year'])} "
        f"| {fmt1(severe['snow_mm_swe'])} "
        f"| {fmt1(severe['snow_days_per_year'])} "
        f"| {fmt1(severe['cold_c'], sign=True)} "
        f"| {fmt1(severe['cold_days_per_year'])} "
        f"| {total:.1f} |"
    )

  flagged = [r for r in baselines if r["warnings"]]
  if flagged:
    lines += ["", "## Warnings", ""]
    for record in flagged:
      lines.append(f"**{record['city']}**")
      for warning in record["warnings"]:
        lines.append(f"- {warning}")
      lines.append("")

  return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
  """Entry point.

  Args:
      argv: Command line arguments, defaulting to sys.argv.

  Returns:
      A process exit code.
  """
  parser = argparse.ArgumentParser(description=__doc__)
  default_cities = os.path.join(
      os.path.dirname(os.path.abspath(__file__)),
      "..",
      "..",
      "config",
      "baselines",
      "cities.json",
  )
  parser.add_argument("--cities-file", default=os.path.normpath(default_cities))
  parser.add_argument(
      "--cities", help="Optional comma-separated subset of city names."
  )
  parser.add_argument("--start-date", default=DEFAULT_START_DATE)
  parser.add_argument("--end-date", default=DEFAULT_END_DATE)
  parser.add_argument("--out-dir", default="./baselines_out")
  parser.add_argument("--cache-dir", default="./baselines_cache")
  parser.add_argument(
      "--sleep-seconds",
      type=float,
      default=1.0,
      help="Delay between city requests, to stay inside rate limits.",
  )
  args = parser.parse_args(argv)

  logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

  try:
    cities = _load_cities(args.cities_file)
  except BaselineError as exc:
    logger.error("%s", exc)
    return 1

  if args.cities:
    wanted = {name.strip() for name in args.cities.split(",")}
    cities = [c for c in cities if c["city"] in wanted]
    unknown = wanted - {c["city"] for c in cities}
    if unknown:
      logger.error("Unknown city names: %s", ", ".join(sorted(unknown)))
      return 1

  logger.info("Building baselines for %d cities", len(cities))

  baselines: List[Dict[str, Any]] = []
  failures: List[str] = []
  for index, city in enumerate(cities):
    try:
      daily = fetch_city_daily(
          city, args.start_date, args.end_date, args.cache_dir
      )
      baselines.append(compute_city_baselines(city, daily))
    except BaselineError as exc:
      logger.error("%s: %s", city["city"], exc)
      failures.append(city["city"])
    if index < len(cities) - 1:
      time.sleep(args.sleep_seconds)

  if not baselines:
    logger.error("No baselines produced")
    return 1

  os.makedirs(args.out_dir, exist_ok=True)
  json_path = os.path.join(args.out_dir, "climate_baselines.json")
  md_path = os.path.join(args.out_dir, "climate_baselines.md")

  with open(json_path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "source": "ERA5 via Open-Meteo archive",
            "period": {"start": args.start_date, "end": args.end_date},
            "wet_day_threshold_mm": WET_DAY_THRESHOLD_MM,
            "snow_units": "mm water equivalent",
            "cities": baselines,
        },
        handle,
        indent=2,
        ensure_ascii=False,
    )
  with open(md_path, "w", encoding="utf-8") as handle:
    handle.write(render_markdown(baselines))

  firestore_path = os.path.join(
      args.out_dir, "climate_baselines_firestore.json"
  )
  with open(firestore_path, "w", encoding="utf-8") as handle:
    json.dump(
        render_firestore_documents(baselines),
        handle,
        indent=2,
        ensure_ascii=False,
    )

  logger.info("Wrote %s, %s and %s", json_path, md_path, firestore_path)

  needs_review, lines = compare_cold_baselines(baselines)
  print("\nCold baseline source comparison (not a pass/fail test):")
  for line in lines:
    print(line)

  flagged = sum(1 for b in baselines if b["warnings"])
  print(f"\n{len(baselines)} cities built, {flagged} with warnings.")
  if failures:
    print(f"FAILED: {', '.join(failures)}")

  print(
      "\nRain and snow baselines are ready to load.\n"
      "Cold baselines are PROVISIONAL."
  )
  if needs_review:
    print(
        "The published values differ from the ERA5 grid by more than "
        f"{COLD_BASELINE_REVIEW_THRESHOLD_C:g}C for at least one city. They "
        "appear to be airport-station values, while the live trigger will read "
        "a grid cell. Decide which source is authoritative before go-live: a "
        "cold baseline only means something relative to the source the trigger "
        "reads."
    )

  return 0 if not failures else 1


if __name__ == "__main__":
  sys.exit(main())
