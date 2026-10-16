"""ZeroMQ-ready wire protocol: JSON string messages (no Python objects on the wire).

Phase 3 will wrap these strings in PUB-SUB multipart frames
``[topic_bytes, json_utf8]`` or REQ-REP single-frame bodies. Phase 4a only
defines encode/decode and the schema.

Version 2 changes (Phase 1, frozen data structures):
- Every message carries ``sent_at`` (sender's Unix time in seconds), used to
  measure communication latency.
- ORDER carries ``seq``: the send order within a step. Every node feeds its
  CDA copy in (seq, agent_id) order, so all copies produce the same trades.
- TRADE and NACK carry the id of the agent that sent them as ``sender_id``;
  there is no central "market" node any more.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal, Union

from microgrid.market.base import Order, OrderSide

# Bumped to 2 when sent_at and seq were added (see module docstring).
PROTOCOL_VERSION = 2

MessageType = Literal["STATUS", "ORDER", "TRADE", "SETTLE", "NACK"]


class ProtocolError(ValueError):
    """Raised when a wire payload cannot be decoded or fails validation."""


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    """Current Unix time in seconds; stamped into ``sent_at``."""
    return time.time()


@dataclass(frozen=True)
class StatusMessage:
    type: Literal["STATUS"]
    v: int
    msg_id: str
    sender_id: str
    step: int
    agent_id: int
    generation_kwh: float
    consumption_kwh: float
    residual_kwh: float
    state: str
    soc: float | None = None
    corr_id: str | None = None
    topic: str | None = "status"
    sent_at: float | None = None


@dataclass(frozen=True)
class OrderMessage:
    type: Literal["ORDER"]
    v: int
    msg_id: str
    sender_id: str
    step: int
    agent_id: int
    side: str
    quantity_kwh: float
    price_tl_per_kwh: float
    corr_id: str | None = None
    topic: str | None = "order"
    seq: int | None = None
    sent_at: float | None = None


@dataclass(frozen=True)
class TradeMessage:
    type: Literal["TRADE"]
    v: int
    msg_id: str
    sender_id: str
    step: int
    buyer_id: int
    seller_id: int
    quantity_kwh: float
    price_tl_per_kwh: float
    corr_id: str | None = None
    topic: str | None = "trade"
    sent_at: float | None = None


@dataclass(frozen=True)
class SettleMessage:
    type: Literal["SETTLE"]
    v: int
    msg_id: str
    sender_id: str
    step: int
    grid_import_kwh: float
    grid_export_kwh: float
    local_traded_kwh: float
    trade_count: int
    avg_trade_price: float | None = None
    corr_id: str | None = None
    topic: str | None = "settle"
    sent_at: float | None = None


@dataclass(frozen=True)
class NackMessage:
    type: Literal["NACK"]
    v: int
    msg_id: str
    sender_id: str
    step: int
    ref_msg_id: str
    reason: str
    corr_id: str | None = None
    topic: str | None = None
    sent_at: float | None = None


Message = Union[StatusMessage, OrderMessage, TradeMessage, SettleMessage, NackMessage]

_TYPE_MAP: dict[str, type] = {
    "STATUS": StatusMessage,
    "ORDER": OrderMessage,
    "TRADE": TradeMessage,
    "SETTLE": SettleMessage,
    "NACK": NackMessage,
}

_REQUIRED: dict[str, tuple[str, ...]] = {
    "STATUS": (
        "agent_id",
        "generation_kwh",
        "consumption_kwh",
        "residual_kwh",
        "state",
    ),
    "ORDER": ("agent_id", "side", "quantity_kwh", "price_tl_per_kwh"),
    "TRADE": ("buyer_id", "seller_id", "quantity_kwh", "price_tl_per_kwh"),
    "SETTLE": (
        "grid_import_kwh",
        "grid_export_kwh",
        "local_traded_kwh",
        "trade_count",
    ),
    "NACK": ("ref_msg_id", "reason"),
}


def encode_message(msg: Message) -> str:
    """Serialize a protocol message to a UTF-8 JSON string."""
    payload = asdict(msg)
    if payload.get("soc") is not None and isinstance(payload["soc"], float):
        if math.isnan(payload["soc"]):
            payload["soc"] = None
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def decode_message(raw: str) -> Message:
    """Parse and validate a JSON wire string into a typed message."""
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("message must be a JSON object")

    msg_type = data.get("type")
    if msg_type not in _TYPE_MAP:
        raise ProtocolError(f"unknown message type: {msg_type!r}")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version: {data.get('v')!r} (expected {PROTOCOL_VERSION})"
        )

    for field in ("msg_id", "sender_id", "step"):
        if field not in data:
            raise ProtocolError(f"missing envelope field: {field}")
    for field in _REQUIRED[msg_type]:
        if field not in data:
            raise ProtocolError(f"missing payload field for {msg_type}: {field}")

    if msg_type == "ORDER" and data["side"] not in ("BUY", "SELL"):
        raise ProtocolError(f"invalid order side: {data['side']!r}")

    cls = _TYPE_MAP[msg_type]
    allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    filtered = {k: v for k, v in data.items() if k in allowed}
    try:
        return cls(**filtered)  # type: ignore[misc]
    except TypeError as exc:
        raise ProtocolError(str(exc)) from exc


def make_order_message(
    order: Order,
    *,
    seq: int | None = None,
    sender_id: str | None = None,
    corr_id: str | None = None,
    msg_id: str | None = None,
) -> OrderMessage:
    """Wrap an Order for the wire. ``seq`` is the send order within the step."""
    return OrderMessage(
        type="ORDER",
        v=PROTOCOL_VERSION,
        msg_id=msg_id or _new_id(),
        sender_id=sender_id if sender_id is not None else str(order.agent_id),
        step=order.step,
        agent_id=order.agent_id,
        side=order.side.value,
        quantity_kwh=order.quantity_kwh,
        price_tl_per_kwh=order.price_tl_per_kwh,
        corr_id=corr_id,
        topic="order",
        seq=seq,
        sent_at=_now(),
    )


def message_to_order(msg: OrderMessage) -> Order:
    return Order(
        agent_id=msg.agent_id,
        side=OrderSide(msg.side),
        quantity_kwh=msg.quantity_kwh,
        price_tl_per_kwh=msg.price_tl_per_kwh,
        step=msg.step,
    )


def make_status_message(
    *,
    agent_id: int,
    step: int,
    generation_kwh: float,
    consumption_kwh: float,
    residual_kwh: float,
    state: str,
    soc: float | None = None,
    sender_id: str | None = None,
    msg_id: str | None = None,
) -> StatusMessage:
    soc_out: float | None = soc
    if soc is not None and isinstance(soc, float) and math.isnan(soc):
        soc_out = None
    return StatusMessage(
        type="STATUS",
        v=PROTOCOL_VERSION,
        msg_id=msg_id or _new_id(),
        sender_id=sender_id if sender_id is not None else str(agent_id),
        step=step,
        agent_id=agent_id,
        generation_kwh=generation_kwh,
        consumption_kwh=consumption_kwh,
        residual_kwh=residual_kwh,
        state=state,
        soc=soc_out,
        corr_id=None,
        topic="status",
        sent_at=_now(),
    )


def make_trade_message(
    *,
    buyer_id: int,
    seller_id: int,
    quantity_kwh: float,
    price_tl_per_kwh: float,
    step: int,
    sender_id: str,
    corr_id: str | None = None,
    msg_id: str | None = None,
) -> TradeMessage:
    """Build a TRADE. ``sender_id`` is the agent sending it (buyer proposes,
    seller confirms); there is no central market node."""
    return TradeMessage(
        type="TRADE",
        v=PROTOCOL_VERSION,
        msg_id=msg_id or _new_id(),
        sender_id=sender_id,
        step=step,
        buyer_id=buyer_id,
        seller_id=seller_id,
        quantity_kwh=quantity_kwh,
        price_tl_per_kwh=price_tl_per_kwh,
        corr_id=corr_id,
        topic="trade",
        sent_at=_now(),
    )


def make_settle_message(
    *,
    step: int,
    grid_import_kwh: float,
    grid_export_kwh: float,
    local_traded_kwh: float,
    trade_count: int,
    avg_trade_price: float | None = None,
    sender_id: str = "market",
    msg_id: str | None = None,
) -> SettleMessage:
    return SettleMessage(
        type="SETTLE",
        v=PROTOCOL_VERSION,
        msg_id=msg_id or _new_id(),
        sender_id=sender_id,
        step=step,
        grid_import_kwh=grid_import_kwh,
        grid_export_kwh=grid_export_kwh,
        local_traded_kwh=local_traded_kwh,
        trade_count=trade_count,
        avg_trade_price=avg_trade_price,
        corr_id=None,
        topic="settle",
        sent_at=_now(),
    )


def make_nack_message(
    *,
    step: int,
    ref_msg_id: str,
    reason: str,
    sender_id: str,
    corr_id: str | None = None,
    msg_id: str | None = None,
) -> NackMessage:
    """Build a NACK. ``sender_id`` is the agent rejecting the request."""
    return NackMessage(
        type="NACK",
        v=PROTOCOL_VERSION,
        msg_id=msg_id or _new_id(),
        sender_id=sender_id,
        step=step,
        ref_msg_id=ref_msg_id,
        reason=reason,
        corr_id=corr_id,
        topic=None,
        sent_at=_now(),
    )
