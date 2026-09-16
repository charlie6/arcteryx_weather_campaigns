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
"""Tests for deterministic 24-hour weather signal aggregation."""

import asyncio
import unittest
from unittest import mock

import requests

from agentic_dsta.tools.weather import weather_signals


MagicMock = mock.MagicMock
patch = mock.patch

# Vancouver, the worked example in the Arc'teryx specification.
VAN_LAT = 49.2827
VAN_LON = -123.1207

MODULE = "agentic_dsta.tools.weather.weather_signals"


def _hour(rain_mm=0.0, snow_mm=0.0, temp_c=None, condition=None, precip_type=None):
  """Builds a single ForecastHour payload."""
  precipitation = {}
  if rain_mm is not None:
    precipitation["qpf"] = {"quantity": rain_mm, "unit": "MILLIMETERS"}
  if snow_mm is not None:
    precipitation["snowQpf"] = {"quantity": snow_mm, "unit": "MILLIMETERS"}
  if precip_type:
    precipitation["probability"] = {"percent": 80, "type": precip_type}

  hour = {"precipitation": precipitation}
  if temp_c is not None:
    hour["temperature"] = {"degrees": temp_c, "unit": "CELSIUS"}
  if condition:
    hour["weatherCondition"] = {"type": condition}
  return hour


def _response(hours, next_page_token=None):
  """Builds a mock requests.Response for an hourly forecast page."""
  resp = MagicMock()
  payload = {"forecastHours": hours}
  if next_page_token:
    payload["nextPageToken"] = next_page_token
  resp.json.return_value = payload
  resp.raise_for_status.return_value = None
  return resp


@patch.dict("os.environ", {"GOOGLE_WEATHER_API_API_KEY": "test-key"}, clear=False)
class TestUnitConversion(unittest.TestCase):
  """Thresholds are expressed in mm and Celsius, so conversion must be exact."""

  def test_millimetres_pass_through(self):
    self.assertEqual(
        weather_signals._to_millimetres({"quantity": 12.5, "unit": "MILLIMETERS"}),
        12.5,
    )

  def test_inches_are_converted(self):
    self.assertAlmostEqual(
        weather_signals._to_millimetres({"quantity": 1.0, "unit": "INCHES"}),
        25.4,
    )

  def test_missing_precipitation_is_zero(self):
    self.assertEqual(weather_signals._to_millimetres(None), 0.0)
    self.assertEqual(weather_signals._to_millimetres({}), 0.0)
    self.assertEqual(weather_signals._to_millimetres({"unit": "MILLIMETERS"}), 0.0)

  def test_non_numeric_quantity_is_zero(self):
    self.assertEqual(
        weather_signals._to_millimetres({"quantity": "lots"}), 0.0
    )

  def test_celsius_passes_through(self):
    self.assertEqual(
        weather_signals._to_celsius({"degrees": -3.0, "unit": "CELSIUS"}), -3.0
    )

  def test_fahrenheit_is_converted(self):
    self.assertAlmostEqual(
        weather_signals._to_celsius({"degrees": 32.0, "unit": "FAHRENHEIT"}), 0.0
    )

  def test_missing_temperature_is_none_not_zero(self):
    # Zero degrees is a meaningful cold reading, so absence must not look like it.
    self.assertIsNone(weather_signals._to_celsius(None))
    self.assertIsNone(weather_signals._to_celsius({}))

  def test_zero_celsius_is_preserved(self):
    self.assertEqual(
        weather_signals._to_celsius({"degrees": 0, "unit": "CELSIUS"}), 0.0
    )


class TestInputValidation(unittest.TestCase):
  """Bad coordinates must be rejected before any network call."""

  @patch(f"{MODULE}.requests.get")
  def test_invalid_latitude_rejected(self, mock_get):
    result = weather_signals.get_24h_weather_signals(91.0, VAN_LON)
    self.assertFalse(result["success"])
    self.assertIn("Invalid latitude", result["error"])
    mock_get.assert_not_called()

  @patch(f"{MODULE}.requests.get")
  def test_invalid_longitude_rejected(self, mock_get):
    result = weather_signals.get_24h_weather_signals(VAN_LAT, -181.0)
    self.assertFalse(result["success"])
    self.assertIn("Invalid longitude", result["error"])
    mock_get.assert_not_called()

  @patch(f"{MODULE}.requests.get")
  def test_invalid_hours_rejected(self, mock_get):
    for bad in (0, 241, "24"):
      with self.subTest(hours=bad):
        result = weather_signals.get_24h_weather_signals(
            VAN_LAT, VAN_LON, hours=bad
        )
        self.assertFalse(result["success"])
        self.assertIn("Invalid hours", result["error"])
    mock_get.assert_not_called()

  @patch.dict("os.environ", {}, clear=True)
  @patch(f"{MODULE}.requests.get")
  def test_missing_api_key_reported(self, mock_get):
    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON)
    self.assertFalse(result["success"])
    self.assertIn("API key", result["error"])
    mock_get.assert_not_called()


