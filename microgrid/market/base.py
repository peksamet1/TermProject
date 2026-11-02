"""Market abstractions shared across phases.

``LoggingMarket`` is the no-P2P baseline. ``CDAMarket`` implements Continuous
Double Auction clearing. Both share ``submit`` / ``settle_step`` so agents
stay unchanged. Wire JSON for these payloads lives in ``microgrid.comm.protocol``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from microgrid.config import TariffConfig

if TYPE_CHECKING:  # household.py imports this module
    from microgrid.agents.household import HouseholdAgent


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Order:
    agent_id: int
    side: OrderSide
    quantity_kwh: float
    price_tl_per_kwh: float
    step: int


@dataclass(frozen=True)
class SettlementResult:
    """Per-step market settlement summary used by MicrogridModel metrics."""

    grid_import_kwh: float
    grid_export_kwh: float
    local_traded_kwh: float
    trade_count: int
    avg_trade_price: float | None


class MarketInterface(ABC):
    @abstractmethod
    def submit(self, order: Order) -> None:
        """Submit a bid (BUY) or ask (SELL) to the market."""

    @abstractmethod
    def settle_step(self, step: int, tariff: TariffConfig) -> SettlementResult:
        """Finalize the step: unmatched volume goes to the macro-grid."""

    @abstractmethod
    def orders_at(self, step: int) -> list[Order]:
        """Return all orders submitted during ``step``."""

    def publish_status(self, agent: HouseholdAgent) -> None:
        """Called by every agent at the end of its step, IDLE included.

        A no-op for the in-process backends (``cda``, ``logging``). Phase 4's
        ``ZmqMarket`` overrides it to broadcast the agent's STATUS: the step
        barrier waits for N - 1 STATUSes every step (Architecture 4.3), and
        this call is what guarantees one per agent.

        An override reads only what the agent cached during its step:
        ``generation_kwh``, ``consumption_kwh``, ``residual_kwh``,
        ``state.value`` and ``soc``. It must never call ``energy_at()`` or
        ``generation()``: synthetic profiles draw from the shared
        ``model.rng``, so a second read returns a different value *and*
        shifts every later agent's values (breaks same-seed reproducibility).

        ``soc`` is read after the battery plan of this step has been applied.
        It is not the start-of-step SoC the strategy decides on
        (Architecture 4.2); analysis code must not read it as such.
        """
        del agent


class LoggingMarket(MarketInterface):
    """No-P2P baseline: records orders, matches nothing.

    Every BUY becomes macro-grid import and every SELL becomes export.
    """

    def __init__(self) -> None:
        self.orders: list[Order] = []

    def submit(self, order: Order) -> None:
        self.orders.append(order)

    def orders_at(self, step: int) -> list[Order]:
        return [order for order in self.orders if order.step == step]

    def settle_step(self, step: int, tariff: TariffConfig) -> SettlementResult:
        del tariff
        step_orders = self.orders_at(step)
        grid_import = sum(
            o.quantity_kwh for o in step_orders if o.side is OrderSide.BUY
        )
        grid_export = sum(
            o.quantity_kwh for o in step_orders if o.side is OrderSide.SELL
        )
        return SettlementResult(
            grid_import_kwh=grid_import,
            grid_export_kwh=grid_export,
            local_traded_kwh=0.0,
            trade_count=0,
            avg_trade_price=None,
        )
