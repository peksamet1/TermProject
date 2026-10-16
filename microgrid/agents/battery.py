"""Energy-bucket battery model with SoC bounds, power limits and losses.

Deliberately free of Mesa/SimPy dependencies: in Phase 5 the same class will be
driven by SimPy processes instead of Mesa steps.
"""

from __future__ import annotations

import math

from microgrid.config import BatteryConfig


class Battery:
    """Tracks stored energy in kWh; charge/discharge respect capacity,
    SoC window, per-step power limits and round-trip efficiency.

    Round-trip losses are split evenly between the charge and the discharge
    direction (one-way efficiency = sqrt(round_trip_efficiency)).
    """

    def __init__(
        self,
        *,
        capacity_kwh: float,
        max_charge_kw: float,
        max_discharge_kw: float,
        round_trip_efficiency: float,
        soc_min: float,
        soc_max: float,
        initial_soc: float,
        step_hours: float,
    ) -> None:
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if not 0 < round_trip_efficiency <= 1:
            raise ValueError("round_trip_efficiency must be in (0, 1]")
        if not 0 <= soc_min < soc_max <= 1:
            raise ValueError("SoC bounds must satisfy 0 <= soc_min < soc_max <= 1")
        if not soc_min <= initial_soc <= soc_max:
            raise ValueError("initial_soc must lie within [soc_min, soc_max]")

        self.capacity_kwh = capacity_kwh
        self.soc_min = soc_min
        self.soc_max = soc_max
        self._max_charge_kwh_per_step = max_charge_kw * step_hours
        self._max_discharge_kwh_per_step = max_discharge_kw * step_hours
        self._one_way_efficiency = math.sqrt(round_trip_efficiency)
        self._energy_kwh = initial_soc * capacity_kwh

    @classmethod
    def from_config(cls, config: BatteryConfig, step_hours: float) -> "Battery":
        return cls(
            capacity_kwh=config.capacity_kwh,
            max_charge_kw=config.max_charge_kw,
            max_discharge_kw=config.max_discharge_kw,
            round_trip_efficiency=config.round_trip_efficiency,
            soc_min=config.soc_min,
            soc_max=config.soc_max,
            initial_soc=config.initial_soc,
            step_hours=step_hours,
        )

    @property
    def soc(self) -> float:
        return self._energy_kwh / self.capacity_kwh

    @property
    def energy_kwh(self) -> float:
        return self._energy_kwh

    def charge(self, offered_kwh: float) -> float:
        """Charge with up to ``offered_kwh``.

        Returns the energy actually drawn from the surplus (input side, i.e.
        what the household no longer has available to sell). The stored amount
        is smaller due to charging losses.
        """
        if offered_kwh < 0:
            raise ValueError("offered_kwh must be non-negative")
        headroom_kwh = max(0.0, self.soc_max * self.capacity_kwh - self._energy_kwh)
        intake_kwh = min(
            offered_kwh,
            self._max_charge_kwh_per_step,
            headroom_kwh / self._one_way_efficiency,
        )
        self._energy_kwh += intake_kwh * self._one_way_efficiency
        return intake_kwh

    def discharge(self, requested_kwh: float) -> float:
        """Discharge up to ``requested_kwh``.

        Returns the energy actually delivered to the household (output side).
        The battery loses slightly more internally due to discharge losses.
        """
        if requested_kwh < 0:
            raise ValueError("requested_kwh must be non-negative")
        available_kwh = max(0.0, self._energy_kwh - self.soc_min * self.capacity_kwh)
        delivered_kwh = min(
            requested_kwh,
            self._max_discharge_kwh_per_step,
            available_kwh * self._one_way_efficiency,
        )
        self._energy_kwh -= delivered_kwh / self._one_way_efficiency
        return delivered_kwh
