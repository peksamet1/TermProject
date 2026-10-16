"""CDA matching and settlement unit tests."""

import pytest

from microgrid.comm.protocol import (
    decode_message,
    encode_message,
    make_trade_message,
)
from microgrid.config import TariffConfig
from microgrid.market.base import Order, OrderSide
from microgrid.market.cda import CDAMarket
from microgrid.model import MicrogridModel


def _order(agent_id, side, qty, price, step=0):
    return Order(
        agent_id=agent_id,
        side=side,
        quantity_kwh=qty,
        price_tl_per_kwh=price,
        step=step,
    )


def test_bid_meets_ask_creates_trade_at_midpoint():
    market = CDAMarket()
    market.submit(_order(1, OrderSide.SELL, 1.0, 1.5))
    market.submit(_order(2, OrderSide.BUY, 1.0, 2.5))
    assert len(market.trades) == 1
    trade = market.trades[0]
    assert trade.buyer_id == 2
    assert trade.seller_id == 1
    assert trade.quantity_kwh == pytest.approx(1.0)
    assert trade.price_tl_per_kwh == pytest.approx(2.0)


def test_partial_fill_leaves_remainder_on_book():
    market = CDAMarket()
    market.submit(_order(1, OrderSide.SELL, 2.0, 1.5))
    market.submit(_order(2, OrderSide.BUY, 0.5, 2.5))
    assert market.trades[0].quantity_kwh == pytest.approx(0.5)
    assert len(market._asks) == 1
    assert market._asks[0].remaining_kwh == pytest.approx(1.5)


def test_price_priority_higher_bid_matches_first():
    """Resting bids: when an ask arrives, the highest bid takes it."""
    market = CDAMarket()
    market.submit(_order(2, OrderSide.BUY, 1.0, 2.1))  # lower bid resting
    market.submit(_order(3, OrderSide.BUY, 1.0, 2.5))  # higher bid resting
    market.submit(_order(1, OrderSide.SELL, 1.0, 2.0))
    assert len(market.trades) == 1
    assert market.trades[0].buyer_id == 3


def test_time_priority_earlier_ask_at_same_price():
    market = CDAMarket()
    market.submit(_order(1, OrderSide.SELL, 1.0, 2.0))
    market.submit(_order(2, OrderSide.SELL, 1.0, 2.0))
    market.submit(_order(3, OrderSide.BUY, 1.0, 2.0))
    assert market.trades[0].seller_id == 1


def test_no_match_when_bid_below_ask_goes_to_grid():
    market = CDAMarket()
    market.submit(_order(1, OrderSide.SELL, 1.0, 3.0))
    market.submit(_order(2, OrderSide.BUY, 1.0, 2.0))
    assert market.trades == []
    result = market.settle_step(0, TariffConfig())
    assert result.local_traded_kwh == pytest.approx(0.0)
    assert result.grid_import_kwh == pytest.approx(1.0)
    assert result.grid_export_kwh == pytest.approx(1.0)


def test_trade_encodes_through_protocol():
    market = CDAMarket()
    market.submit(_order(1, OrderSide.SELL, 0.8, 1.6))
    market.submit(_order(2, OrderSide.BUY, 0.8, 2.4))
    trade = market.trades[0]
    msg = make_trade_message(
        buyer_id=trade.buyer_id,
        seller_id=trade.seller_id,
        quantity_kwh=trade.quantity_kwh,
        price_tl_per_kwh=trade.price_tl_per_kwh,
        step=trade.step,
        sender_id=str(trade.seller_id),
    )
    decoded = decode_message(encode_message(msg))
    assert decoded.quantity_kwh == pytest.approx(trade.quantity_kwh)
    assert decoded.price_tl_per_kwh == pytest.approx(trade.price_tl_per_kwh)


def test_model_cda_produces_local_trades_and_lower_grid_than_baseline():
    cda = MicrogridModel(market_backend="cda", seed=42)
    log = MicrogridModel(market_backend="logging", seed=42)
    cda.run(96)
    log.run(96)
    cda_df = cda.datacollector.get_model_vars_dataframe()
    log_df = log.datacollector.get_model_vars_dataframe()
    assert cda_df["LocalTradedKwh"].sum() > 0
    assert cda_df["GridImport"].sum() < log_df["GridImport"].sum()
