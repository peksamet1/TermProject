"""Synthetic rooftop PV generation profile.

Clear-sky output follows a sine arc between sunrise and sunset scaled to the
installed peak power; multiplicative cloud noise makes each step stochastic.
Default sunrise/sunset hours approximate an Istanbul summer day; both are
parameters so other seasons can be simulated.
"""

from __future__ import annotations

import math

import numpy as np


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
    ) -> None:
        if peak_kw < 0:
            raise ValueError("peak_kw must be non-negative")
        if not 0 <= sunrise_hour < sunset_hour <= 24:
            raise ValueError("daylight window must satisfy 0 <= sunrise < sunset <= 24")

        self.peak_kw = peak_kw
        self.step_hours = step_hours
        self.steps_per_day = steps_per_day
        self.sunrise_hour = sunrise_hour
        self.sunset_hour = sunset_hour
        self.cloud_sigma = cloud_sigma
        self._rng = rng

    def energy_at(self, step: int) -> float:
        """PV energy (kWh) generated during the given simulation step."""
        hour = (step % self.steps_per_day) * self.step_hours
        if not self.sunrise_hour < hour < self.sunset_hour:
            return 0.0
        daylight_position = (hour - self.sunrise_hour) / (
            self.sunset_hour - self.sunrise_hour
        )
        clear_sky_kw = self.peak_kw * math.sin(math.pi * daylight_position)
        if self.cloud_sigma > 0:
            # Mean slightly below 1 so the profile is usually a bit under clear-sky.
            cloud_factor = float(
                np.clip(self._rng.normal(0.9, self.cloud_sigma), 0.2, 1.0)
            )
        else:
            cloud_factor = 1.0
        return clear_sky_kw * cloud_factor * self.step_hours
