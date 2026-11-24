"""Trading strategy: converts a residual energy balance into a priced order.

Pricing stays inside the two-tier tariff band (feed-in floor, retail ceiling):
any local trade inside that band is a win-win, since the seller earns more than
the feed-in tariff and the buyer pays less than retail.

With a battery, both the share stored and the price follow the start-of-step
state of charge (Architecture 4.2). s = (SoC − soc_min) / (soc_max − soc_min),
clipped to [0, 1]:

- surplus: the battery is asked for (1 − s) × net; the rest is sold.
- deficit: the battery is asked for s × |net|; the rest is bought.
- ask = feed-in + sell_margin × (1 − s) × band. A full battery sells cheaper.
- bid = retail − buy_margin × s × band. An empty battery bids up toward retail.

``plan_charge`` / ``plan_discharge`` still clip that target to power and the
SoC window. A household without a battery keeps the fixed margin and offers
its whole net. At the default margins (0.25) every bid stays above every ask,
so the book still clears on quantity; the share changes how much reaches the
book, and SoC moves the clearing price.
"""

from __future__ import annotations

import math

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

    @staticmethod
    def fullness(soc: float | None, soc_min: float | None, soc_max: float | None) -> float | None:
        """Start-of-step SoC scaled to [0, 1] inside the battery window.

        ``None`` when there is no usable battery reading (no battery, NaN, or
        a zero-width window). Callers then keep the fixed margin.
        """
        if soc is None or soc_min is None or soc_max is None:
            return None
        if not math.isfinite(soc) or not math.isfinite(soc_min) or not math.isfinite(soc_max):
            return None
        span = soc_max - soc_min
        if span <= 0:
            return None
        return min(1.0, max(0.0, (soc - soc_min) / span))

    def battery_target(self, net_kwh: float, fullness: float | None) -> float:
        """How much of this step's net the battery is asked to take.

        Positive is a charge target (kWh drawn from the household), negative a
        discharge target (kWh delivered back). ``fullness`` None keeps the
        earlier rule and offers the whole net. Otherwise start-of-step
        fullness ``s`` splits it: charge ``(1 − s) × net`` on a surplus,
        discharge ``s × |net|`` on a deficit. Power and the SoC window are
        applied later by ``plan_charge`` / ``plan_discharge``.
        """
        if fullness is None or net_kwh == 0.0:
            return net_kwh
        if net_kwh > 0:
            return (1.0 - fullness) * net_kwh
        return -fullness * abs(net_kwh)

    def sell_price(self, *, fullness: float | None = None) -> float:
        """Ask. ``fullness`` None is the fixed margin; 1 is a full battery."""
        room = 1.0 if fullness is None else 1.0 - fullness
        return self.tariff.feed_in_tl_per_kwh + self.sell_margin * room * self._band

    def buy_price(self, *, fullness: float | None = None) -> float:
        """Bid. ``fullness`` None is the fixed margin; 0 is an empty battery."""
        pressure = 1.0 if fullness is None else fullness
        return self.tariff.retail_tl_per_kwh - self.buy_margin * pressure * self._band

    def make_order(
        self,
        *,
        agent_id: int,
        residual_kwh: float,
        step: int,
        soc: float | None = None,
        soc_min: float | None = None,
        soc_max: float | None = None,
    ) -> Order | None:
        """Return a SELL order for a surplus, a BUY order for a deficit, or
        None when the residual is negligible."""
        level = self.fullness(soc, soc_min, soc_max)
        if residual_kwh >= self.min_order_kwh:
            price = self.sell_price(fullness=level)
        elif residual_kwh <= -self.min_order_kwh:
            price = self.buy_price(fullness=level)
        else:
            return None
        if not math.isfinite(price):
            raise ValueError("order price must be a finite number inside the tariff band")
        if not self.tariff.feed_in_tl_per_kwh <= price <= self.tariff.retail_tl_per_kwh:
            raise ValueError("order price must lie inside [feed-in, retail]")
        side = OrderSide.SELL if residual_kwh > 0 else OrderSide.BUY
        return Order(
            agent_id=agent_id,
            side=side,
            quantity_kwh=abs(residual_kwh),
            price_tl_per_kwh=price,
            step=step,
        )
