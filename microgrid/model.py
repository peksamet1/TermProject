"""MicrogridModel: wires agents, profiles, battery and market together.

Notes:
- Constructor is keyword-only because SolaraViz re-instantiates the model from
  ``model_params``; positional arguments are not supported there.
- Mesa 3.x has no scheduler classes: agents are activated in random order via
  ``self.agents.shuffle_do("step")``. Random order matters so no household gets
  a systematic first-mover advantage once the CDA market is in place.
- With the Phase 2 ``LoggingMarket`` nothing is matched, so every BUY order
  falls back to the macro-grid (import) and every SELL order is exported at
  the feed-in tariff. These totals form the no-P2P baseline for Phase 6.
"""

from __future__ import annotations

import math

from mesa import DataCollector, Model

from microgrid.agents.battery import Battery
from microgrid.agents.household import ConsumerAgent, ProsumerAgent
from microgrid.agents.strategy import TradingStrategy
from microgrid.config import SimulationConfig
from microgrid.market.base import LoggingMarket, OrderSide
from microgrid.profiles.consumption import ConsumptionProfile
from microgrid.profiles.solar import SolarProfile


class MicrogridModel(Model):
    def __init__(
        self,
        *,
        num_prosumers: int | None = None,
        num_consumers: int | None = None,
        pv_peak_kw: float | None = None,
        battery_capacity_kwh: float | None = None,
        seed: int | None = None,
        config: SimulationConfig | None = None,
    ) -> None:
        # Scalar overrides (used by the SolaraViz sliders) win over the config.
        self.config = config or SimulationConfig()
        num_prosumers = num_prosumers if num_prosumers is not None else self.config.num_prosumers
        num_consumers = num_consumers if num_consumers is not None else self.config.num_consumers
        pv_peak_kw = pv_peak_kw if pv_peak_kw is not None else self.config.pv_peak_kw
        if battery_capacity_kwh is not None:
            self.config.battery.capacity_kwh = battery_capacity_kwh
        seed = seed if seed is not None else self.config.seed

        super().__init__(rng=seed)

        self.current_step = 0
        self.market = LoggingMarket()
        strategy = TradingStrategy(tariff=self.config.tariff)
        step_hours = self.config.step_hours
        steps_per_day = self.config.steps_per_day

        for _ in range(num_prosumers):
            ProsumerAgent(
                self,
                solar_profile=SolarProfile(
                    peak_kw=pv_peak_kw * float(self.rng.uniform(0.8, 1.2)),
                    step_hours=step_hours,
                    steps_per_day=steps_per_day,
                    rng=self.rng,
                ),
                consumption_profile=self._make_consumption_profile(),
                strategy=strategy,
                battery=Battery.from_config(self.config.battery, step_hours),
            )
        for _ in range(num_consumers):
            ConsumerAgent(
                self,
                consumption_profile=self._make_consumption_profile(),
                strategy=strategy,
            )

        # Per-step aggregates, refreshed in step() before collection.
        self.total_generation_kwh = 0.0
        self.total_consumption_kwh = 0.0
        self.grid_import_kwh = 0.0
        self.grid_export_kwh = 0.0
        self.buy_orders = 0
        self.sell_orders = 0

        self.datacollector = DataCollector(
            model_reporters={
                "TotalGeneration": "total_generation_kwh",
                "TotalConsumption": "total_consumption_kwh",
                "GridImport": "grid_import_kwh",
                "GridExport": "grid_export_kwh",
                "BuyOrders": "buy_orders",
                "SellOrders": "sell_orders",
                "AvgProsumerSoC": self._avg_prosumer_soc,
            },
            agent_reporters={
                "Generation": "generation_kwh",
                "Consumption": "consumption_kwh",
                "BatteryFlow": "battery_flow_kwh",
                "Residual": "residual_kwh",
                "BuyOrder": lambda a: max(0.0, -a.residual_kwh),
                "SellOrder": lambda a: max(0.0, a.residual_kwh),
                "SoC": "soc",
                "State": lambda a: a.state.value,
                "Label": "label",
            },
        )

    def _make_consumption_profile(self) -> ConsumptionProfile:
        return ConsumptionProfile(
            step_hours=self.config.step_hours,
            steps_per_day=self.config.steps_per_day,
            rng=self.rng,
            scale=float(self.rng.uniform(0.7, 1.3)),
        )

    @staticmethod
    def _avg_prosumer_soc(model: "MicrogridModel") -> float:
        socs = [
            agent.soc
            for agent in model.agents_by_type.get(ProsumerAgent, [])
            if not math.isnan(agent.soc)
        ]
        return sum(socs) / len(socs) if socs else math.nan

    def step(self) -> None:
        self.agents.shuffle_do("step")

        self.total_generation_kwh = sum(a.generation_kwh for a in self.agents)
        self.total_consumption_kwh = sum(a.consumption_kwh for a in self.agents)
        # LoggingMarket matches nothing, so deficits import from and surpluses
        # export to the macro-grid.
        self.grid_import_kwh = sum(
            -a.residual_kwh for a in self.agents if a.residual_kwh < 0
        )
        self.grid_export_kwh = sum(
            a.residual_kwh for a in self.agents if a.residual_kwh > 0
        )
        step_orders = self.market.orders_at(self.current_step)
        self.buy_orders = sum(1 for o in step_orders if o.side is OrderSide.BUY)
        self.sell_orders = sum(1 for o in step_orders if o.side is OrderSide.SELL)

        self.datacollector.collect(self)
        self.current_step += 1

    def run(self, num_steps: int | None = None) -> None:
        num_steps = num_steps if num_steps is not None else self.config.total_steps
        for _ in range(num_steps):
            self.step()
