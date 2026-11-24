"""Household agents: abstract base plus Prosumer and Consumer subclasses.

Design principle ("role != type"): whether a household buys or sells is decided
by its net energy at the current step, not by its class. The full decision flow
therefore lives in the base class ``step()`` (template method); subclasses only
declare structural differences (PV installed or not, battery installed or not).

Step flow (battery-before-market priority):
1. Read generation and consumption from the profiles.
2. net = generation - consumption
3. If a battery exists, *plan* to charge with surplus / discharge to cover a
   deficit. The plan does not touch the battery: SimPy applies it after the
   market has settled (``microgrid.physics``, Architecture 4.5).
4. Residual surplus -> SELL ask, residual deficit -> BUY bid, ~0 -> IDLE.
5. Report the step to the market (``publish_status``), IDLE included: the
   ZeroMQ step barrier expects a STATUS from every agent every step.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from enum import Enum

from mesa import Agent

from microgrid.agents.battery import Battery
from microgrid.agents.strategy import TradingStrategy
from microgrid.market.base import OrderSide
from microgrid.profiles.base import EnergyProfile


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
        consumption_profile: EnergyProfile,
        strategy: TradingStrategy,
        battery: Battery | None = None,
    ) -> None:
        super().__init__(model)
        self.consumption_profile = consumption_profile
        self.strategy = strategy
        self.battery = battery
        self.generation_scale = 1.0
        self.load_scale = 1.0

        # Per-step telemetry, read by the model's DataCollector.
        self.generation_kwh = 0.0
        self.consumption_kwh = 0.0
        self.net_kwh = 0.0  # before battery correction
        self.battery_flow_kwh = 0.0  # planned: >0 charging, <0 discharging (household side)
        self.battery_applied_kwh = 0.0  # what SimPy applied (set by the physical slot)
        self.battery_shortfall_kwh = 0.0  # planned but not applied (>= 0)
        self.residual_kwh = 0.0  # after battery, what goes to the market
        self.state = HouseholdState.IDLE
        self.cash_tl = 0.0  # cumulative P2P + grid settlement balance

    @staticmethod
    def _checked_scale(value: float, name: str) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a non-negative finite number")
        return float(value)

    @property
    def generation_scale(self) -> float:
        """Multiplier on this step's generation. 1 leaves the profile as it is."""
        return self._generation_scale

    @generation_scale.setter
    def generation_scale(self, value: float) -> None:
        self._generation_scale = self._checked_scale(value, "generation_scale")

    @property
    def load_scale(self) -> float:
        """Multiplier on this step's consumption. 1 leaves the profile as it is."""
        return self._load_scale

    @load_scale.setter
    def load_scale(self, value: float) -> None:
        self._load_scale = self._checked_scale(value, "load_scale")

    @abstractmethod
    def generation(self, step: int) -> float:
        """Energy (kWh) generated during the given step."""

    @property
    def label(self) -> str:
        """Stable display name for dashboards (e.g. Prosumer-3)."""
        return f"{type(self).__name__.replace('Agent', '')}-{self.unique_id}"

    @property
    def soc(self) -> float:
        """Battery state of charge; NaN for households without a battery.

        During the agent step this is the start-of-step SoC (the plan is not
        applied yet); after the physical slot it is the end-of-step SoC.
        """
        return self.battery.soc if self.battery is not None else math.nan

    def step(self) -> None:
        t = self.model.current_step
        # Each profile is read once. Scales apply to that reading only.
        self.generation_kwh = self.generation(t)
        self.consumption_kwh = self.consumption_profile.energy_at(t) * self.load_scale
        net = self.generation_kwh - self.consumption_kwh
        self.net_kwh = net

        # Plan only: the battery state changes in the physical slot.
        # The target is a share of the net; power and the SoC window clip it.
        self.battery_flow_kwh = 0.0
        if self.battery is not None:
            level = self.strategy.fullness(
                self.soc, self.battery.soc_min, self.battery.soc_max
            )
            target = self.strategy.battery_target(net, level)
            if target > 0:
                intake = self.battery.plan_charge(target)
                self.battery_flow_kwh = intake
                net -= intake
            elif target < 0:
                delivered = self.battery.plan_discharge(-target)
                self.battery_flow_kwh = -delivered
                net += delivered
        self.residual_kwh = net

        # SoC is the start-of-step value: the plan has not been applied yet.
        order = self.strategy.make_order(
            agent_id=self.unique_id,
            residual_kwh=net,
            step=t,
            soc=None if self.battery is None else self.soc,
            soc_min=None if self.battery is None else self.battery.soc_min,
            soc_max=None if self.battery is None else self.battery.soc_max,
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
        # Last, and unconditionally: everything it reports is cached above.
        self.model.market.publish_status(self)


class ProsumerAgent(HouseholdAgent):
    """Household with rooftop PV and (typically) a battery."""

    def __init__(
        self,
        model,
        *,
        solar_profile: EnergyProfile,
        consumption_profile: EnergyProfile,
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
        return self.solar_profile.energy_at(step) * self.generation_scale


class ConsumerAgent(HouseholdAgent):
    """Pure consumer: no PV, no battery. Intentionally thin."""

    def generation(self, step: int) -> float:
        return 0.0
