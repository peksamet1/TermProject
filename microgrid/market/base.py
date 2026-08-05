"""Market abstractions shared across phases.

Phase 2 ships only the interface plus a logging stub; the Phase 4 CDA engine
will implement ``MarketInterface`` so agent code stays untouched.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


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


class MarketInterface(ABC):
    @abstractmethod
    def submit(self, order: Order) -> None:
        """Submit a bid (BUY) or ask (SELL) to the market."""


class LoggingMarket(MarketInterface):
    """Phase 2 stub: records every order without matching anything.

    Since no order is ever matched, every BUY falls back to the macro-grid and
    every SELL is exported at the feed-in tariff. This is exactly the no-P2P
    baseline that Phase 6 will compare the CDA results against.
    """

    def __init__(self) -> None:
        self.orders: list[Order] = []

    def submit(self, order: Order) -> None:
        self.orders.append(order)

    def orders_at(self, step: int) -> list[Order]:
        return [order for order in self.orders if order.step == step]
