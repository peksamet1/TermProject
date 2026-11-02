"""Decentralized CDA over ZeroMQ (Phase 4, Architecture 4.4).

There is no central market node. Every agent owns one ``PeerNode`` and each
step runs in three parts:

1. Broadcast: ``submit`` publishes the ORDER over PUB-SUB with ``seq`` = the
   step's global send counter (Mesa activation order); ``publish_status``
   publishes the agent's STATUS, IDLE included.
2. Same matching everywhere: ``settle_step`` waits for the step barrier,
   then every node feeds its ORDERs, sorted by ``(seq, agent_id)``, into its
   own ``CDAMarket`` copy. Same input -> same trade list on every copy.
3. Confirmation over REQ-REP: for each trade in its copy the buyer proposes
   a TRADE to the seller; the seller answers TRADE if its own copy holds the
   same trade, NACK otherwise. Only confirmed trades are paid locally;
   everything else settles with the macro-grid (the safe side).

A lossless run therefore gives exactly the trades of the in-process ``cda``
backend. The model always calls ``close()`` (``MicrogridModel.close``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from microgrid.comm.network import PeerNetwork, StepReport
from microgrid.comm.protocol import (
    NackMessage,
    ProtocolError,
    TradeMessage,
    decode_message,
    encode_message,
    make_nack_message,
    make_order_message,
    make_status_message,
    make_trade_message,
    message_to_order,
)
from microgrid.config import CommConfig, TariffConfig
from microgrid.market.base import (
    MarketInterface,
    Order,
    OrderSide,
    SettlementResult,
    Trade,
)
from microgrid.market.cda import CDAMarket

# Quantities and prices are compared after rounding so the same float
# computed on two copies always matches (it is bit-identical in practice).
_DIGITS = 9


def _trade_key(buyer_id: int, seller_id: int, qty: float, price: float, step: int) -> tuple:
    return (buyer_id, seller_id, round(qty, _DIGITS), round(price, _DIGITS), step)


@dataclass
class StepNegotiation:
    """What happened in one step's confirmation round (for metrics)."""

    step: int
    proposed: int = 0
    confirmed: int = 0
    nacked: int = 0
    timed_out: int = 0
    nack_reasons: list[str] = field(default_factory=list)


