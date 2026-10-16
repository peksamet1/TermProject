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
        sender_id="1",
        corr_id="abc",
    )
    decoded = decode_message(encode_message(msg))
    assert decoded.type == "TRADE"
    assert decoded.buyer_id == 3
    assert decoded.seller_id == 1
    assert decoded.quantity_kwh == pytest.approx(0.5)
    assert decoded.corr_id == "abc"
    assert decoded.sender_id == "1"


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
        step=9, ref_msg_id="m1", reason="invalid", sender_id="1", corr_id="c1"
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
        decode_message('{"type":"FOO","v":2,"msg_id":"x","sender_id":"1","step":0}')


def test_missing_field_raises():
    with pytest.raises(ProtocolError):
        decode_message(
            '{"type":"ORDER","v":2,"msg_id":"x","sender_id":"1","step":0,"agent_id":1}'
        )


def test_wrong_version_raises():
    raw = encode_message(make_order_message(Order(1, OrderSide.BUY, 0.1, 2.0, 0)))
    raw = raw.replace('"v":2', '"v":99')
    with pytest.raises(ProtocolError):
        decode_message(raw)


# --- Protocol version 2: sent_at, seq and agent sender ids -----------------


def test_every_message_carries_sent_at():
    order = make_order_message(Order(1, OrderSide.BUY, 0.1, 2.0, 0))
    status = make_status_message(
        agent_id=1, step=0, generation_kwh=0.0, consumption_kwh=0.1,
        residual_kwh=-0.1, state="BUYING",
    )
    trade = make_trade_message(
        buyer_id=2, seller_id=1, quantity_kwh=0.1, price_tl_per_kwh=2.0,
        step=0, sender_id="2",
    )
    nack = make_nack_message(step=0, ref_msg_id="m1", reason="x", sender_id="1")
    for msg in (order, status, trade, nack):
        decoded = decode_message(encode_message(msg))
        # sent_at must survive the round trip (it is a real dataclass field).
        assert isinstance(decoded.sent_at, float)
        assert decoded.sent_at == pytest.approx(msg.sent_at)


def test_order_seq_round_trip():
    msg = make_order_message(Order(4, OrderSide.SELL, 0.3, 1.8, 2), seq=5)
    decoded = decode_message(encode_message(msg))
    assert decoded.seq == 5


def test_trade_and_nack_require_sender_id():
    # No default "market" sender any more: the sending agent must be named.
    with pytest.raises(TypeError):
        make_trade_message(  # type: ignore[call-arg]
            buyer_id=2, seller_id=1, quantity_kwh=0.1, price_tl_per_kwh=2.0, step=0
        )
    with pytest.raises(TypeError):
        make_nack_message(step=0, ref_msg_id="m1", reason="x")  # type: ignore[call-arg]


def test_version_1_message_is_rejected():
    raw = encode_message(make_order_message(Order(1, OrderSide.BUY, 0.1, 2.0, 0)))
    with pytest.raises(ProtocolError):
        decode_message(raw.replace('"v":2', '"v":1'))
