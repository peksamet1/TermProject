"""Wire-protocol round-trips and validation (ZeroMQ payload contract)."""

import math

import pytest

from microgrid.comm.protocol import (
    ProtocolError,
    decode_message,
    encode_message,
    make_nack_message,
    make_order_message,
    make_settle_message,
    make_status_message,
    make_trade_message,
    message_to_order,
)
from microgrid.market.base import Order, OrderSide
from microgrid.market.cda import Trade


def test_order_round_trip_preserves_fields():
    order = Order(
        agent_id=7,
        side=OrderSide.SELL,
        quantity_kwh=1.25,
        price_tl_per_kwh=1.775,
        step=12,
    )
    msg = make_order_message(order, corr_id="corr-1")
    raw = encode_message(msg)
    decoded = decode_message(raw)
    assert decoded.type == "ORDER"
    assert decoded.corr_id == "corr-1"
    assert decoded.topic == "order"
    assert message_to_order(decoded) == order


def test_trade_round_trip():
    trade = Trade(
        buyer_id=3,
        seller_id=1,
        quantity_kwh=0.5,
        price_tl_per_kwh=2.0,
        step=5,
    )
    msg = make_trade_message(
        buyer_id=trade.buyer_id,
        seller_id=trade.seller_id,
        quantity_kwh=trade.quantity_kwh,
        price_tl_per_kwh=trade.price_tl_per_kwh,
        step=trade.step,
        corr_id="abc",
    )
    decoded = decode_message(encode_message(msg))
    assert decoded.type == "TRADE"
    assert decoded.buyer_id == 3
    assert decoded.seller_id == 1
    assert decoded.quantity_kwh == pytest.approx(0.5)
    assert decoded.corr_id == "abc"


def test_status_nan_soc_becomes_null():
    msg = make_status_message(
        agent_id=2,
        step=1,
        generation_kwh=0.0,
        consumption_kwh=0.4,
        residual_kwh=-0.4,
        state="BUYING",
        soc=math.nan,
    )
    decoded = decode_message(encode_message(msg))
    assert decoded.soc is None
    assert decoded.topic == "status"


def test_settle_and_nack_round_trip():
    settle = make_settle_message(
        step=9,
        grid_import_kwh=1.0,
        grid_export_kwh=2.0,
        local_traded_kwh=3.0,
        trade_count=4,
        avg_trade_price=2.1,
    )
    assert decode_message(encode_message(settle)).trade_count == 4

    nack = make_nack_message(
        step=9, ref_msg_id="m1", reason="invalid", corr_id="c1"
    )
    decoded = decode_message(encode_message(nack))
    assert decoded.type == "NACK"
    assert decoded.corr_id == "c1"
    assert decoded.ref_msg_id == "m1"


def test_envelope_fields_always_present():
    msg = make_order_message(
        Order(1, OrderSide.BUY, 0.1, 2.5, 0)
    )
    data_keys = set(decode_message(encode_message(msg)).__dataclass_fields__)
    for key in ("type", "v", "msg_id", "sender_id", "step"):
        assert key in data_keys


def test_invalid_json_raises():
    with pytest.raises(ProtocolError):
        decode_message("{not-json")


def test_unknown_type_raises():
    with pytest.raises(ProtocolError):
        decode_message('{"type":"FOO","v":1,"msg_id":"x","sender_id":"1","step":0}')


def test_missing_field_raises():
    with pytest.raises(ProtocolError):
        decode_message(
            '{"type":"ORDER","v":1,"msg_id":"x","sender_id":"1","step":0,"agent_id":1}'
        )


def test_wrong_version_raises():
    raw = encode_message(make_order_message(Order(1, OrderSide.BUY, 0.1, 2.0, 0)))
    raw = raw.replace('"v":1', '"v":99')
    with pytest.raises(ProtocolError):
        decode_message(raw)
