"""Central configuration for the microgrid simulation.

All tunable parameters live here so scenario changes never require touching
agent or model code. Unit conventions:
- Energy: kWh per simulation step
- Power:  kW (converted to kWh/step via ``step_hours``)
- SoC:    fraction in [0, 1]
- Prices: TL/kWh
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BatteryConfig:
    capacity_kwh: float = 10.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    round_trip_efficiency: float = 0.90
    soc_min: float = 0.10
    soc_max: float = 0.95
    initial_soc: float = 0.50


@dataclass
class TariffConfig:
    """Turkish two-tier tariff: the feed-in rate paid for surplus sold to the
    macro-grid is far below the retail rate paid when buying from it. Any local
    P2P trade priced inside this band benefits both sides."""

    feed_in_tl_per_kwh: float = 1.20
    retail_tl_per_kwh: float = 3.50


@dataclass
class SimulationConfig:
    steps_per_day: int = 96  # 15-minute resolution (smart meter standard)
    num_days: int = 3
    num_prosumers: int = 6
    num_consumers: int = 4
    pv_peak_kw: float = 5.0
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    tariff: TariffConfig = field(default_factory=TariffConfig)
    seed: int = 42

    @property
    def step_hours(self) -> float:
        return 24.0 / self.steps_per_day

    @property
    def total_steps(self) -> int:
        return self.steps_per_day * self.num_days
