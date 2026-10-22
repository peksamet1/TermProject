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
    # "cda" = Continuous Double Auction (default); "logging" = no-P2P baseline
    market_backend: str = "cda"
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    tariff: TariffConfig = field(default_factory=TariffConfig)
    seed: int = 42
    # CommConfig is defined at the end of this module, hence the lambda.
    comm: CommConfig = field(default_factory=lambda: CommConfig())

    @property
    def step_hours(self) -> float:
        return 24.0 / self.steps_per_day

    @property
    def total_steps(self) -> int:
        return self.steps_per_day * self.num_days


# Appended at the end of the file on purpose: Phase 2 edits this module in
# parallel, and end-of-file additions keep merge conflicts to a minimum.
@dataclass
class CommConfig:
    """ZeroMQ communication layer settings (Phase 3, Architecture 4.3)."""

    # "tcp" (default) or "inproc"; inproc is the fallback when the OS file
    # descriptor limit is too small for a full tcp mesh. Only the address changes.
    transport: str = "tcp"
    bind_host: str = "127.0.0.1"  # local sockets only (Architecture 3)
    handshake_interval_ms: int = 10
    handshake_timeout_s: float = 5.0
    step_timeout_s: float = 2.0
    request_timeout_s: float = 1.0
    poll_interval_ms: int = 5
    # PUB silently drops messages once SNDHWM is reached (zmq default: 1000).
    # Large but finite: an unlimited HWM (0) lets queues grow without bound.
    sndhwm: int = 100_000
    rcvhwm: int = 100_000
    io_threads: int = 1
    # Raise RLIMIT_NOFILE before building the mesh; skipped with a warning on
    # platforms without the ``resource`` module (Windows).
    raise_fd_limit: bool = True
