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
"""Asset group activation thresholds: names, plausible ranges and sources.

Kept free of heavy imports so that both the decision agent's playbook renderer
and the config sheet sync (including its command-line tool) can share one
definition of what a valid threshold is.
"""

from typing import Any, Dict, Optional, Tuple

# The asset group activation thresholds a city may set, and the plausible range
# for each. A value outside the range is treated as unset, so the playbook
# default applies instead of a typo such as 90 instead of 9.0.
ACTIVATION_LIMITS: Dict[str, Tuple[float, float]] = {
    "coldBelowC": (-30.0, 30.0),
    "rainRateMmPerH": (0.01, 20.0),
    "sunnyMinHours": (0.0, 24.0),
}

ACTIVATION_SOURCE_CITY = "city"
ACTIVATION_SOURCE_PARTIAL = "city, with playbook default for unset values"
ACTIVATION_SOURCE_DEFAULT = "playbook default"


def normalise_activation_value(key: str, value: Any) -> Optional[float]:
    """Validates one activation threshold and normalises its type.

    Args:
        key: One of the ACTIVATION_LIMITS keys.
        value: The raw value from Firestore or the config sheet.

    Returns:
        The value as a float (an int for a whole-number sunnyMinHours, so it
        renders as "3" rather than "3.0"), or None if it is missing, not a
        number, or outside the plausible range.
    """
    if key not in ACTIVATION_LIMITS or value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    low, high = ACTIVATION_LIMITS[key]
    if not low <= number <= high:
        return None
    if key == "sunnyMinHours" and number.is_integer():
        return int(number)
    return number
