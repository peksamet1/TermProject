"""Communication layer: wire protocol now; ZeroMQ sockets in Phase 3.

Phase 3 planned patterns (PSD 6.2):
- PUB-SUB: STATUS / ORDER / TRADE / SETTLE (multipart topic + JSON)
- REQ-REP: ORDER request with TRADE or NACK reply (same corr_id)
"""

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
    "NackMessage",
    "OrderMessage",
    "ProtocolError",
    "SettleMessage",
    "StatusMessage",
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
