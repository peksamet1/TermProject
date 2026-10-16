"""Trading strategy: converts a residual energy balance into a priced order.

Pricing stays inside the two-tier tariff band (feed-in floor, retail ceiling):
any local trade inside that band is a win-win, since the seller earns more than
the feed-in tariff and the buyer pays less than retail. Phase 2 uses fixed
margins inside the band; Phase 4 will make pricing adaptive (e.g. based on SoC
and order book depth).
"""

from __future__ import annotations

from microgrid.config import TariffConfig
from microgrid.market.base import Order, OrderSide


class TradingStrategy:
    def __init__(
        self,
        *,
        tariff: TariffConfig,
        sell_margin: float = 0.25,
        buy_margin: float = 0.25,
        min_order_kwh: float = 0.01,
    ) -> None:
        if not 0 <= sell_margin <= 1 or not 0 <= buy_margin <= 1:
            raise ValueError("margins must be fractions in [0, 1]")
        self.tariff = tariff
        self.sell_margin = sell_margin
        self.buy_margin = buy_margin
        self.min_order_kwh = min_order_kwh

    @property
    def _band(self) -> float:
        return self.tariff.retail_tl_per_kwh - self.tariff.feed_in_tl_per_kwh

    def sell_price(self) -> float:
        return self.tariff.feed_in_tl_per_kwh + self.sell_margin * self._band

    def buy_price(self) -> float:
        return self.tariff.retail_tl_per_kwh - self.buy_margin * self._band

    def make_order(
        self, *, agent_id: int, residual_kwh: float, step: int
    ) -> Order | None:
        """Return a SELL order for a surplus, a BUY order for a deficit, or
        None when the residual is negligible."""
        if residual_kwh >= self.min_order_kwh:
            return Order(
                agent_id=agent_id,
                side=OrderSide.SELL,
                quantity_kwh=residual_kwh,
                price_tl_per_kwh=self.sell_price(),
                step=step,
            )
        if residual_kwh <= -self.min_order_kwh:
            return Order(
                agent_id=agent_id,
                side=OrderSide.BUY,
                quantity_kwh=-residual_kwh,
                price_tl_per_kwh=self.buy_price(),
                step=step,
            )
        return None