class TestApiKeyResolution(unittest.TestCase):
  """The documented variable and the API Hub-derived one differ."""

  @patch.dict("os.environ", {"GOOGLE_WEATHER_API_KEY": "documented"}, clear=True)
  def test_documented_name_is_accepted(self):
    self.assertEqual(weather_signals._get_api_key(), "documented")

  @patch.dict(
      "os.environ", {"GOOGLE_WEATHER_API_API_KEY": "derived"}, clear=True
  )
  def test_apihub_derived_name_is_accepted(self):
    self.assertEqual(weather_signals._get_api_key(), "derived")

  @patch.dict("os.environ", {"GOOGLE_API_KEY": "generic"}, clear=True)
  def test_generic_name_is_accepted(self):
    self.assertEqual(weather_signals._get_api_key(), "generic")

  @patch.dict(
      "os.environ",
      {"GOOGLE_WEATHER_API_KEY": "specific", "GOOGLE_API_KEY": "generic"},
      clear=True,
  )
  def test_weather_specific_key_wins_over_generic(self):
    self.assertEqual(weather_signals._get_api_key(), "specific")

  @patch.dict("os.environ", {}, clear=True)
  def test_no_key_returns_none(self):
    self.assertIsNone(weather_signals._get_api_key())


@patch.dict("os.environ", {"GOOGLE_WEATHER_API_API_KEY": "test-key"}, clear=False)
class TestAggregation(unittest.TestCase):

  @patch(f"{MODULE}.requests.get")
  def test_accumulations_are_summed(self, mock_get):
    hours = [_hour(rain_mm=2.0, snow_mm=0.5, temp_c=3.0) for _ in range(24)]
    mock_get.return_value = _response(hours)

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON)

    self.assertTrue(result["success"])
    self.assertAlmostEqual(result["rain_accumulation_mm"], 48.0)
    self.assertAlmostEqual(result["snow_accumulation_mm"], 12.0)
    self.assertEqual(result["hours_received"], 24)
    self.assertNotIn("partial_window", result)

  @patch(f"{MODULE}.requests.get")
  def test_min_and_max_temperature(self, mock_get):
    temps = [5.0, -3.0, 0.0, 11.0]
    mock_get.return_value = _response([_hour(temp_c=t) for t in temps])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=4)

    self.assertEqual(result["min_temperature_c"], -3.0)
    self.assertEqual(result["max_temperature_c"], 11.0)

  @patch(f"{MODULE}.requests.get")
  def test_peak_hourly_rain_rate(self, mock_get):
    mock_get.return_value = _response([
        _hour(rain_mm=0.1),
        _hour(rain_mm=4.7),
        _hour(rain_mm=0.3),
    ])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=3)

    self.assertAlmostEqual(result["max_rain_rate_mm_per_h"], 4.7)

  @patch(f"{MODULE}.requests.get")
  def test_temperatures_absent_yields_none(self, mock_get):
    mock_get.return_value = _response([_hour(rain_mm=1.0) for _ in range(3)])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=3)

    self.assertTrue(result["success"])
    self.assertIsNone(result["min_temperature_c"])
    self.assertIsNone(result["max_temperature_c"])

  @patch(f"{MODULE}.requests.get")
  def test_inch_units_are_normalised(self, mock_get):
    hour = {
        "precipitation": {"qpf": {"quantity": 2.0, "unit": "INCHES"}},
        "temperature": {"degrees": 50.0, "unit": "FAHRENHEIT"},
    }
    mock_get.return_value = _response([hour])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)

    self.assertAlmostEqual(result["rain_accumulation_mm"], 50.8)
    self.assertAlmostEqual(result["min_temperature_c"], 10.0)

  @patch(f"{MODULE}.requests.get")
  def test_condition_types_are_collected(self, mock_get):
    mock_get.return_value = _response([
        _hour(condition="SNOW"),
        _hour(condition="LIGHT_SNOW"),
        _hour(condition="SNOW"),
    ])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=3)

    self.assertEqual(result["condition_types"], ["LIGHT_SNOW", "SNOW"])


