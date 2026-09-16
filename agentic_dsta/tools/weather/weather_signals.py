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
"""Deterministic 24-hour weather signal aggregation.

The weather-triggered campaign rules depend on quantities that must be derived
by arithmetic over an hourly forecast, not judged by a language model:

  * rolling 24-hour rain accumulation, compared against an absolute floor;
  * rolling 24-hour snow accumulation, compared against an absolute floor;
  * the forecast daily low, compared against a seasonal baseline;
  * the peak hourly rain rate, compared against an activation threshold.

Because these values decide whether real budget moves, this module fetches the
hourly forecast and reduces it to scalars in Python. The agent receives a small
set of finished numbers rather than 24 raw records, which keeps the decision
reproducible and auditable, and avoids spending context on forecast rows.

Units
-----
Requests are issued with ``unitsSystem=METRIC``, so temperatures are Celsius and
precipitation is millimetres. Responses are nonetheless converted defensively,
since the API may return ``INCHES`` or ``FAHRENHEIT`` if that default changes.

``snowQpf`` is a LIQUID WATER EQUIVALENT, not a snow depth. Roughly a 10:1 ratio
applies, so 10 mm here corresponds to on the order of 10 cm of snowfall. Any
severe-snow threshold compared against ``snow_accumulation_mm`` must be
expressed in the same liquid-equivalent terms.

Configuration
-------------
``GOOGLE_WEATHER_API_KEY``, ``GOOGLE_WEATHER_API_API_KEY`` or ``GOOGLE_API_KEY``
    API key for the Google Weather API, checked in that order. Two weather-
    specific names are accepted because ``.env.example`` documents the first
    while the API Hub toolset derives the second from the registered API
    display name.
"""

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

logger = logging.getLogger(__name__)

_WEATHER_API_BASE_URL = "https://weather.googleapis.com/v1"

# The API caps a single page at 24 hourly records, which is exactly the window
# the rules need, so a 24-hour request is normally a single round trip.
_MAX_PAGE_SIZE = 24
_DEFAULT_WINDOW_HOURS = 24
_MAX_WINDOW_HOURS = 240

_REQUEST_TIMEOUT_SECONDS = 30

# weatherCondition.type values that mean frozen precipitation is falling.
_SNOW_CONDITION_TYPES = frozenset({
    "SNOW",
    "LIGHT_SNOW",
    "SNOW_SHOWERS",
    "HEAVY_SNOW",
    "SNOWSTORM",
    "BLOWING_SNOW",
    "SLEET",
    "FREEZING_RAIN",
})

# weatherCondition.type values that mean liquid precipitation is falling.
_RAIN_CONDITION_TYPES = frozenset({
    "RAIN",
    "LIGHT_RAIN",
    "HEAVY_RAIN",
    "RAIN_SHOWERS",
    "SCATTERED_SHOWERS",
    "DRIZZLE",
    "THUNDERSTORMS",
})

# precipitation.probability.type values indicating snow.
_SNOW_PROBABILITY_TYPES = frozenset({"SNOW", "RAIN_AND_SNOW", "FREEZING_RAIN", "SLEET"})

# precipitation.probability.type values indicating rain.
_RAIN_PROBABILITY_TYPES = frozenset({"RAIN", "RAIN_AND_SNOW", "FREEZING_RAIN"})

# Minimum precipitation.probability.percent for the probability type to count as
# evidence of precipitation. The API populates probability.type with the kind of
# precipitation that *would* fall, and leaves it set even when percent is 0, so
# reading the type alone reports rain on a clear day.
_PRECIPITATION_PROBABILITY_THRESHOLD_PERCENT = 50.0

_MM_PER_INCH = 25.4


