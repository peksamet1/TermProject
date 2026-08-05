"""Continuous Double Auction (CDA) market clearing (Phase 4a).

Matching runs on every ``submit``. Mesa ``shuffle_do`` activation order is
time priority. Unmatched volume settles to the macro-grid at step end.
Trade objects are encodable via ``microgrid.comm.protocol``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from microgrid.config import TariffConfig
from microgrid.market.base import (
    MarketInterface,
    Order,
    OrderSide,
    SettlementResult,
)


@dataclass(frozen=True)
class Trade:
    buyer_id: int
    seller_id: int
    quantity_kwh: float
    price_tl_per_kwh: float
    step: int


@dataclass
class BookOrder:
    order: Order
    remaining_kwh: float
    arrival: int


@dataclass
class CDAMarket(MarketInterface):
    orders: list[Order] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    _bids: list[BookOrder] = field(default_factory=list)
    _asks: list[BookOrder] = field(default_factory=list)
    _arrival: int = 0
    _step_traded_kwh: float = 0.0
    _step_trade_count: int = 0
    _step_trade_price_volume: float = 0.0

    def submit(self, order: Order) -> None:
        if order.quantity_kwh <= 0:
            raise ValueError("order quantity must be positive")
        self.orders.append(order)
        book_order = BookOrder(
            order=order, remaining_kwh=order.quantity_kwh, arrival=self._arrival
        )
        self._arrival += 1
        if order.side is OrderSide.BUY:
            self._bids.append(book_order)
        else:
            self._asks.append(book_order)
        self._try_match()

    def _best_bid(self) -> BookOrder | None:
        active = [b for b in self._bids if b.remaining_kwh > 0]
        if not active:
            return None
        return max(active, key=lambda b: (b.order.price_tl_per_kwh, -b.arrival))

    def _best_ask(self) -> BookOrder | None:
        active = [a for a in self._asks if a.remaining_kwh > 0]
        if not active:
            return None
        return min(active, key=lambda a: (a.order.price_tl_per_kwh, a.arrival))

    def _try_match(self) -> None:
        while True:
            bid = self._best_bid()
            ask = self._best_ask()
            if bid is None or ask is None:
                break
            if bid.order.price_tl_per_kwh < ask.order.price_tl_per_kwh:
                break

            qty = min(bid.remaining_kwh, ask.remaining_kwh)
            price = 0.5 * (
                bid.order.price_tl_per_kwh + ask.order.price_tl_per_kwh
            )
            self.trades.append(
                Trade(
                    buyer_id=bid.order.agent_id,
                    seller_id=ask.order.agent_id,
                    quantity_kwh=qty,
                    price_tl_per_kwh=price,
                    step=bid.order.step,
                )
            )
            self._step_traded_kwh += qty
            self._step_trade_count += 1
            self._step_trade_price_volume += price * qty
            bid.remaining_kwh -= qty
            ask.remaining_kwh -= qty

        self._bids = [b for b in self._bids if b.remaining_kwh > 1e-12]
        self._asks = [a for a in self._asks if a.remaining_kwh > 1e-12]

    def orders_at(self, step: int) -> list[Order]:
        return [order for order in self.orders if order.step == step]

    def trades_at(self, step: int) -> list[Trade]:
        return [trade for trade in self.trades if trade.step == step]

    def settle_step(self, step: int, tariff: TariffConfig) -> SettlementResult:
        del tariff
        grid_import = sum(b.remaining_kwh for b in self._bids)
        grid_export = sum(a.remaining_kwh for a in self._asks)
        local = self._step_traded_kwh
        count = self._step_trade_count
        avg_price = self._step_trade_price_volume / local if local > 0 else None

        self._bids.clear()
        self._asks.clear()
        self._step_traded_kwh = 0.0
        self._step_trade_count = 0
        self._step_trade_price_volume = 0.0

        return SettlementResult(
            grid_import_kwh=grid_import,
            grid_export_kwh=grid_export,
            local_traded_kwh=local,
            trade_count=count,
            avg_trade_price=avg_price,
        )
