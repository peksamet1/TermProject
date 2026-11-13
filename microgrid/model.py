"""MicrogridModel: wires agents, profiles, battery and market together.

Notes:
- Constructor is keyword-only (SolaraViz re-instantiates from model_params).
- ``shuffle_do("step")`` supplies CDA time priority.
- Default market is ``CDAMarket``. Use ``market_backend="logging"`` for the
  no-P2P baseline. Wire JSON for orders/trades is in ``microgrid.comm.protocol``.
"""

from __future__ import annotations

import math

from mesa import DataCollector, Model

from microgrid.agents.battery import Battery
from microgrid.agents.household import ConsumerAgent, HouseholdAgent, ProsumerAgent
from microgrid.agents.strategy import TradingStrategy
from microgrid.config import SimulationConfig
from microgrid.market.base import LoggingMarket, MarketInterface, OrderSide
from microgrid.market.cda import CDAMarket
from microgrid.physics import PhysicalSimulator, SyncError
from microgrid.profiles.base import EnergyProfile
from microgrid.profiles.consumption import ConsumptionProfile
from microgrid.profiles.solar import SolarProfile
from microgrid.profiles.timeseries import ProfileTable


MARKET_BACKENDS = ("cda", "cda_zmq", "logging")


def _build_market(backend: str, agent_ids: list[int], config: SimulationConfig) -> MarketInterface:
    if backend == "cda":
        return CDAMarket()
    if backend == "logging":
        return LoggingMarket()
    if backend == "cda_zmq":
        # Imported here so the in-process backends never load pyzmq.
        from microgrid.market.zmq_market import ZmqMarket

        return ZmqMarket(agent_ids, config.comm)
    raise ValueError(f"unknown market_backend {backend!r}; use one of {MARKET_BACKENDS}")


