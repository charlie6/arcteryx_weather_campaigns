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
"""Tests for the climate baseline builder.

These exercise the reduction logic against synthetic daily records, so they
run offline. The network path is covered separately by the live validation
against the specification's published cold baselines.
"""

import datetime
import importlib.util
import os
import unittest
from typing import Any, Dict, List, Optional

# The builder is an infra script rather than a package module, so it is loaded
# by path instead of imported.
_SCRIPT_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "infra",
        "scripts",
        "baselines",
        "build_climate_baselines.py",
    )
)
_SPEC = importlib.util.spec_from_file_location(
    "build_climate_baselines", _SCRIPT_PATH
)
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)


CITY = {
    "city": "Testville",
    "account": "Canada",
    "country": "CA",
    "latitude": 49.0,
    "longitude": -123.0,
    "timezone": "America/Vancouver",
}


def make_daily(
    rain: Optional[List[float]] = None,
    snow_cm: Optional[List[float]] = None,
    tmin: Optional[List[float]] = None,
    start: str = "2020-01-01",
    days: int = 31,
) -> Dict[str, Any]:
  """Builds a synthetic `daily` block covering consecutive days.

  Args:
      rain: Daily rain in mm; defaults to all zeros.
      snow_cm: Daily snowfall in cm; defaults to all zeros.
      tmin: Daily minimum temperature in C; defaults to all zeros.
      start: ISO date of the first day.
      days: Number of consecutive days to generate.

  Returns:
      A dictionary shaped like the archive's `daily` block.
  """
  first = datetime.date.fromisoformat(start)
  times = [
      (first + datetime.timedelta(days=i)).isoformat() for i in range(days)
  ]
  zeros = [0.0] * days
  return {
      "time": times,
      "precipitation_sum": list(rain or zeros),
      "rain_sum": list(rain or zeros),
      "snowfall_sum": list(snow_cm or zeros),
      "temperature_2m_min": list(tmin or zeros),
      "temperature_2m_max": list(zeros),
      "_elevation": 73.0,
  }


class TestWetDayThreshold(unittest.TestCase):
  """The 1 mm threshold is what keeps drizzle out of the denominator."""

  def test_sub_threshold_days_are_not_wet_days(self):
    # Twenty-eight days of drizzle and three real rain days. Counting the
    # drizzle would drag the intensity down by roughly an order of magnitude.
    rain = [0.2] * 28 + [10.0, 20.0, 30.0]
    result = builder.compute_city_baselines(
        CITY, make_daily(rain=rain, days=31)
    )
    january = result["months"]["1"]

    self.assertEqual(january["wet_days_per_month"], 3.0)
    self.assertAlmostEqual(january["rain_mm_per_wet_day"], 20.0, places=2)

  def test_exactly_one_millimetre_counts_as_wet(self):
    result = builder.compute_city_baselines(
        CITY, make_daily(rain=[1.0] + [0.0] * 30, days=31)
    )
    self.assertEqual(result["months"]["1"]["wet_days_per_month"], 1.0)

  def test_just_under_threshold_does_not_count(self):
    result = builder.compute_city_baselines(
        CITY, make_daily(rain=[0.99] + [0.0] * 30, days=31)
    )
    self.assertEqual(result["months"]["1"]["wet_days_per_month"], 0.0)
    self.assertIsNone(result["months"]["1"]["rain_mm_per_wet_day"])


class TestSnowWaterEquivalent(unittest.TestCase):
  """Snow must never be expressed in centimetres downstream."""

  def test_centimetres_are_converted_to_water_equivalent(self):
    # 10 cm at Open-Meteo's 7:1 convention is 7 mm of liquid water.
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[10.0] + [0.0] * 30, days=31)
    )
    january = result["months"]["1"]
    self.assertAlmostEqual(
        january["snow_mm_swe_per_snow_day"], 7.0, places=2
    )

  def test_snow_day_threshold_applies_in_water_equivalent(self):
    # 1.0 cm -> 0.7 mm SWE, below the 1 mm threshold, so not a snow day.
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[1.0] + [0.0] * 30, days=31)
    )
    self.assertEqual(result["months"]["1"]["snow_days_per_month"], 0.0)

    # 2.0 cm -> 1.4 mm SWE, which does qualify.
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[2.0] + [0.0] * 30, days=31)
    )
    self.assertEqual(result["months"]["1"]["snow_days_per_month"], 1.0)

  def test_snow_is_reported_separately_from_rain(self):
    result = builder.compute_city_baselines(
        CITY,
        make_daily(
            rain=[5.0] + [0.0] * 30, snow_cm=[10.0] + [0.0] * 30, days=31
        ),
    )
    january = result["months"]["1"]
    self.assertAlmostEqual(january["rain_mm_per_wet_day"], 5.0, places=2)
    self.assertAlmostEqual(january["snow_mm_swe_per_snow_day"], 7.0, places=2)