class ZmqMarket(MarketInterface):
    def __init__(self, agent_ids: list[int], config: CommConfig | None = None) -> None:
        self.net = PeerNetwork(agent_ids, config)
        try:
            self.net.handshake()
        except Exception:
            self.net.close()
            raise
        self.orders: list[Order] = []
        self.trades: list[Trade] = []
        self.reports: list[StepReport] = []
        self.negotiations: list[StepNegotiation] = []
        self._seq_step: int | None = None
        self._seq = 0
        # Seller-side view while the step's REQ-REP rounds run.
        self._open_trades: dict[int, Counter] = {}

    # --- Stage 1: broadcast ---------------------------------------------

    def submit(self, order: Order) -> None:
        if order.quantity_kwh <= 0:
            raise ValueError("order quantity must be positive")
        if order.step != self._seq_step:
            self._seq_step, self._seq = order.step, 0
        # Global counter, never per node (see microgrid.comm docstring).
        msg = make_order_message(order, seq=self._seq)
        self._seq += 1
        self.net[order.agent_id].publish_order(msg)
        self.orders.append(order)

    def publish_status(self, agent) -> None:
        # Cached fields only: a second profile read would shift model.rng.
        msg = make_status_message(
            agent_id=agent.unique_id,
            step=agent.model.current_step,
            generation_kwh=agent.generation_kwh,
            consumption_kwh=agent.consumption_kwh,
            residual_kwh=agent.residual_kwh,
            state=agent.state.value,
            soc=agent.soc,
        )
        self.net[agent.unique_id].publish_status(msg)

    def orders_at(self, step: int) -> list[Order]:
        return [order for order in self.orders if order.step == step]

    def trades_at(self, step: int) -> list[Trade]:
        return [trade for trade in self.trades if trade.step == step]

    # --- Stage 2: match on every copy, confirm, settle ---------------------

    def settle_step(self, step: int, tariff: TariffConfig) -> SettlementResult:
        del tariff
        self.reports.append(self.net.await_step(step))

        # Each node matches its own view of the step.
        copies: dict[int, list[Trade]] = {}
        for node_id in self.net.nodes:
            copy = CDAMarket()
            for msg in self.net[node_id].orders_at(step):
                copy.submit(message_to_order(msg))
            copies[node_id] = copy.trades_at(step)

        confirmed = self._confirm(step, copies)
        self.trades.extend(confirmed)
        return self._settlement(step, confirmed)

    def _confirm(self, step: int, copies: dict[int, list[Trade]]) -> list[Trade]:
        """Buyers propose their copy's trades; sellers check theirs."""
        self._open_trades = {
            node_id: Counter(
                _trade_key(t.buyer_id, t.seller_id, t.quantity_kwh, t.price_tl_per_kwh, t.step)
                for t in trades
                if t.seller_id == node_id
            )
            for node_id, trades in copies.items()
        }

        requests: list[tuple[int, int, str]] = []
        proposals: list[tuple[int, Trade]] = []  # (book index in buyer copy, trade)
        for buyer_id, trades in copies.items():
            for index, trade in enumerate(trades):
                if trade.buyer_id != buyer_id:
                    continue
                body = encode_message(make_trade_message(
                    buyer_id=trade.buyer_id,
                    seller_id=trade.seller_id,
                    quantity_kwh=trade.quantity_kwh,
                    price_tl_per_kwh=trade.price_tl_per_kwh,
                    step=step,
                    sender_id=str(buyer_id),
                ))
                requests.append((buyer_id, trade.seller_id, body))
                proposals.append((index, trade))

        log = StepNegotiation(step=step, proposed=len(requests))
        results = self.net.negotiation_rounds(requests, self._answer, error_reply=_nack_on_error)

        accepted: list[tuple[int, Trade]] = []
        for (index, trade), result in zip(proposals, results):
            if result.reply is None:
                log.timed_out += 1
                continue
            reply = decode_message(result.reply)
            if isinstance(reply, TradeMessage):
                log.confirmed += 1
                accepted.append((index, trade))
            else:
                log.nacked += 1
                log.nack_reasons.append(reply.reason if isinstance(reply, NackMessage) else "?")
        self.negotiations.append(log)
        self._open_trades = {}

        # Book order: in a lossless step every copy is identical, so the
        # index in the buyer's copy is the global match order.
        accepted.sort(key=lambda pair: pair[0])
        return [trade for _, trade in accepted]

    def _answer(self, seller_id: int, body: str) -> str:
        """Seller side of one proposal: TRADE if the own copy agrees, else NACK."""
        msg = decode_message(body)
        if not isinstance(msg, TradeMessage):
            raise ProtocolError(f"expected TRADE, got {msg.type}")
        key = _trade_key(msg.buyer_id, msg.seller_id, msg.quantity_kwh,
                         msg.price_tl_per_kwh, msg.step)
        open_trades = self._open_trades.get(seller_id, Counter())
        if msg.seller_id != seller_id or open_trades[key] <= 0:
            return encode_message(make_nack_message(
                step=msg.step, ref_msg_id=msg.msg_id,
                reason="trade not in seller book", sender_id=str(seller_id),
                corr_id=msg.msg_id,
            ))
        open_trades[key] -= 1  # each matched trade is confirmed at most once
        return encode_message(make_trade_message(
            buyer_id=msg.buyer_id, seller_id=msg.seller_id,
            quantity_kwh=msg.quantity_kwh, price_tl_per_kwh=msg.price_tl_per_kwh,
            step=msg.step, sender_id=str(seller_id), corr_id=msg.msg_id,
        ))

    def _settlement(self, step: int, confirmed: list[Trade]) -> SettlementResult:
        """Grid flow = submitted volume minus confirmed trade volume."""
        bought: Counter = Counter()
        sold: Counter = Counter()
        for trade in confirmed:
            bought[trade.buyer_id] += trade.quantity_kwh
            sold[trade.seller_id] += trade.quantity_kwh
        grid_import = grid_export = 0.0
        for order in self.orders_at(step):
            if order.side is OrderSide.BUY:
                grid_import += max(0.0, order.quantity_kwh - bought[order.agent_id])
            else:
                grid_export += max(0.0, order.quantity_kwh - sold[order.agent_id])
        local = sum(t.quantity_kwh for t in confirmed)
        value = sum(t.quantity_kwh * t.price_tl_per_kwh for t in confirmed)
        return SettlementResult(
            grid_import_kwh=grid_import,
            grid_export_kwh=grid_export,
            local_traded_kwh=local,
            trade_count=len(confirmed),
            avg_trade_price=value / local if local > 0 else None,
        )

    # --- Metrics and teardown ----------------------------------------------

    def comm_summary(self) -> dict[str, float | int | None]:
        """Communication counters plus confirmation counts for the whole run."""
        summary = self.net.stats().summary()
        summary["trades_proposed"] = sum(n.proposed for n in self.negotiations)
        summary["trades_confirmed"] = sum(n.confirmed for n in self.negotiations)
        summary["trades_nacked"] = sum(n.nacked for n in self.negotiations)
        summary["trades_timed_out"] = sum(n.timed_out for n in self.negotiations)
        return summary

    def close(self) -> None:
        self.net.close()


def _nack_on_error(body: str, exc: Exception) -> str:
    """Reply for a handler that raised: a NACK, so the buyer never hangs."""
    try:
        msg = decode_message(body)
        step, ref = msg.step, msg.msg_id
    except ProtocolError:
        step, ref = -1, "?"
    return encode_message(make_nack_message(
        step=step, ref_msg_id=ref, reason=f"error: {exc}", sender_id="?",
    ))
