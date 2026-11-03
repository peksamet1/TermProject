"""Energy-bucket battery model with SoC bounds, power limits, losses and fade.

Deliberately free of Mesa/SimPy dependencies. Two kinds of calls (Phase 5):

- ``plan_charge`` / ``plan_discharge`` never change the state. The agent uses
  them during its Mesa step to decide how much the battery will take or give
  this step, so only the rest goes to the market.
- ``charge`` / ``discharge`` (with a ``hours`` duration) and ``apply_fade``
  change the state. Only the SimPy layer (``microgrid.physics``) calls them,
  minute by minute, after the market has settled.

Keeping planning and applying apart is what stops the battery from being
applied twice (once by the agent, once by SimPy) or not at all.
"""

from __future__ import annotations

import math

from microgrid.config import BatteryConfig


class Battery:
    """Tracks stored energy in kWh; charge/discharge respect capacity,
    SoC window, power limits and round-trip efficiency.

    Round-trip losses are split evenly between the charge and the discharge
    direction (one-way efficiency = sqrt(round_trip_efficiency)).

    Capacity fades with throughput (``apply_fade``); SoC bounds always apply
    to the current, faded capacity.
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
        fade_per_cycle: float = 0.0,
    ) -> None:
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if max_charge_kw < 0 or max_discharge_kw < 0:
            raise ValueError("power limits must be non-negative")
        if not 0 < round_trip_efficiency <= 1:
            raise ValueError("round_trip_efficiency must be in (0, 1]")
        if not 0 <= soc_min < soc_max <= 1:
            raise ValueError("SoC bounds must satisfy 0 <= soc_min < soc_max <= 1")
        if not soc_min <= initial_soc <= soc_max:
            raise ValueError("initial_soc must lie within [soc_min, soc_max]")
        if not 0 <= fade_per_cycle < 1:
            raise ValueError("fade_per_cycle must be in [0, 1)")

        self.nominal_capacity_kwh = capacity_kwh
        self.capacity_kwh = capacity_kwh
        self.soc_min = soc_min
        self.soc_max = soc_max
        self.max_charge_kw = max_charge_kw
        self.max_discharge_kw = max_discharge_kw
        self.step_hours = step_hours
        self.fade_per_cycle = fade_per_cycle
        self._one_way_efficiency = math.sqrt(round_trip_efficiency)
        self._energy_kwh = initial_soc * capacity_kwh
        # Lifetime counters (kWh).
        self.throughput_kwh = 0.0  # energy in + energy out, battery side
        self.fade_loss_kwh = 0.0  # stored energy clipped away by capacity fade

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
            fade_per_cycle=config.fade_per_cycle,
        )

    @property
    def soc(self) -> float:
        return self._energy_kwh / self.capacity_kwh

    @property
    def energy_kwh(self) -> float:
        return self._energy_kwh

    @property
    def one_way_efficiency(self) -> float:
        return self._one_way_efficiency

    # --- Planning: no side effects -------------------------------------

    def plan_charge(self, offered_kwh: float, hours: float | None = None) -> float:
        """Energy the battery would draw from ``offered_kwh`` (input side)
        within ``hours`` (default: one simulation step). State is unchanged."""
        if offered_kwh < 0:
            raise ValueError("offered_kwh must be non-negative")
        hours = self.step_hours if hours is None else hours
        headroom_kwh = max(0.0, self.soc_max * self.capacity_kwh - self._energy_kwh)
        return min(
            offered_kwh,
            self.max_charge_kw * hours,
            headroom_kwh / self._one_way_efficiency,
        )

    def plan_discharge(self, requested_kwh: float, hours: float | None = None) -> float:
        """Energy the battery would deliver toward ``requested_kwh`` (output
        side) within ``hours`` (default: one simulation step). State is unchanged."""
        if requested_kwh < 0:
            raise ValueError("requested_kwh must be non-negative")
        hours = self.step_hours if hours is None else hours
        available_kwh = max(0.0, self._energy_kwh - self.soc_min * self.capacity_kwh)
        return min(
            requested_kwh,
            self.max_discharge_kw * hours,
            available_kwh * self._one_way_efficiency,
        )

    # --- Applying: changes the state -------------------------------------

    def charge(self, offered_kwh: float, hours: float | None = None) -> float:
        """Charge with up to ``offered_kwh`` during ``hours``.

        Returns the energy actually drawn (input side). The stored amount is
        smaller due to charging losses.
        """
        intake_kwh = self.plan_charge(offered_kwh, hours)
        stored_kwh = intake_kwh * self._one_way_efficiency
        self._energy_kwh += stored_kwh
        self.throughput_kwh += stored_kwh
        return intake_kwh

    def discharge(self, requested_kwh: float, hours: float | None = None) -> float:
        """Discharge up to ``requested_kwh`` during ``hours``.

        Returns the energy actually delivered (output side). The battery loses
        slightly more internally due to discharge losses.
        """
        delivered_kwh = self.plan_discharge(requested_kwh, hours)
        drained_kwh = delivered_kwh / self._one_way_efficiency
        self._energy_kwh -= drained_kwh
        self.throughput_kwh += drained_kwh
        return delivered_kwh

    def apply_fade(self, throughput_kwh: float) -> float:
        """Shrink the capacity for ``throughput_kwh`` of battery-side energy
        moved; returns the stored energy clipped away (a loss).

        Stored energy above soc_max x the new capacity is clipped so the SoC
        window keeps holding.
        """
        if throughput_kwh < 0:
            raise ValueError("throughput_kwh must be non-negative")
        if self.fade_per_cycle == 0 or throughput_kwh == 0:
            return 0.0
        cycles = throughput_kwh / (2 * self.nominal_capacity_kwh)
        self.capacity_kwh -= self.nominal_capacity_kwh * self.fade_per_cycle * cycles
        ceiling_kwh = self.soc_max * self.capacity_kwh
        clipped_kwh = max(0.0, self._energy_kwh - ceiling_kwh)
        self._energy_kwh -= clipped_kwh
        self.fade_loss_kwh += clipped_kwh
        return clipped_kwh
