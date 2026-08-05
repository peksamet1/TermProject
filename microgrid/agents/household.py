"""Household agents: abstract base plus Prosumer and Consumer subclasses.

Design principle ("role != type"): whether a household buys or sells is decided
by its net energy at the current step, not by its class. The full decision flow
therefore lives in the base class ``step()`` (template method); subclasses only
declare structural differences (PV installed or not, battery installed or not).

Step flow (battery-before-market priority):
1. Read generation and consumption from the profiles.
2. net = generation - consumption
3. If a battery exists, charge with surplus / discharge to cover a deficit.
4. Residual surplus -> SELL ask, residual deficit -> BUY bid, ~0 -> IDLE.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from enum import Enum

from mesa import Agent

from microgrid.agents.battery import Battery
from microgrid.agents.strategy import TradingStrategy
from microgrid.market.base import OrderSide
from microgrid.profiles.consumption import ConsumptionProfile
from microgrid.profiles.solar import SolarProfile


class HouseholdState(str, Enum):
    IDLE = "IDLE"
    BUYING = "BUYING"
    SELLING = "SELLING"


class HouseholdAgent(Agent, ABC):
    """Abstract base for all households; instantiation is blocked until
    ``generation`` is implemented by a subclass."""

    def __init__(
        self,
        model,
        *,
        consumption_profile: ConsumptionProfile,
        strategy: TradingStrategy,
        battery: Battery | None = None,
    ) -> None:
        super().__init__(model)
        self.consumption_profile = consumption_profile
        self.strategy = strategy
        self.battery = battery

        # Per-step telemetry, read by the model's DataCollector.
        self.generation_kwh = 0.0
        self.consumption_kwh = 0.0
        self.net_kwh = 0.0  # before battery correction
        self.battery_flow_kwh = 0.0  # >0 charging, <0 discharging (household side)
        self.residual_kwh = 0.0  # after battery, what goes to the market
        self.state = HouseholdState.IDLE

    @abstractmethod
    def generation(self, step: int) -> float:
        """Energy (kWh) generated during the given step."""

    @property
    def label(self) -> str:
        """Stable display name for dashboards (e.g. Prosumer-3)."""
        return f"{type(self).__name__.replace('Agent', '')}-{self.unique_id}"

    @property
    def soc(self) -> float:
        """Battery state of charge; NaN for households without a battery."""
        return self.battery.soc if self.battery is not None else math.nan

    def step(self) -> None:
        t = self.model.current_step
        self.generation_kwh = self.generation(t)
        self.consumption_kwh = self.consumption_profile.energy_at(t)
        net = self.generation_kwh - self.consumption_kwh
        self.net_kwh = net

        self.battery_flow_kwh = 0.0
        if self.battery is not None:
            if net > 0:
                stored = self.battery.charge(net)
                self.battery_flow_kwh = stored
                net -= stored
            elif net < 0:
                delivered = self.battery.discharge(-net)
                self.battery_flow_kwh = -delivered
                net += delivered
        self.residual_kwh = net

        order = self.strategy.make_order(
            agent_id=self.unique_id, residual_kwh=net, step=t
        )
        if order is None:
            self.state = HouseholdState.IDLE
        else:
            self.state = (
                HouseholdState.SELLING
                if order.side is OrderSide.SELL
                else HouseholdState.BUYING
            )
            self.model.market.submit(order)


class ProsumerAgent(HouseholdAgent):
    """Household with rooftop PV and (typically) a battery."""

    def __init__(
        self,
        model,
        *,
        solar_profile: SolarProfile,
        consumption_profile: ConsumptionProfile,
        strategy: TradingStrategy,
        battery: Battery | None = None,
    ) -> None:
        super().__init__(
            model,
            consumption_profile=consumption_profile,
            strategy=strategy,
            battery=battery,
        )
        self.solar_profile = solar_profile

    def generation(self, step: int) -> float:
        return self.solar_profile.energy_at(step)


class ConsumerAgent(HouseholdAgent):
    """Pure consumer: no PV, no battery. Intentionally thin."""

    def generation(self, step: int) -> float:
        return 0.0
