"""Synthetic rooftop PV generation profile.

Clear-sky output follows a sine arc between sunrise and sunset scaled to the
installed peak power; multiplicative cloud noise makes each step stochastic.

Two modes:
- Fixed window (default): ``sunrise_hour`` / ``sunset_hour`` stay the same
  every day (an Istanbul summer day by default).
- Seasonal: pass ``start_day_of_year`` (1-365). Day length then follows the
  sun at Istanbul's latitude and the daily yield follows the monthly table
  below, so winter days are short and weak, summer days long and strong.
"""

from __future__ import annotations

import math

import numpy as np

ISTANBUL_LATITUDE_DEG = 41.0
ISTANBUL_SOLAR_NOON_HOUR = 13.0  # local time (UTC+3), longitude ~29 E

# Approximate daily PV yield in Istanbul, kWh per installed kWp, per month
# (Jan..Dec). Derived from NASA POWER irradiance with ~14% system losses;
# seasonal averages: winter ~1.9, spring ~4.6, summer ~6.3, autumn ~3.3.
# Real PVGIS data replaces this table when profile_source="csv".
ISTANBUL_KWH_PER_KWP_DAY = (
    1.8, 2.4, 3.4, 4.6, 5.7, 6.4, 6.6, 5.9, 4.6, 3.2, 2.1, 1.4,
)

# Average of the cloud factor below (normal(0.9, sigma) clipped to [0.2, 1]).
_MEAN_CLOUD_FACTOR = 0.9
_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def day_length_hours(day_of_year: int, latitude_deg: float = ISTANBUL_LATITUDE_DEG) -> float:
    """Astronomical day length (sunrise to sunset) in hours."""
    declination = math.radians(23.44) * math.sin(2 * math.pi * (284 + day_of_year) / 365)
    cos_hour_angle = -math.tan(math.radians(latitude_deg)) * math.tan(declination)
    cos_hour_angle = min(1.0, max(-1.0, cos_hour_angle))
    return 2 * math.degrees(math.acos(cos_hour_angle)) / 15.0


def month_of_day(day_of_year: int) -> int:
    """Month index 0-11 for a day of year 1-365 (non-leap)."""
    day = (day_of_year - 1) % 365
    for month, length in enumerate(_DAYS_IN_MONTH):
        if day < length:
            return month
        day -= length
    return 11


class SolarProfile:
    def __init__(
        self,
        *,
        peak_kw: float,
        step_hours: float,
        steps_per_day: int,
        rng: np.random.Generator,
        sunrise_hour: float = 6.0,
        sunset_hour: float = 20.0,
        cloud_sigma: float = 0.15,
        start_day_of_year: int | None = None,
    ) -> None:
        if peak_kw < 0:
            raise ValueError("peak_kw must be non-negative")
        if not 0 <= sunrise_hour < sunset_hour <= 24:
            raise ValueError("daylight window must satisfy 0 <= sunrise < sunset <= 24")
        if start_day_of_year is not None and not 1 <= start_day_of_year <= 365:
            raise ValueError("start_day_of_year must be in 1..365")

        self.peak_kw = peak_kw
        self.step_hours = step_hours
        self.steps_per_day = steps_per_day
        self.sunrise_hour = sunrise_hour
        self.sunset_hour = sunset_hour
        self.cloud_sigma = cloud_sigma
        self.start_day_of_year = start_day_of_year
        self._rng = rng

    def _daylight(self, step: int) -> tuple[float, float, float]:
        """(sunrise hour, sunset hour, clear-sky peak kW) for the step's day."""
        if self.start_day_of_year is None:
            return self.sunrise_hour, self.sunset_hour, self.peak_kw

        day_of_year = (self.start_day_of_year - 1 + step // self.steps_per_day) % 365 + 1
        length = day_length_hours(day_of_year)
        sunrise = ISTANBUL_SOLAR_NOON_HOUR - length / 2
        sunset = ISTANBUL_SOLAR_NOON_HOUR + length / 2
        # Pick the peak so the expected daily energy matches the monthly table.
        # Integral of a sine arc over the day = peak * length * 2 / pi.
        target_kwh = ISTANBUL_KWH_PER_KWP_DAY[month_of_day(day_of_year)] * self.peak_kw
        peak = target_kwh / (length * 2 / math.pi) / _MEAN_CLOUD_FACTOR
        return sunrise, sunset, peak

    def energy_at(self, step: int) -> float:
        """PV energy (kWh) generated during the given simulation step."""
        sunrise, sunset, peak_kw = self._daylight(step)
        hour = (step % self.steps_per_day) * self.step_hours
        if not sunrise < hour < sunset:
            return 0.0
        daylight_position = (hour - sunrise) / (sunset - sunrise)
        clear_sky_kw = peak_kw * math.sin(math.pi * daylight_position)
        if self.cloud_sigma > 0:
            # Mean slightly below 1 so the profile is usually a bit under clear-sky.
            cloud_factor = float(
                np.clip(self._rng.normal(_MEAN_CLOUD_FACTOR, self.cloud_sigma), 0.2, 1.0)
            )
        else:
            cloud_factor = 1.0
        return clear_sky_kw * cloud_factor * self.step_hours