def _get_api_key() -> Optional[str]:
  """Returns the configured Google Weather API key, if any.

  Three names are accepted because the repository is inconsistent about which
  one to use. ``.env.example`` documents ``GOOGLE_WEATHER_API_KEY``, whereas the
  API Hub toolset derives ``GOOGLE_WEATHER_API_API_KEY`` from the registered API
  display name. Reading both avoids silently falling back to the generic key
  when only one has been set.

  Returns:
      The API key, or None when no candidate variable is set.
  """
  for name in (
      "GOOGLE_WEATHER_API_KEY",
      "GOOGLE_WEATHER_API_API_KEY",
      "GOOGLE_API_KEY",
  ):
    value = os.environ.get(name)
    if value:
      return value
  return None


def _to_millimetres(qpf: Optional[Dict[str, Any]]) -> float:
  """Converts a QuantitativePrecipitationForecast to millimetres.

  Args:
      qpf: A ``qpf``/``snowQpf`` object, or None when the field is absent.

  Returns:
      The accumulation in millimetres. Missing or unparseable values yield 0.0,
      so a partial forecast under-reports rather than aborting the run.
  """
  if not isinstance(qpf, dict):
    return 0.0

  quantity = qpf.get("quantity")
  if not isinstance(quantity, (int, float)):
    return 0.0

  if qpf.get("unit") == "INCHES":
    return float(quantity) * _MM_PER_INCH
  return float(quantity)


def _to_celsius(temperature: Optional[Dict[str, Any]]) -> Optional[float]:
  """Converts a Temperature object to Celsius.

  Args:
      temperature: A ``Temperature`` object, or None when the field is absent.

  Returns:
      The temperature in Celsius, or None when it cannot be determined. None is
      distinct from 0.0 here because 0 degrees is a meaningful cold reading.
  """
  if not isinstance(temperature, dict):
    return None

  degrees = temperature.get("degrees")
  if not isinstance(degrees, (int, float)):
    return None

  if temperature.get("unit") == "FAHRENHEIT":
    return (float(degrees) - 32.0) * 5.0 / 9.0
  return float(degrees)


