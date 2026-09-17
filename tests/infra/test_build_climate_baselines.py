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
    # Open-Meteo's convention is 7 cm of snow per 10 mm of liquid water, so
    # 10 cm of snow is 14.29 mm SWE. An earlier revision inverted this and
    # produced 7.0, halving every stored snow baseline.
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[10.0] + [0.0] * 30, days=31)
    )
    january = result["months"]["1"]
    self.assertAlmostEqual(
        january["snow_mm_swe_per_snow_day"], 14.29, places=2
    )

  def test_conversion_increases_the_magnitude(self):
    """Pins the direction of the conversion, independently of its exact value.

    Snow depth is reported in centimetres and water equivalent in
    millimetres. Those units differ by a factor of ten while the snow-to-
    liquid ratio is seven, so the SWE figure is always the larger number for
    any ratio under 10:1. The inverted conversion breaks that invariant,
    which makes this a cheap guard that survives a change of ratio.
    """
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[10.0] + [0.0] * 30, days=31)
    )
    swe = result["months"]["1"]["snow_mm_swe_per_snow_day"]
    self.assertGreater(swe, 10.0, "inverted conversion would give 7.0")

  def test_matches_observed_open_meteo_ratio(self):
    """Cross-checks against a real archive response.

    Saskatoon 2013-03-15 returned 4.48 cm snowfall against 6.10 mm
    precipitation on a day with no rain, so the archive's own SWE for that day
    is 6.10 mm. Reconstructing from centimetres gives 6.4 mm: the nominal 7:1
    convention is a rounding of an observed ratio that runs 0.73-0.75, and
    snowfall_sum is itself reported to two decimal places. The tolerance is
    therefore loose enough to absorb that, but far tighter than the factor of
    two an inverted conversion would introduce.
    """
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[4.48] + [0.0] * 30, days=31)
    )
    swe = result["months"]["1"]["snow_mm_swe_per_snow_day"]
    self.assertAlmostEqual(swe, 6.10, delta=0.5)

  def test_snow_day_threshold_applies_in_water_equivalent(self):
    # 0.6 cm -> 0.86 mm SWE, below the 1 mm threshold, so not a snow day.
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[0.6] + [0.0] * 30, days=31)
    )
    self.assertEqual(result["months"]["1"]["snow_days_per_month"], 0.0)

    # 0.8 cm -> 1.14 mm SWE, which does qualify.
    result = builder.compute_city_baselines(
        CITY, make_daily(snow_cm=[0.8] + [0.0] * 30, days=31)
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
    self.assertAlmostEqual(
        january["snow_mm_swe_per_snow_day"], 14.29, places=2
    )


class TestPercentileHelper(unittest.TestCase):
  """The percentile function underpinning every severity threshold."""

  def test_empty_sample_is_none(self):
    self.assertIsNone(builder._percentile([], 95.0))

  def test_single_observation_is_returned_unchanged(self):
    self.assertEqual(builder._percentile([7.0], 95.0), 7.0)

  def test_median_of_odd_sample(self):
    self.assertEqual(builder._percentile([1.0, 2.0, 3.0], 50.0), 2.0)

  def test_median_of_even_sample_interpolates(self):
    self.assertEqual(builder._percentile([1.0, 2.0, 3.0, 4.0], 50.0), 2.5)

  def test_zero_and_hundred_are_min_and_max(self):
    values = [5.0, 1.0, 9.0, 3.0]
    self.assertEqual(builder._percentile(values, 0.0), 1.0)
    self.assertEqual(builder._percentile(values, 100.0), 9.0)

  def test_input_order_does_not_matter(self):
    ascending = builder._percentile([1.0, 2.0, 3.0, 4.0], 75.0)
    shuffled = builder._percentile([3.0, 1.0, 4.0, 2.0], 75.0)
    self.assertEqual(ascending, shuffled)

  def test_input_is_not_mutated(self):
    values = [3.0, 1.0, 2.0]
    builder._percentile(values, 50.0)
    self.assertEqual(values, [3.0, 1.0, 2.0])

  def test_out_of_range_percentile_is_rejected(self):
    with self.assertRaises(ValueError):
      builder._percentile([1.0, 2.0], 101.0)
    with self.assertRaises(ValueError):
      builder._percentile([1.0, 2.0], -1.0)


class TestSeverityThresholds(unittest.TestCase):
  """Thresholds are only useful if the implied trigger rate is right."""

  def test_rain_threshold_excludes_sub_wet_day_drizzle(self):
    """Drizzle must not enter the distribution the percentile is taken over.

    Including dry and near-dry days would collapse the tail toward zero and
    make the threshold fire constantly.
    """
    rain = [0.2] * 28 + [10.0, 20.0, 30.0]
    result = builder.compute_city_baselines(CITY, make_daily(rain=rain,
                                                             days=31))
    # Only the three real rain days define the distribution, so p98 must sit
    # inside their range rather than down among the drizzle.
    self.assertGreaterEqual(result["severe"]["rain_mm"], 10.0)

  def test_cold_threshold_is_the_lower_tail(self):
    tmin = [-30.0] + [0.0] * 30
    result = builder.compute_city_baselines(CITY, make_daily(tmin=tmin,
                                                             days=31))
    self.assertLess(result["severe"]["cold_c"], 0.0)

  def test_trigger_rates_are_reported_per_year(self):
    rain = [50.0] + [5.0] * 30
    result = builder.compute_city_baselines(CITY, make_daily(rain=rain,
                                                             days=31))
    severe = result["severe"]
    self.assertIsNotNone(severe["rain_days_per_year"])
    # One calendar year is covered, so the rate equals the raw count.
    self.assertGreaterEqual(severe["rain_days_per_year"], 1.0)

  def test_percentiles_used_are_recorded(self):
    result = builder.compute_city_baselines(CITY, make_daily(days=31))
    severe = result["severe"]
    self.assertEqual(severe["rain_percentile"], builder.RAIN_SEVERE_PERCENTILE)
    self.assertEqual(severe["snow_percentile"], builder.SNOW_SEVERE_PERCENTILE)
    self.assertEqual(severe["cold_percentile"], builder.COLD_SEVERE_PERCENTILE)

  def test_city_with_no_snow_is_warned_about(self):
    """A trigger that can never fire is the defect percentiles exist to fix."""
    result = builder.compute_city_baselines(CITY, make_daily(days=31))
    self.assertIsNone(result["severe"]["snow_mm_swe"])
    self.assertTrue(
        any("snow trigger will never fire" in w for w in result["warnings"]),
        f"expected a never-fires warning, got {result['warnings']}",
    )

  def test_city_with_no_rain_is_warned_about(self):
    result = builder.compute_city_baselines(CITY, make_daily(days=31))
    self.assertIsNone(result["severe"]["rain_mm"])
    self.assertTrue(
        any("rain trigger will never fire" in w for w in result["warnings"]),
        f"expected a never-fires warning, got {result['warnings']}",
    )

  def test_thresholds_reach_the_firestore_document(self):
    rain = [50.0] + [5.0] * 30
    record = builder.compute_city_baselines(CITY, make_daily(rain=rain,
                                                             days=31))
    documents = builder.render_firestore_documents([record])
    thresholds = documents[0]["data"]["severeThresholds"]
    self.assertEqual(thresholds["rainMm"], record["severe"]["rain_mm"])
    self.assertEqual(
        thresholds["coldPercentile"], builder.COLD_SEVERE_PERCENTILE
    )


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