@patch.dict("os.environ", {"GOOGLE_WEATHER_API_API_KEY": "test-key"}, clear=False)
class TestPrecipitationPresence(unittest.TestCase):

  @patch(f"{MODULE}.requests.get")
  def test_snow_detected_from_accumulation(self, mock_get):
    mock_get.return_value = _response([_hour(snow_mm=0.4)])
    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)
    self.assertTrue(result["snow_present"])

  @patch(f"{MODULE}.requests.get")
  def test_snow_detected_from_condition_without_accumulation(self, mock_get):
    mock_get.return_value = _response([_hour(condition="SNOW_SHOWERS")])
    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)
    self.assertTrue(result["snow_present"])
    self.assertEqual(result["snow_accumulation_mm"], 0.0)

  @patch(f"{MODULE}.requests.get")
  def test_snow_detected_from_probability_type(self, mock_get):
    mock_get.return_value = _response([_hour(precip_type="RAIN_AND_SNOW")])
    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)
    self.assertTrue(result["snow_present"])

  @patch(f"{MODULE}.requests.get")
  def test_rain_detected_from_condition(self, mock_get):
    mock_get.return_value = _response([_hour(condition="DRIZZLE")])
    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)
    self.assertTrue(result["rain_present"])

  @patch(f"{MODULE}.requests.get")
  def test_clear_weather_reports_neither(self, mock_get):
    mock_get.return_value = _response([_hour(condition="CLEAR", temp_c=18.0)])
    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)
    self.assertFalse(result["rain_present"])
    self.assertFalse(result["snow_present"])


@patch.dict("os.environ", {"GOOGLE_WEATHER_API_API_KEY": "test-key"}, clear=False)
class TestPaginationAndFailures(unittest.TestCase):

  @patch(f"{MODULE}.requests.get")
  def test_pagination_is_followed(self, mock_get):
    page_one = _response([_hour(rain_mm=1.0) for _ in range(24)], "tok")
    page_two = _response([_hour(rain_mm=1.0) for _ in range(24)])
    mock_get.side_effect = [page_one, page_two]

    result = weather_signals.get_24h_weather_signals(
        VAN_LAT, VAN_LON, hours=48
    )

    self.assertEqual(result["hours_received"], 48)
    self.assertAlmostEqual(result["rain_accumulation_mm"], 48.0)
    self.assertEqual(mock_get.call_count, 2)

  @patch(f"{MODULE}.requests.get")
  def test_results_truncated_to_requested_hours(self, mock_get):
    # The API may return a full page even when fewer hours were asked for.
    mock_get.return_value = _response([_hour(rain_mm=1.0) for _ in range(24)])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=6)

    self.assertEqual(result["hours_received"], 6)
    self.assertAlmostEqual(result["rain_accumulation_mm"], 6.0)

  @patch(f"{MODULE}.requests.get")
  def test_short_window_is_flagged(self, mock_get):
    mock_get.return_value = _response([_hour(rain_mm=1.0) for _ in range(5)])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=24)

    self.assertTrue(result["success"])
    self.assertTrue(result["partial_window"])
    self.assertEqual(result["hours_received"], 5)

  @patch(f"{MODULE}.requests.get")
  def test_empty_response_reports_failure(self, mock_get):
    mock_get.return_value = _response([])

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON)

    self.assertFalse(result["success"])
    self.assertIn("no forecast hours", result["error"])

  @patch(f"{MODULE}.requests.get")
  def test_http_error_is_caught(self, mock_get):
    mock_get.side_effect = requests.HTTPError("403 Forbidden")

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON)

    self.assertFalse(result["success"])
    self.assertIn("request failed", result["error"])

  @patch(f"{MODULE}.requests.get")
  def test_timeout_is_caught(self, mock_get):
    mock_get.side_effect = requests.Timeout("timed out")

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON)

    self.assertFalse(result["success"])
    self.assertIn("request failed", result["error"])

  @patch(f"{MODULE}.requests.get")
  def test_malformed_json_is_caught(self, mock_get):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.side_effect = ValueError("not json")
    mock_get.return_value = resp

    result = weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON)

    self.assertFalse(result["success"])
    self.assertIn("Malformed", result["error"])

  @patch(f"{MODULE}.requests.get")
  def test_metric_units_are_requested(self, mock_get):
    mock_get.return_value = _response([_hour(rain_mm=1.0)])

    weather_signals.get_24h_weather_signals(VAN_LAT, VAN_LON, hours=1)

    params = mock_get.call_args.kwargs["params"]
    self.assertEqual(params["unitsSystem"], "METRIC")
    self.assertEqual(params["location.latitude"], VAN_LAT)
    self.assertEqual(params["hours"], 1)


class TestToolsetRegistration(unittest.TestCase):

  def test_toolset_exposes_signal_tool(self):
    toolset = weather_signals.WeatherSignalsToolset()
    tools = asyncio.run(toolset.get_tools())
    self.assertEqual(len(tools), 1)
    self.assertEqual(tools[0].name, "get_24h_weather_signals")


if __name__ == "__main__":
  unittest.main()