class TestColdBaseline(unittest.TestCase):

  def test_winter_baseline_is_mean_daily_minimum(self):
    result = builder.compute_city_baselines(
        CITY, make_daily(tmin=[0.0, 2.0, 4.0] + [3.0] * 28, days=31)
    )
    expected = (0.0 + 2.0 + 4.0 + 3.0 * 28) / 31
    self.assertAlmostEqual(
        result["seasons"]["winter"]["cold_baseline_c"],
        round(expected, 1),
        places=1,
    )

  def test_missing_temperatures_are_skipped_not_zeroed(self):
    daily = make_daily(days=3)
    daily["temperature_2m_min"] = [-5.0, None, -7.0]
    result = builder.compute_city_baselines(CITY, daily)
    # Treating None as 0 would give -4.0 and badly understate the cold.
    self.assertAlmostEqual(
        result["months"]["1"]["mean_daily_min_c"], -6.0, places=2
    )


class TestMissingDataDetection(unittest.TestCase):
  """The failure mode that motivated this script: gaps encoded as zero."""

  def test_all_zero_precipitation_is_flagged(self):
    result = builder.compute_city_baselines(
        CITY, make_daily(days=31)
    )
    self.assertTrue(
        any("zero wet days" in w for w in result["warnings"]),
        result["warnings"],
    )

  def test_implausibly_low_annual_total_is_flagged(self):
    result = builder.compute_city_baselines(
        CITY, make_daily(rain=[2.0] + [0.0] * 30, days=31)
    )
    self.assertTrue(
        any("implausibly low" in w for w in result["warnings"]),
        result["warnings"],
    )

  def test_realistic_record_is_not_flagged_for_low_total(self):
    # Roughly 600 mm spread over a year of consecutive days.
    rain = [5.0 if i % 3 == 0 else 0.0 for i in range(366)]
    result = builder.compute_city_baselines(
        CITY, make_daily(rain=rain, start="2020-01-01", days=366)
    )
    self.assertFalse(
        any("implausibly low" in w for w in result["warnings"]),
        result["warnings"],
    )

  def test_missing_variable_raises(self):
    daily = make_daily(days=3)
    del daily["rain_sum"]
    with self.assertRaises(builder.BaselineError):
      builder.compute_city_baselines(CITY, daily)


class TestPerYearNormalisation(unittest.TestCase):

  def test_wet_days_are_averaged_across_years(self):
    # Two Januaries, each with 5 wet days, should report 5 per month.
    rain = [10.0] * 5 + [0.0] * 26
    daily = make_daily(rain=rain, start="2019-01-01", days=31)
    second = make_daily(rain=rain, start="2020-01-01", days=31)
    for key in ("time", "precipitation_sum", "rain_sum", "snowfall_sum",
                "temperature_2m_min", "temperature_2m_max"):
      daily[key] = daily[key] + second[key]

    result = builder.compute_city_baselines(CITY, daily)
    self.assertEqual(result["years_covered"], 2)
    self.assertEqual(result["months"]["1"]["wet_days_per_month"], 5.0)


class TestColdBaselineComparison(unittest.TestCase):
  """The comparison reports divergence; it does not pass or fail a target."""

  def _record(self, city: str, winter_c: Optional[float]) -> Dict[str, Any]:
    return {"city": city, "seasons": {"winter": {"cold_baseline_c": winter_c}}}

  def test_close_agreement_needs_no_review(self):
    needs_review, _ = builder.compare_cold_baselines([
        self._record("Vancouver BC", 2.0),
        self._record("Chicago IL", -9.0),
        self._record("Los Angeles CA", 9.0),
    ])
    self.assertFalse(needs_review)

  def test_small_divergence_needs_no_review(self):
    needs_review, _ = builder.compare_cold_baselines(
        [self._record("Vancouver BC", 3.4)]
    )
    self.assertFalse(needs_review)

  def test_large_divergence_is_flagged_for_review(self):
    needs_review, lines = builder.compare_cold_baselines(
        [self._record("Los Angeles CA", 7.0)]
    )
    self.assertTrue(needs_review)
    self.assertTrue(any("REVIEW" in line for line in lines))

  def test_report_shows_signed_difference(self):
    _, lines = builder.compare_cold_baselines(
        [self._record("Chicago IL", -7.2)]
    )
    # ERA5 reads warmer than the published airport value, so the sign matters.
    self.assertTrue(any("+1.8" in line for line in lines), lines)

  def test_absent_city_is_reported_not_flagged(self):
    needs_review, lines = builder.compare_cold_baselines(
        [self._record("Vancouver BC", 2.0)]
    )
    self.assertFalse(needs_review)
    self.assertTrue(any("not in this run" in line for line in lines))

  def test_missing_winter_data_needs_review(self):
    needs_review, _ = builder.compare_cold_baselines(
        [self._record("Vancouver BC", None)]
    )
    self.assertTrue(needs_review)


if __name__ == "__main__":
  unittest.main()
