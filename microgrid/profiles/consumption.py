"""Synthetic household consumption profile.

Double-peaked daily load curve typical for residential users: a constant
baseline plus Gaussian-shaped morning and evening peaks, with per-step noise.
A per-household ``scale`` factor differentiates small and large households.
"""

from __future__ import annotations

import math

import numpy as np


def _gaussian(hour: float, mean_hour: float, sigma_hours: float) -> float:
    return math.exp(-((hour - mean_hour) ** 2) / (2 * sigma_hours**2))


class ConsumptionProfile:
    def __init__(
        self,
        *,
        step_hours: float,
        steps_per_day: int,
        rng: np.random.Generator,
        base_kw: float = 0.3,
        morning_peak_kw: float = 0.8,
        morning_peak_hour: float = 7.5,
        evening_peak_kw: float = 1.5,
        evening_peak_hour: float = 20.0,
        scale: float = 1.0,
        noise_sigma_kw: float = 0.05,
    ) -> None:
        if scale <= 0:
            raise ValueError("scale must be positive")

        self.step_hours = step_hours
        self.steps_per_day = steps_per_day
        self.base_kw = base_kw
        self.morning_peak_kw = morning_peak_kw
        self.morning_peak_hour = morning_peak_hour
        self.evening_peak_kw = evening_peak_kw
        self.evening_peak_hour = evening_peak_hour
        self.scale = scale
        self.noise_sigma_kw = noise_sigma_kw
        self._rng = rng

    def energy_at(self, step: int) -> float:
        """Household energy demand (kWh) during the given simulation step."""
        hour = (step % self.steps_per_day) * self.step_hours
        load_kw = (
            self.base_kw
            + self.morning_peak_kw * _gaussian(hour, self.morning_peak_hour, 1.5)
            + self.evening_peak_kw * _gaussian(hour, self.evening_peak_hour, 2.0)
        ) * self.scale
        if self.noise_sigma_kw > 0:
            load_kw = max(0.0, load_kw + float(self._rng.normal(0, self.noise_sigma_kw)))
        return load_kw * self.step_hours
