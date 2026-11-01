"""Communication layer: wire protocol plus ZeroMQ peer sockets (Phase 3).

- ``protocol``: frozen v2 JSON messages.
- ``node``: ``PeerNode`` (PUB / SUB / REP sockets of one agent, step inbox,
  pooled per-target REQs), ``CommStats``, ``StepResult``, ``PendingRequest``
  and errors.
- ``network``: ``PeerNetwork`` (full-mesh setup, readiness handshake, step
  barrier, three-round request / reply, teardown), ``StepReport`` and
  ``RequestResult``.

Patterns (PSD 5.1 / 6.2): PUB-SUB carries STATUS and ORDER as multipart
``[topic, json]``; REQ-REP carries opaque string bodies, which Phase 4 fills
with TRADE and TRADE / NACK.

Phase 4 contract
================

What ``ZmqMarket`` (``market/zmq_market.py``) may rely on. Everything not
listed here is internal and may change.

Setup and teardown
    ``net = PeerNetwork(agent_ids, config.comm)`` builds one node per agent
    id (the Mesa ``unique_id``s), then ``net.handshake()`` blocks until every
    node hears every other one, or raises ``HandshakeTimeout``. Publishing
    before the handshake raises ``CommError``: a fresh SUB would drop it.
    ``net.close()`` (or ``with PeerNetwork(...)``) releases every socket;
    Phase 4 calls it from ``MicrogridModel.close()``.

One step ``t``, in this order
    1. ``submit(order)``: ``ZmqMarket`` sets ``seq`` to the step's *global*
       send counter (Mesa activation order, reset every step), wraps it with
       ``make_order_message(order, seq=seq)`` and calls
       ``net[order.agent_id].publish_order(msg)``. Never a per-node counter:
       every ORDER would get ``seq`` 0, sorting would fall back to
       ``agent_id`` and CDA time priority would vanish with every test green.
       A missing ``seq`` raises ``ValueError``.
    2. ``publish_status(agent)``: the ``MarketInterface`` hook, which
       ``HouseholdAgent.step()`` calls last, IDLE included. ``ZmqMarket``
       builds ``make_status_message`` from the agent's cached fields
       (``generation_kwh``, ``consumption_kwh``, ``residual_kwh``,
       ``state.value``, ``soc``), never from ``energy_at()``, and calls
       ``net[agent.unique_id].publish_status(msg)``. Same name, two things:
       the hook takes an agent, the node method a message. A non-IDLE
       STATUS commits its sender to one ORDER that step.
    3. ``report = net.await_step(t)``: waits for N - 1 STATUSes and one ORDER
       per non-IDLE peer on every node, then closes ``t`` everywhere. Never
       raises: after ``step_timeout_s`` whatever is missing is counted as
       lost and ``report.degraded`` is set. A message arriving later is
       counted as late and never added.
    4. ``net[i].orders_at(t)``: that node's ORDERs of ``t``, own included,
       sorted by ``(seq, agent_id)``; the input of node ``i``'s ``CDAMarket``
       copy (clear the copy after the step). ``statuses_at(t)`` likewise.
       Only the last closed step is readable; reading any other raises.
    5. ``results = net.negotiation_rounds(requests, handler)``: ``requests``
       are ``(requester, target, body)`` triples, ``handler(target, body)``
       returns the reply. Results come back in input order with
       ``reply is None`` for a timeout (``request_timeout_s``). A handler
       that raises still answers: with ``error_reply(body, exc)``, a keyword
       parameter of ``negotiation_rounds`` that defaults to
       ``default_error_reply``; pass one that builds a NACK. ``PeerNode.send_request`` / ``collect_reply`` /
       ``request`` exist, but one thread cannot wait on a REQ while also
       serving the target's REP, so the simulation uses this three-round
       drive.

Measurements
    ``net.stats()`` returns one ``CommStats`` for the whole network;
    ``summary()`` gives raw counters and p50 / p95. KPIs (confirmed /
    matched ratio, settlement) are Phase 4/6's.

Known margin
    All-to-all at N = 50 over tcp measured a REQ-REP RTT p95 of 472 ms
    over 5 rounds, and 525 ms with a 692 ms round in the first round alone,
    while connections are still cold; ``request_timeout_s`` defaults to
    1.0 s (echo handler, synthetic order stream, see README). A handler
    that does real CDA checks narrows this; measure it before relying on
    the default.

Checks to carry over
    ``tests/test_agents.py`` pins the hook: called last in ``step()`` with
    that step's values, once per agent and step, IDLE included, a non-IDLE
    STATUS matched by an ORDER, and profiles read exactly once per step.
    Run the same assertions against ``ZmqMarket``, whose real
    ``publish_status`` replaces the stubs used there.

Not in Phase 3, deliberately: the ``cda_zmq`` backend, copy matching,
TRADE / NACK semantics, ``trades_at``, settlement and grid fallback, and
``model.py`` integration.
"""

from microgrid.comm.network import PeerNetwork, RequestResult, StepReport
from microgrid.comm.node import (
    CommError,
    CommStats,
    HandshakeTimeout,
    PeerNode,
    PendingRequest,
    StepResult,
    StepTimeout,
    is_error_reply,
)
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
    "CommStats",
    "HandshakeTimeout",
    "NackMessage",
    "OrderMessage",
    "PeerNetwork",
    "PeerNode",
    "PendingRequest",
    "ProtocolError",
    "RequestResult",
    "SettleMessage",
    "StatusMessage",
    "StepReport",
    "StepResult",
    "StepTimeout",
    "TradeMessage",
    "decode_message",
    "encode_message",
    "is_error_reply",
    "make_nack_message",
    "make_order_message",
    "make_settle_message",
    "make_status_message",
    "make_trade_message",
    "message_to_order",
]