def _fetch_hourly_forecast(
    latitude: float,
    longitude: float,
    hours: int,
    api_key: str,
) -> List[Dict[str, Any]]:
  """Retrieves hourly forecast records, following pagination as needed.

  Args:
      latitude: Latitude in degrees (WGS84).
      longitude: Longitude in degrees (WGS84).
      hours: Number of hours to retrieve, starting from the current hour.
      api_key: Google Weather API key.

  Returns:
      A list of ForecastHour dictionaries, at most ``hours`` long.

  Raises:
      requests.HTTPError: If the API returns a non-2xx status.
      requests.RequestException: On network or timeout failures.
  """
  collected: List[Dict[str, Any]] = []
  page_token: Optional[str] = None

  while len(collected) < hours:
    params: Dict[str, Any] = {
        "key": api_key,
        "location.latitude": latitude,
        "location.longitude": longitude,
        "hours": hours,
        "pageSize": min(_MAX_PAGE_SIZE, hours),
        "unitsSystem": "METRIC",
    }
    if page_token:
      params["pageToken"] = page_token

    response = requests.get(
        f"{_WEATHER_API_BASE_URL}/forecast/hours:lookup",
        params=params,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    page = payload.get("forecastHours") or []
    if not page:
      # No further data available; return what we have rather than looping.
      break

    collected.extend(page)

    page_token = payload.get("nextPageToken")
    if not page_token:
      break

  return collected[:hours]


def _probability_indicates(hour: Dict[str, Any], types: frozenset) -> bool:
  """Reports whether the precipitation probability points at a given type.

  The probability type alone is not sufficient evidence. The API describes the
  kind of precipitation that would fall if any did, and leaves the field
  populated when the chance is zero, so a clear day still reports
  ``type: "RAIN"``. The percentage must therefore also clear a threshold.

  Args:
      hour: A ForecastHour dictionary.
      types: The precipitation probability types that count as a match.

  Returns:
      True if the probability type matches and the percentage is at least
      ``_PRECIPITATION_PROBABILITY_THRESHOLD_PERCENT``.
  """
  probability = (hour.get("precipitation") or {}).get("probability") or {}
  if probability.get("type") not in types:
    return False

  try:
    percent = float(probability.get("percent"))
  except (TypeError, ValueError):
    # A missing or non-numeric percentage is not evidence of precipitation.
    return False

  return percent >= _PRECIPITATION_PROBABILITY_THRESHOLD_PERCENT


def _hour_is_snow(hour: Dict[str, Any], snow_mm: float) -> bool:
  """Reports whether an hourly record represents snowfall.

  Args:
      hour: A ForecastHour dictionary.
      snow_mm: The already-converted snow accumulation for that hour.

  Returns:
      True if the snow accumulation is positive, the condition type names snow,
      or a sufficiently likely snow probability is forecast.
  """
  if snow_mm > 0:
    return True

  condition = (hour.get("weatherCondition") or {}).get("type")
  if condition in _SNOW_CONDITION_TYPES:
    return True

  return _probability_indicates(hour, _SNOW_PROBABILITY_TYPES)


def _hour_is_rain(hour: Dict[str, Any], rain_mm: float) -> bool:
  """Reports whether an hourly record represents rainfall.

  Args:
      hour: A ForecastHour dictionary.
      rain_mm: The already-converted rain accumulation for that hour.

  Returns:
      True if the rain accumulation is positive, the condition type names rain,
      or a sufficiently likely rain probability is forecast.
  """
  if rain_mm > 0:
    return True

  condition = (hour.get("weatherCondition") or {}).get("type")
  if condition in _RAIN_CONDITION_TYPES:
    return True

  return _probability_indicates(hour, _RAIN_PROBABILITY_TYPES)


def get_24h_weather_signals(
    latitude: float, longitude: float, hours: int = _DEFAULT_WINDOW_HOURS
) -> Dict[str, Any]:
  """Summarises the forecast for a location into decision-ready signals.

  Fetches the hourly forecast starting at the current hour and reduces it to
  the scalar quantities the weather rules depend on. All arithmetic happens
  here rather than in the model, so the same forecast always produces the same
  numbers.

  Args:
      latitude: Latitude in degrees (WGS84), between -90 and 90.
      longitude: Longitude in degrees (WGS84), between -180 and 180.
      hours: Size of the look-ahead window in hours, 1 to 240. Defaults to 24.

  Returns:
      A dictionary with a boolean 'success' key. On success it contains:

      rain_accumulation_mm
          Total liquid rainfall over the window, in millimetres.
      snow_accumulation_mm
          Total snowfall over the window as LIQUID WATER EQUIVALENT, in
          millimetres. Not a snow depth.
      max_rain_rate_mm_per_h
          The largest single-hour rain accumulation in the window.
      min_temperature_c, max_temperature_c
          Lowest and highest forecast temperature in the window, or None when
          no hour reported a usable temperature.
      rain_present, snow_present
          Whether any hour in the window indicates that precipitation type.
      hours_requested, hours_received
          Coverage of the window. A short response narrows the window actually
          summarised, so these should be checked before acting on a threshold.
      condition_types
          Sorted distinct ``weatherCondition.type`` values seen in the window.

      On failure, 'error' describes the problem and no other fields are
      guaranteed.
  """
  if not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
    return {"success": False, "error": f"Invalid latitude: {latitude!r}."}
  if not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
    return {"success": False, "error": f"Invalid longitude: {longitude!r}."}
  if not isinstance(hours, int) or not 1 <= hours <= _MAX_WINDOW_HOURS:
    return {
        "success": False,
        "error": f"Invalid hours: {hours!r}. Use 1 to {_MAX_WINDOW_HOURS}.",
    }

  api_key = _get_api_key()
  if not api_key:
    logger.error(
        "No Google Weather API key configured",
        extra={"latitude": latitude, "longitude": longitude},
    )
    return {
        "success": False,
        "error": (
            "No Google Weather API key configured. Set "
            "GOOGLE_WEATHER_API_API_KEY or GOOGLE_API_KEY."
        ),
    }

  try:
    forecast_hours = _fetch_hourly_forecast(latitude, longitude, hours, api_key)
  except requests.RequestException as ex:
    logger.error(
        "Hourly forecast request failed: %s",
        ex,
        exc_info=True,
        extra={"latitude": latitude, "longitude": longitude, "hours": hours},
    )
    return {"success": False, "error": f"Weather API request failed: {ex}"}
  except ValueError as ex:
    # Raised by response.json() when the body is not valid JSON.
    logger.error(
        "Hourly forecast response was not valid JSON: %s",
        ex,
        exc_info=True,
        extra={"latitude": latitude, "longitude": longitude},
    )
    return {"success": False, "error": f"Malformed weather API response: {ex}"}

  if not forecast_hours:
    logger.warning(
        "Hourly forecast returned no records",
        extra={"latitude": latitude, "longitude": longitude, "hours": hours},
    )
    return {
        "success": False,
        "error": "Weather API returned no forecast hours for this location.",
    }

  rain_total_mm = 0.0
  snow_total_mm = 0.0
  max_rain_rate = 0.0
  temperatures: List[float] = []
  condition_types = set()
  rain_present = False
  snow_present = False

  for hour in forecast_hours:
    precipitation = hour.get("precipitation") or {}
    rain_mm = _to_millimetres(precipitation.get("qpf"))
    snow_mm = _to_millimetres(precipitation.get("snowQpf"))

    rain_total_mm += rain_mm
    snow_total_mm += snow_mm
    max_rain_rate = max(max_rain_rate, rain_mm)

    celsius = _to_celsius(hour.get("temperature"))
    if celsius is not None:
      temperatures.append(celsius)

    condition = (hour.get("weatherCondition") or {}).get("type")
    if condition:
      condition_types.add(condition)

    if _hour_is_rain(hour, rain_mm):
      rain_present = True
    if _hour_is_snow(hour, snow_mm):
      snow_present = True

  result = {
      "success": True,
      "latitude": latitude,
      "longitude": longitude,
      "hours_requested": hours,
      "hours_received": len(forecast_hours),
      "rain_accumulation_mm": round(rain_total_mm, 3),
      "snow_accumulation_mm": round(snow_total_mm, 3),
      "max_rain_rate_mm_per_h": round(max_rain_rate, 3),
      "min_temperature_c": (
          round(min(temperatures), 2) if temperatures else None
      ),
      "max_temperature_c": (
          round(max(temperatures), 2) if temperatures else None
      ),
      "rain_present": rain_present,
      "snow_present": snow_present,
      "condition_types": sorted(condition_types),
      "snow_units_note": (
          "snow_accumulation_mm is liquid water equivalent, not snow depth."
      ),
  }

  if len(forecast_hours) < hours:
    # Surfaced rather than silently tolerated: a truncated window makes any
    # accumulation threshold easier to miss.
    result["partial_window"] = True
    logger.warning(
        "Hourly forecast covered %d of %d requested hours",
        len(forecast_hours),
        hours,
        extra={"latitude": latitude, "longitude": longitude},
    )

  logger.info(
      "Aggregated %d forecast hours",
      len(forecast_hours),
      extra={
          "latitude": latitude,
          "longitude": longitude,
          "rain_accumulation_mm": result["rain_accumulation_mm"],
          "snow_accumulation_mm": result["snow_accumulation_mm"],
          "min_temperature_c": result["min_temperature_c"],
      },
  )
  return result


class WeatherSignalsToolset(BaseToolset):
  """Toolset exposing deterministic weather signal aggregation."""

  def __init__(self):
    super().__init__()
    self._signals_tool = FunctionTool(func=get_24h_weather_signals)

  async def get_tools(
      self, readonly_context: Optional[Any] = None
  ) -> List[FunctionTool]:
    """Returns a list of tools in this toolset.

    Args:
        readonly_context: Context object allowed to be used by the tools.

    Returns:
        A list containing the weather signal aggregation tool.
    """
    return [self._signals_tool]