class MicrogridModel(Model):
    def __init__(
        self,
        *,
        num_prosumers: int | None = None,
        num_consumers: int | None = None,
        pv_peak_kw: float | None = None,
        battery_capacity_kwh: float | None = None,
        market_backend: str | None = None,
        seed: int | None = None,
        config: SimulationConfig | None = None,
    ) -> None:
        self.config = config or SimulationConfig()
        num_prosumers = num_prosumers if num_prosumers is not None else self.config.num_prosumers
        num_consumers = num_consumers if num_consumers is not None else self.config.num_consumers
        pv_peak_kw = pv_peak_kw if pv_peak_kw is not None else self.config.pv_peak_kw
        if battery_capacity_kwh is not None:
            self.config.battery.capacity_kwh = battery_capacity_kwh
        backend = market_backend if market_backend is not None else self.config.market_backend
        seed = seed if seed is not None else self.config.seed

        super().__init__(rng=seed)

        self.current_step = 0
        if backend not in MARKET_BACKENDS:
            raise ValueError(f"unknown market_backend {backend!r}; use one of {MARKET_BACKENDS}")
        strategy = TradingStrategy(tariff=self.config.tariff)
        step_hours = self.config.step_hours

        self._pv_peak_kw = pv_peak_kw
        self._profile_table = self._load_profile_table()

        # Household index i (prosumers first, then consumers) selects row
        # group i mod k of the profile CSV in csv mode. In csv mode the file
        # already holds each home's PV power, so pv_peak_kw is not used.
        for i in range(num_prosumers):
            ProsumerAgent(
                self,
                solar_profile=self._make_solar_profile(i),
                consumption_profile=self._make_consumption_profile(i),
                strategy=strategy,
                battery=Battery.from_config(self.config.battery, step_hours),
            )
        for j in range(num_consumers):
            ConsumerAgent(
                self,
                consumption_profile=self._make_consumption_profile(num_prosumers + j),
                strategy=strategy,
            )

        # Built after the agents: cda_zmq needs one network node per agent id.
        self.market = _build_market(
            backend, [agent.unique_id for agent in self.agents], self.config
        )

        self.physics = PhysicalSimulator(step_hours=step_hours)
        self.battery_shortfall_kwh = 0.0
        self.battery_fade_loss_kwh = 0.0
        self.constraint_violations = 0

        self.total_generation_kwh = 0.0
        self.total_consumption_kwh = 0.0
        # Grid totals = market settlement + battery fallback (stage 3).
        self.grid_import_kwh = 0.0
        self.grid_export_kwh = 0.0
        self.fallback_import_kwh = 0.0
        self.fallback_export_kwh = 0.0
        self.local_traded_kwh = 0.0
        self.local_trade_count = 0
        self.avg_trade_price = math.nan
        self.buy_orders = 0
        self.sell_orders = 0

        self.datacollector = DataCollector(
            model_reporters={
                "TotalGeneration": "total_generation_kwh",
                "TotalConsumption": "total_consumption_kwh",
                "GridImport": "grid_import_kwh",
                "GridExport": "grid_export_kwh",
                "FallbackImport": "fallback_import_kwh",
                "FallbackExport": "fallback_export_kwh",
                "LocalTradedKwh": "local_traded_kwh",
                "LocalTradeCount": "local_trade_count",
                "AvgTradePrice": "avg_trade_price",
                "BuyOrders": "buy_orders",
                "SellOrders": "sell_orders",
                "AvgProsumerSoC": self._avg_prosumer_soc,
                "BatteryShortfall": "battery_shortfall_kwh",
                "BatteryFadeLoss": "battery_fade_loss_kwh",
                "ConstraintViolations": "constraint_violations",
            },
            agent_reporters={
                "Generation": "generation_kwh",
                "Consumption": "consumption_kwh",
                "BatteryFlow": "battery_flow_kwh",
                "BatteryApplied": "battery_applied_kwh",
                "Residual": "residual_kwh",
                "BuyOrder": lambda a: max(0.0, -a.residual_kwh),
                "SellOrder": lambda a: max(0.0, a.residual_kwh),
                "SoC": "soc",
                "CashTL": "cash_tl",
                "State": lambda a: a.state.value,
                "Label": "label",
            },
        )

    def _load_profile_table(self) -> ProfileTable | None:
        source = self.config.profile_source
        if source == "synthetic":
            return None
        if source == "csv":
            if not self.config.profile_csv_path:
                raise ValueError("profile_source='csv' needs profile_csv_path")
            return ProfileTable.from_csv(
                self.config.profile_csv_path,
                step_hours=self.config.step_hours,
                steps_per_day=self.config.steps_per_day,
                start=self.config.profile_start,
            )
        raise ValueError(f"unknown profile_source {source!r}; use 'synthetic' or 'csv'")

    def _make_solar_profile(self, index: int) -> EnergyProfile:
        if self._profile_table is not None:
            return self._profile_table.solar_profile(index)
        return SolarProfile(
            peak_kw=self._pv_peak_kw * float(self.rng.uniform(0.8, 1.2)),
            step_hours=self.config.step_hours,
            steps_per_day=self.config.steps_per_day,
            rng=self.rng,
            start_day_of_year=self.config.start_day_of_year,
        )

    def _make_consumption_profile(self, index: int) -> EnergyProfile:
        if self._profile_table is not None:
            return self._profile_table.consumption_profile(index)
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

    def _agent_by_id(self, agent_id: int) -> HouseholdAgent | None:
        for agent in self.agents:
            if agent.unique_id == agent_id:
                return agent
        return None

    def _apply_settlement_cash(self, settlement_step: int) -> None:
        tariff = self.config.tariff
        matched_buy: dict[int, float] = {}
        matched_sell: dict[int, float] = {}
        # Same path for every backend: only confirmed trades are paid locally.
        for trade in self.market.trades_at(settlement_step):
            cost = trade.quantity_kwh * trade.price_tl_per_kwh
            buyer = self._agent_by_id(trade.buyer_id)
            seller = self._agent_by_id(trade.seller_id)
            if buyer is not None:
                buyer.cash_tl -= cost
            if seller is not None:
                seller.cash_tl += cost
            matched_buy[trade.buyer_id] = (
                matched_buy.get(trade.buyer_id, 0.0) + trade.quantity_kwh
            )
            matched_sell[trade.seller_id] = (
                matched_sell.get(trade.seller_id, 0.0) + trade.quantity_kwh
            )

        for agent in self.agents:
            buy_qty = max(0.0, -agent.residual_kwh)
            sell_qty = max(0.0, agent.residual_kwh)
            unmatched_buy = max(0.0, buy_qty - matched_buy.get(agent.unique_id, 0.0))
            unmatched_sell = max(0.0, sell_qty - matched_sell.get(agent.unique_id, 0.0))
            agent.cash_tl -= unmatched_buy * tariff.retail_tl_per_kwh
            agent.cash_tl += unmatched_sell * tariff.feed_in_tl_per_kwh

    # ------------------------------------------------------------------
    # One simulation step = four stages, always in this order.
    # Each later project phase writes only into its own stage:
    #   Phase 2 -> _agents_step, Phase 4 -> _market_settle,
    #   Phase 5 -> _physics_slot, Phase 5/6 -> _collect.
    # This keeps parallel work by different team members conflict-free.
    # ------------------------------------------------------------------

    def step(self) -> None:
        self._agents_step()
        self._market_settle()
        self._physics_slot()
        self._collect()
        self.current_step += 1

    def _agents_step(self) -> None:
        """Stage 1/4: every agent reads its profile, plans its battery and
        submits an order. Random order gives CDA time priority."""
        self.agents.shuffle_do("step")
        self.total_generation_kwh = sum(a.generation_kwh for a in self.agents)
        self.total_consumption_kwh = sum(a.consumption_kwh for a in self.agents)

    def _market_settle(self) -> None:
        """Stage 2/4: close the market for this step, send unmatched volume
        to the macro grid and update every agent's cash."""
        settlement = self.market.settle_step(self.current_step, self.config.tariff)
        self.grid_import_kwh = settlement.grid_import_kwh
        self.grid_export_kwh = settlement.grid_export_kwh
        self.local_traded_kwh = settlement.local_traded_kwh
        self.local_trade_count = settlement.trade_count
        self.avg_trade_price = (
            settlement.avg_trade_price
            if settlement.avg_trade_price is not None
            else math.nan
        )

        self._apply_settlement_cash(self.current_step)

        step_orders = self.market.orders_at(self.current_step)
        self.buy_orders = sum(1 for o in step_orders if o.side is OrderSide.BUY)
        self.sell_orders = sum(1 for o in step_orders if o.side is OrderSide.SELL)

    def _physics_slot(self) -> None:
        """Stage 3/4: SimPy applies every battery plan of this step minute
        by minute (all backends, the no-P2P baseline included)."""
        with_battery = sorted(
            (a for a in self.agents if a.battery is not None), key=lambda a: a.unique_id
        )
        report = self.physics.run_slot(self.current_step, with_battery)
        # Two independent clocks: Mesa advances ``time`` at the start of
        # step(), so after slot t both must sit at (t + 1) slots.
        if self.physics.env.now != self.time * self.physics.slot_minutes:
            raise SyncError(
                f"SimPy at minute {self.physics.env.now}, Mesa time {self.time}"
            )
        results = {r.agent_id: r for r in report.batteries}
        for agent in with_battery:
            agent.battery_applied_kwh = results[agent.unique_id].applied_kwh
            agent.battery_shortfall_kwh = results[agent.unique_id].shortfall_kwh
        for agent in self.agents:
            if agent.battery is None:
                agent.battery_applied_kwh = agent.battery_shortfall_kwh = 0.0
        self.battery_shortfall_kwh = report.shortfall_kwh
        self.battery_fade_loss_kwh = report.fade_loss_kwh
        self.constraint_violations = report.violations
        self._apply_battery_fallback(with_battery)

    def _apply_battery_fallback(self, with_battery: list[HouseholdAgent]) -> None:
        """Second cash pass (PSD 6.4): a battery plan SimPy could not apply
        falls back to the macro-grid. The agent traded as if it were applied:

        - charge shortfall: surplus the battery did not take -> exported at
          the feed-in tariff;
        - discharge shortfall: deficit the battery did not cover -> imported
          at the retail tariff.
        """
        tariff = self.config.tariff
        self.fallback_import_kwh = self.fallback_export_kwh = 0.0
        for agent in with_battery:
            shortfall = agent.battery_shortfall_kwh
            if shortfall <= 0:
                continue
            if agent.battery_flow_kwh > 0:
                self.fallback_export_kwh += shortfall
                agent.cash_tl += shortfall * tariff.feed_in_tl_per_kwh
            else:
                self.fallback_import_kwh += shortfall
                agent.cash_tl -= shortfall * tariff.retail_tl_per_kwh
        self.grid_import_kwh += self.fallback_import_kwh
        self.grid_export_kwh += self.fallback_export_kwh

    def _collect(self) -> None:
        """Stage 4/4: record this step's data."""
        self.datacollector.collect(self)

    def close(self) -> None:
        """Release market resources (the ZeroMQ sockets of cda_zmq)."""
        self.market.close()

    def __enter__(self) -> "MicrogridModel":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def run(self, num_steps: int | None = None) -> None:
        num_steps = num_steps if num_steps is not None else self.config.total_steps
        for _ in range(num_steps):
            self.step()
