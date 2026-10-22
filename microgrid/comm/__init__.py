"""Communication layer: wire protocol plus ZeroMQ peer sockets (Phase 3).

- ``protocol``: frozen v2 JSON messages.
- ``node``: ``PeerNode`` (PUB / SUB / REP sockets of one agent) and errors.
- ``network``: ``PeerNetwork`` (full-mesh setup and teardown).

Phase 3 planned patterns (PSD 6.2):
- PUB-SUB: STATUS / ORDER / TRADE / SETTLE (multipart topic + JSON)
- REQ-REP: ORDER request with TRADE or NACK reply (same corr_id)
"""

from microgrid.comm.network import PeerNetwork
from microgrid.comm.node import CommError, HandshakeTimeout, PeerNode, StepTimeout
from microgrid.comm.protocol import (
    NackMessage,
    OrderMessage,
    ProtocolError,
    SettleMessage,
    StatusMessage,
    TradeMessage,
    decode_message,
    encode_message,
    make_nack_message,
    make_order_message,
    make_settle_message,
    make_status_message,
    make_trade_message,
    message_to_order,
)

__all__ = [
    "CommError",
    "HandshakeTimeout",
    "NackMessage",
    "OrderMessage",
    "PeerNetwork",
    "PeerNode",
    "ProtocolError",
    "SettleMessage",
    "StatusMessage",
    "StepTimeout",
    "TradeMessage",
    "decode_message",
    "encode_message",
    "make_nack_message",
    "make_order_message",
    "make_settle_message",
    "make_status_message",
    "make_trade_message",
    "message_to_order",
]
