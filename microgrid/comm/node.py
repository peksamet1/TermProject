"""One agent's ZeroMQ endpoint: persistent PUB, SUB and REP sockets.

Sockets are split by pattern (PSD 5.1, only PUB-SUB and REQ-REP):
- PUB: this node's STATUS / ORDER broadcasts, multipart ``[topic, json]``.
- SUB: connected to every other node's PUB. A SUB never hears its own PUB,
  so ``publish`` also delivers the node's own message straight into its own
  inbox (see ``expected_per_step``).
- REP: answers requests from peers (``serve_requests``).
- REQ: connected to one target's REP *only*, kept in a per-target pool of
  idle sockets and reused for the next request to that target.

Request / reply (step 4) is mechanism, not semantics: an opaque string body
goes to one peer and an opaque string (or ``None`` on timeout) comes back;
TRADE / NACK fill it in Phase 4. It is split into ``send_request`` and
``collect_reply`` because in one thread nobody serves the target's REP while
a requester waits: a blocking ``REQ.recv`` deadlocks the process
(Architecture 4.3). ``PeerNetwork.negotiation_rounds`` therefore runs three
rounds: every requester sends, every target drains its REP, every requester
collects. ``request`` is send + collect in one call and only gets an answer
when the target is served while it waits; every wait is a bounded poll.

Why REQs are pooled instead of opened and closed per request (the plan's
fallback, step 4 item 6, taken because of a measurement): over tcp every new
REQ is a new TCP connection, and each one closed parks an ephemeral port in
TIME_WAIT for 2 x MSL (30 s on macOS, 16384 ports). One N=3 + N=10 run of
20 everyone-asks-everyone rounds left 2112 ports in TIME_WAIT; back-to-back
runs reached 14343 and then showed up as timeouts at N=10 and as
"Can't assign requested address" at N=50. These counts are a one-off
in-session observation (macOS 26.6.2 arm64, Python 3.13.1, ad-hoc script
not in the repo, per-request REQs before pooling); only the 30 s and the
16384 come from the OS settings (sysctl net.inet.tcp.msl,
net.inet.ip.portrange). Step 5 re-measures. A pooled REQ still talks to
exactly one REP, so the design constraint holds. A REQ that times out is
closed, never pooled: it is stuck waiting for a reply it may never get, and
a late reply must not be read as the answer to the next request. A second
concurrent request to the same target opens another REQ, which joins the
pool once answered.

Readiness handshake (step 2): a STATUS with ``step == HANDSHAKE_STEP`` only
signals "my PUB reaches you". It is recorded in ``seen_peers`` and never
enters a step inbox. Once the network declares the node ready, leftover
handshake STATUSes still in flight are dropped and only tallied in
``handshake_leftovers``: they are neither lost nor late, and counting them
as such would dirty the loss counters before the first real step.

Step traffic (step 3): every STATUS / ORDER of a step that is still open
lands in ``inbox[step]``, including one for a *later* step: ZeroMQ delivery
is asynchronous, so step t + 1 can overtake the close of step t, and
dropping it would show up as loss one step later. Once a step's barrier has
closed, a message for it is counted as ``late`` and never added: the box a
node's CDA copy is fed from must be exactly the set seen at close, or nodes
that closed with different sets would produce different trades
(Architecture 4.4). ``orders_at`` sorts by ``(seq, agent_id)``, never by
arrival, which differs per node. Anything that does not decode into a
well-formed STATUS / ORDER from a known peer is counted in
``protocol_errors`` and skipped; it never stops the run.

Lifecycle is split in two on purpose: the constructor binds PUB and REP,
``connect_peers`` connects the SUB. ``PeerNetwork`` constructs *all* nodes
before connecting any of them, because inproc does not guarantee
connect-before-bind and over tcp an early connect only produces retries.

Every socket gets ``LINGER=0`` so that closing never waits on unsent
messages; a lingering socket is what makes ``ctx.term()`` hang a test run.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

import zmq

from microgrid.comm.protocol import (
    OrderMessage,
    ProtocolError,
    StatusMessage,
    decode_message,
    encode_message,
    make_status_message,
)
from microgrid.config import CommConfig

# ZeroMQ topic matching is a *prefix* match: subscribing to b"status" also
# delivers b"status2", b"statusX", ... Topics are therefore kept to exactly
# these two names; adding a topic that starts with either one would leak it
# into the inbox of every subscriber.
TOPIC_STATUS = b"status"
TOPIC_ORDER = b"order"
SUBSCRIBED_TOPICS: tuple[bytes, ...] = (TOPIC_STATUS, TOPIC_ORDER)

TRANSPORTS = ("tcp", "inproc")

# Step value of readiness handshake STATUSes; real steps start at 0.
HANDSHAKE_STEP = -1

# Wire value of HouseholdState.IDLE. A STATUS in any other state commits its
# sender to exactly one ORDER in the same step (Architecture 4.3).
IDLE_STATE = "IDLE"

# Start of the reply a target sends when its handler raised. The body is
# opaque in Phase 3, so this is only the default; Phase 4 passes an
# ``error_reply`` that builds a NACK instead.
ERROR_REPLY_PREFIX = "ERROR "


def default_error_reply(body: str, exc: Exception) -> str:
    """Reply for a request whose handler raised: never leave it unanswered."""
    return f"{ERROR_REPLY_PREFIX}{type(exc).__name__}: {exc}"


def is_error_reply(reply: str | None) -> bool:
    return reply is not None and reply.startswith(ERROR_REPLY_PREFIX)


def _encode_reply(reply: object, source: str) -> bytes:
    # Raises for a non-str or a str UTF-8 cannot encode (a lone surrogate).
    if not isinstance(reply, str):
        raise TypeError(f"{source} returned {type(reply).__name__}, not str")
    return reply.encode("utf-8")


def expected_per_step(num_nodes: int) -> int:
    """STATUSes a node holds once a step's barrier is complete.

    N - 1 arrive from peers over the SUB; those are what the barrier waits
    for and what can be lost. The node's own one never travels (a SUB does
    not hear its own PUB) and is delivered locally by ``publish``. The STATUS
    count derives from this function and nowhere else; ``barrier_state``
    subtracts the local one. It is *not* the ORDER count: an IDLE peer sends no ORDER, so expected ORDERs
    are derived from the non-IDLE STATUSes actually received.
    """
    from_peers = num_nodes - 1
    own = 1
    return from_peers + own


@dataclass
class CommStats:
    """Communication counters (Architecture 4.3).

    Each ``PeerNode`` fills the ``NODE_COUNTERS`` fields and ``latency_ms`` in
    its own instance; ``PeerNetwork`` keeps the network-wide fields and its
    ``stats()`` sums the per-node ones into a single snapshot.
    """

    handshake_ms: float | None = None
    handshake_rounds: int = 0
    # Peer handshake STATUSes that arrived after readiness; dropped, not
    # lost/late. A node's own local delivery never arrives, so never counts.
    handshake_leftovers: int = 0
    # Network-wide: closed steps, and those whose barrier ran into
    # step_timeout_s with something still missing on at least one node.
    steps: int = 0
    degraded_steps: int = 0
    # Degraded steps in which a STATUS was lost: in these order_loss is only
    # a lower bound, so a "0 loss" claim has to state this count next to it.
    status_loss_steps: int = 0
    barrier_ms: list[float] = field(default_factory=list)
    # Barrier accounting at close. STATUS is expected from every peer, ORDER
    # from every peer whose *received* STATUS is not IDLE: the ORDER of a
    # peer whose STATUS was lost is unknown rather than lost, so order counts
    # are a lower bound whenever status_loss > 0.
    status_expected: int = 0
    status_received: int = 0
    order_expected: int = 0
    order_received: int = 0
    # Arrived after its step closed: counted, never added to the box.
    late: int = 0
    protocol_errors: int = 0
    # Receive time minus the envelope's sent_at, both time.time(). In one
    # process this is mostly time spent waiting for the loop to drain, which
    # is why it is reported next to barrier_ms (Architecture 4.3).
    latency_ms: list[float] = field(default_factory=list)
    # REQ-REP, requester side: sent, answered, and given up on after the
    # timeout (the reply, if it comes, is dropped with the closed REQ).
    requests: int = 0
    replies: int = 0
    timeouts: int = 0
    # REQ-REP, target side: requests answered, and how many of those got the
    # error reply because the handler raised.
    served: int = 0
    handler_errors: int = 0
    # Send to reply receipt, time.perf_counter(). Under negotiation_rounds it
    # includes waiting for the other rounds, like latency_ms does.
    rtt_ms: list[float] = field(default_factory=list)
    # REQ sockets created; with the pool this stays far below ``requests``.
    req_opened: int = 0
    # Socket churn of one request: create + connect and close of a REQ it
    # needed; only the pool lookup when a pooled REQ was reused and returned
    # (plan step 4, item 6).
    req_churn_ms: list[float] = field(default_factory=list)

    @property
    def status_loss(self) -> int:
        return self.status_expected - self.status_received

    @property
    def order_loss(self) -> int:
        return self.order_expected - self.order_received


# CommStats counters a node fills itself; PeerNetwork.stats() sums them.
NODE_COUNTERS = (
    "handshake_leftovers",
    "status_expected",
    "status_received",
    "order_expected",
    "order_received",
    "late",
    "protocol_errors",
    "requests",
    "replies",
    "timeouts",
    "served",
    "handler_errors",
    "req_opened",
)
# CommStats sample lists a node fills itself; PeerNetwork.stats() concatenates them.
NODE_SAMPLES = ("latency_ms", "rtt_ms", "req_churn_ms")


@dataclass(eq=False)
class PendingRequest:
    """One request in flight; its REQ is pooled or closed when it finishes."""

    node_id: int
    peer_id: int
    body: str
    socket: zmq.Socket | None
    sent_at: float  # time.perf_counter()
    churn_ms: float  # create + connect, or the pool lookup; close is added on finish
    reply: str | None = None
    rtt_ms: float | None = None
    done: bool = False


@dataclass(frozen=True)
class StepResult:
    """One node's barrier for one step: what was due and what is missing."""

    node_id: int
    step: int
    status_expected: int
    order_expected: int
    missing_status: tuple[int, ...]
    missing_orders: tuple[int, ...]

    @property
    def status_loss(self) -> int:
        return len(self.missing_status)

    @property
    def order_loss(self) -> int:
        return len(self.missing_orders)

    @property
    def complete(self) -> bool:
        return not self.missing_status and not self.missing_orders

    @property
    def degraded(self) -> bool:
        """Closed with something missing; a lost STATUS alone is enough."""
        return not self.complete


class CommError(RuntimeError):
    """Base class for communication layer failures."""


class HandshakeTimeout(CommError):
    """Raised when the readiness handshake does not complete in time."""


class StepTimeout(CommError):
    """Reserved; nothing raises it today.

    ``PeerNetwork.await_step`` never raises on a timeout: it counts the loss
    and returns a degraded ``StepReport`` (see its module docstring), so
    there is nothing to catch around the barrier.
    """


class PeerNode:
    """PUB + SUB + REP sockets for one agent, on a context owned by the network."""

    def __init__(self, node_id: int, ctx: zmq.Context, config: CommConfig) -> None:
        if config.transport not in TRANSPORTS:
            raise ValueError(
                f"unknown transport: {config.transport!r} (expected one of {TRANSPORTS})"
            )
        self.node_id = node_id
        self.config = config
        self._ctx = ctx  # per-request REQ sockets are created on it
        self._closed = False
        # Set before any socket: close() runs on a failed constructor too.
        self._pending: set[PendingRequest] = set()
        # Idle REQs per target id, each connected to that target's REP only.
        self._idle_reqs: dict[int, list[zmq.Socket]] = {}
        self.pub: zmq.Socket | None = None
        self.sub: zmq.Socket | None = None
        self.rep: zmq.Socket | None = None
        try:
            self.pub = self._socket(ctx, zmq.PUB)
            self.rep = self._socket(ctx, zmq.REP)
            self.sub = self._socket(ctx, zmq.SUB)
            for topic in SUBSCRIBED_TOPICS:
                self.sub.setsockopt(zmq.SUBSCRIBE, topic)
            self.pub_endpoint = self._bind(self.pub, "pub")
            self.rep_endpoint = self._bind(self.rep, "rep")
        except Exception:
            self.close()
            raise
        self.connected_peers: list[str] = []
        self.peer_ids: set[int] = set()
        # REP endpoint per node id, own included: what a REQ connects to.
        self.rep_endpoints: dict[int, str] = {node_id: self.rep_endpoint}
        self._poller = zmq.Poller()
        self._poller.register(self.sub, zmq.POLLIN)
        self._rep_poller = zmq.Poller()
        self._rep_poller.register(self.rep, zmq.POLLIN)
        # Readiness handshake state: ids of peers whose PUB reached this SUB.
        self.seen_peers: set[int] = set()
        self.ready = False
        self.stats = CommStats()
        # inbox[step] = {"status": {agent_id: StatusMessage}, "orders": [OrderMessage]}
        # Only steps above last_closed_step receive messages; the last closed
        # one stays readable until the next close, older ones are dropped.
        self.inbox: dict[int, dict] = {}
        self.last_closed_step = HANDSHAKE_STEP  # nothing closed yet

    def _socket(self, ctx: zmq.Context, kind: int) -> zmq.Socket:
        # Options must be set before bind/connect: HWM applies only to
        # connections made after it is set.
        sock = ctx.socket(kind)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.SNDHWM, self.config.sndhwm)
        sock.setsockopt(zmq.RCVHWM, self.config.rcvhwm)
        return sock

    def _bind(self, sock: zmq.Socket, role: str) -> str:
        if self.config.transport == "inproc":
            endpoint = f"inproc://node-{self.node_id}-{role}"
            sock.bind(endpoint)
            return endpoint
        # A random free port keeps two models running at once from colliding.
        base = f"tcp://{self.config.bind_host}"
        port = sock.bind_to_random_port(base)
        return f"{base}:{port}"

    def connect_peers(
        self,
        pub_endpoints: Mapping[int, str],
        rep_endpoints: Mapping[int, str] | None = None,
    ) -> None:
        """Connect the SUB to every given peer PUB (never to its own).

        ``pub_endpoints`` maps node id to PUB endpoint; the ids become
        ``peer_ids``, the set the step barrier waits for. ``rep_endpoints``
        is only recorded: a REQ connects per request, in ``send_request``.
        """
        if self._closed or self.sub is None:
            raise CommError(f"node {self.node_id} is closed")
        if rep_endpoints is not None:
            self.rep_endpoints.update(rep_endpoints)
        for peer_id, endpoint in pub_endpoints.items():
            if peer_id == self.node_id:
                continue
            self.sub.connect(endpoint)
            self.connected_peers.append(endpoint)
            self.peer_ids.add(peer_id)

    def publish_status(self, msg: StatusMessage) -> None:
        """Broadcast this node's STATUS for ``msg.step``, IDLE included: the
        barrier of every peer waits for one from this node every step."""
        self._publish_step_traffic(msg, StatusMessage)

    def publish_order(self, msg: OrderMessage) -> None:
        """Broadcast this node's ORDER; ``msg.seq`` must already be set."""
        self._publish_step_traffic(msg, OrderMessage)

    def _publish_step_traffic(self, msg: StatusMessage | OrderMessage, kind: type) -> None:
        if not isinstance(msg, kind):
            raise TypeError(f"expected {kind.__name__}, got {type(msg).__name__}")
        if not self.ready:
            # Before the handshake a freshly connected SUB still drops what
            # it is sent (slow joiner): this would be silent loss.
            raise CommError(
                f"node {self.node_id} is not ready: run PeerNetwork.handshake() first"
            )
        self.publish(msg)

    def publish(self, msg: StatusMessage | OrderMessage) -> None:
        """Broadcast ``msg`` to all peers and deliver it to this node too.

        Low level: step traffic goes through ``publish_status`` /
        ``publish_order``, which also require readiness. Whatever would let
        the nodes' copies diverge is refused here, before anything is sent.
        """
        if self._closed or self.pub is None:
            raise CommError(f"node {self.node_id} is closed")
        self._check_outbound(msg)
        self.pub.send_multipart(
            [msg.topic.encode("ascii"), encode_message(msg).encode("utf-8")]
        )
        self._deliver(msg)

    def _check_outbound(self, msg: StatusMessage | OrderMessage) -> None:
        if not isinstance(msg, (StatusMessage, OrderMessage)):
            raise TypeError(f"only STATUS / ORDER go over PUB-SUB, got {type(msg).__name__}")
        if msg.agent_id != self.node_id:
            raise ValueError(f"node {self.node_id} cannot publish for agent {msg.agent_id}")
        if msg.step == HANDSHAKE_STEP:
            return
        if isinstance(msg, OrderMessage) and type(msg.seq) is not int:
            # seq is the step's *global* send order, i.e. Mesa activation
            # order, assigned by ZmqMarket.submit (Phase 4) and only carried
            # here. A per-node counter would give every ORDER seq 0: sorting
            # would fall back to agent_id and CDA time priority would vanish
            # while every copy still agreed, so no test would notice.
            raise ValueError(
                f"ORDER from node {self.node_id} has no seq: the caller assigns it"
            )
        if msg.step <= self.last_closed_step:
            raise CommError(f"step {msg.step} is already closed on node {self.node_id}")
        box = self.inbox.get(msg.step)
        if box is not None:
            # The barrier waits for one STATUS and at most one ORDER per node
            # and step; a second one could reach some nodes before their close
            # and others after it.
            if isinstance(msg, StatusMessage):
                duplicate = self.node_id in box["status"]
            else:
                duplicate = any(o.agent_id == self.node_id for o in box["orders"])
            if duplicate:
                raise CommError(
                    f"node {self.node_id} already published a {msg.type} for step {msg.step}"
                )

    def publish_handshake(self) -> None:
        """Broadcast one readiness STATUS; its payload is meaningless."""
        self.publish(
            make_status_message(
                agent_id=self.node_id,
                step=HANDSHAKE_STEP,
                generation_kwh=0.0,
                consumption_kwh=0.0,
                residual_kwh=0.0,
                state=IDLE_STATE,
            )
        )

    def drain(self, timeout_ms: int = 0) -> int:
        """Read every message waiting on the SUB; returns how many were read.

        Waits at most ``timeout_ms`` for the first one, then reads without
        blocking, so a silent peer can never stall the single thread. A
        malformed message is counted in ``protocol_errors`` and skipped.
        """
        if self._closed or self.sub is None:
            raise CommError(f"node {self.node_id} is closed")
        if not self._poller.poll(timeout_ms):
            return 0
        count = 0
        while True:
            try:
                frames = self.sub.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return count
            # time.time(), never a monotonic clock: sent_at is Unix time
            # (protocol v2) and the two clocks have unrelated origins.
            received_at = time.time()
            count += 1
            try:
                msg = self._decode_inbound(frames)
            except ProtocolError:
                self.stats.protocol_errors += 1
                continue
            if msg.step != HANDSHAKE_STEP and msg.sent_at is not None:
                self.stats.latency_ms.append(1000.0 * (received_at - msg.sent_at))
            self._deliver(msg)

    def _decode_inbound(self, frames: list[bytes]) -> StatusMessage | OrderMessage:
        """Decode one SUB delivery; anything unusable raises ProtocolError.

        ``decode_message`` checks the schema. On top of that this rejects
        what would otherwise crash or skew the barrier, the sort or the
        latency later: other message types, a non-integer step / agent_id /
        seq, a sender that is not a peer, a non-numeric sent_at.
        """
        try:
            msg = decode_message(frames[-1].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"payload is not UTF-8: {exc}") from exc
        if not isinstance(msg, (StatusMessage, OrderMessage)):
            raise ProtocolError(f"{msg.type} is not PUB-SUB traffic")
        if type(msg.step) is not int or msg.step < HANDSHAKE_STEP:
            raise ProtocolError(f"invalid step: {msg.step!r}")
        if type(msg.agent_id) is not int or msg.agent_id not in self.peer_ids:
            raise ProtocolError(f"sender {msg.agent_id!r} is not a peer of node {self.node_id}")
        if isinstance(msg, OrderMessage) and type(msg.seq) is not int:
            raise ProtocolError(f"ORDER without an integer seq: {msg.seq!r}")
        if msg.sent_at is not None and type(msg.sent_at) not in (int, float):
            raise ProtocolError(f"invalid sent_at: {msg.sent_at!r}")
        return msg

    def _deliver(self, msg: StatusMessage | OrderMessage) -> None:
        if msg.step == HANDSHAKE_STEP:
            if self.ready:
                if msg.agent_id != self.node_id:
                    self.stats.handshake_leftovers += 1
            elif isinstance(msg, StatusMessage) and msg.agent_id != self.node_id:
                self.seen_peers.add(msg.agent_id)
            return
        if msg.step <= self.last_closed_step:
            # Never added: the other nodes closed this step without it.
            self.stats.late += 1
            return
        box = self.inbox.setdefault(msg.step, {"status": {}, "orders": []})
        if isinstance(msg, StatusMessage):
            box["status"][msg.agent_id] = msg
        else:
            box["orders"].append(msg)

    def barrier_state(self, step: int) -> StepResult:
        """What ``step``'s barrier is still missing right now; no side effects.

        STATUS is due from every peer, ORDER from every peer whose received
        STATUS is not IDLE (the state is a commitment to an ORDER). An ORDER
        that arrives before its STATUS is held but not yet due. The node's
        own messages are local and never waited for.
        """
        box = self.inbox.get(step)
        statuses: dict[int, StatusMessage] = box["status"] if box else {}
        orders: list[OrderMessage] = box["orders"] if box else []
        committed = {
            agent_id
            for agent_id, status in statuses.items()
            if agent_id != self.node_id and status.state != IDLE_STATE
        }
        ordered = {o.agent_id for o in orders}
        return StepResult(
            node_id=self.node_id,
            step=step,
            # Own STATUS is local and never waited for.
            status_expected=expected_per_step(len(self.peer_ids) + 1) - 1,
            order_expected=len(committed),
            missing_status=tuple(sorted(self.peer_ids - statuses.keys())),
            missing_orders=tuple(sorted(committed - ordered)),
        )

    def close_step(self, step: int) -> StepResult:
        """Close ``step``'s barrier: freeze its box and count what is missing.

        Afterwards a message for ``step`` or any earlier step is late. The
        box stays readable until the next step closes; every older one is
        dropped here so 192 steps x 50 nodes never pile up in memory.
        """
        if step <= self.last_closed_step:
            raise CommError(f"step {step} is already closed on node {self.node_id}")
        result = self.barrier_state(step)
        self.last_closed_step = step
        for old in [s for s in self.inbox if s < step]:
            del self.inbox[old]
        stats = self.stats
        stats.status_expected += result.status_expected
        stats.status_received += result.status_expected - result.status_loss
        stats.order_expected += result.order_expected
        stats.order_received += result.order_expected - result.order_loss
        return result

    def statuses_at(self, step: int) -> dict[int, StatusMessage]:
        """The closed step's STATUSes by agent id, own one included."""
        return dict(sorted(self._closed_box(step)["status"].items()))

    def orders_at(self, step: int) -> list[OrderMessage]:
        """The closed step's ORDERs in ``(seq, agent_id)`` order, own included.

        This is what a node's CDA copy is fed (Phase 4), so nodes that closed
        with the same set get the same list. Arrival order never counts: it
        differs per node. msg_id only breaks a tie a valid step cannot have
        (seq is unique within a step).
        """
        orders = self._closed_box(step)["orders"]
        return sorted(orders, key=lambda m: (m.seq, m.agent_id, m.msg_id))

    def _closed_box(self, step: int) -> dict:
        # An open step's box is still filling and differs per node; a
        # released one is gone. Refuse both rather than return a partial set.
        if step != self.last_closed_step or step == HANDSHAKE_STEP:
            raise CommError(
                f"step {step} is not readable on node {self.node_id}: only the last "
                f"closed step ({self.last_closed_step}) is"
            )
        return self.inbox.get(step, {"status": {}, "orders": []})

    def send_request(self, peer_id: int, body: str) -> PendingRequest:
        """Send ``body`` to ``peer_id``'s REP and return at once.

        Takes an idle REQ for ``peer_id`` from the pool or opens one. The
        reply is read later with ``collect_reply``; until then the request
        is listed in ``open_requests``.
        """
        if self._closed:
            raise CommError(f"node {self.node_id} is closed")
        if peer_id not in self.rep_endpoints:
            raise ValueError(f"node {self.node_id} has no REP endpoint for peer {peer_id}")
        if not isinstance(body, str):
            raise TypeError(f"request body must be str, got {type(body).__name__}")
        payload = body.encode("utf-8")
        opened = time.perf_counter()
        idle = self._idle_reqs.get(peer_id)
        if idle:
            sock = idle.pop()
        else:
            sock = self._socket(self._ctx, zmq.REQ)
            try:
                # Connected to exactly one REP, ever: a REQ connected to
                # several REPs round-robins its requests over them, so the
                # target could not be chosen and the body would reach the
                # wrong agent (Architecture 4.3). Pooling is per target for
                # the same reason.
                sock.connect(self.rep_endpoints[peer_id])
            except Exception:
                sock.close(linger=0)
                raise
            self.stats.req_opened += 1
        sent_at = time.perf_counter()
        try:
            # HWM > 0 queues the message even before a fresh REQ's
            # connection is up, so this never blocks.
            sock.send(payload, zmq.NOBLOCK)
        except Exception:
            sock.close(linger=0)
            raise
        pending = PendingRequest(
            node_id=self.node_id,
            peer_id=peer_id,
            body=body,
            socket=sock,
            sent_at=sent_at,
            churn_ms=1000.0 * (sent_at - opened),
        )
        self._pending.add(pending)
        self.stats.requests += 1
        return pending

    def collect_reply(self, pending: PendingRequest, timeout_s: float = 0.0) -> str | None:
        """Wait at most ``timeout_s`` for ``pending``'s reply, then finish it.

        Returns the reply, or ``None`` after counting a timeout. An answered
        REQ goes back to the pool; a timed-out one is closed with
        ``LINGER=0``, so nothing waits on a peer that never answers. Never a
        blocking ``recv`` (see the module docstring).
        """
        if pending.done:
            return pending.reply
        if pending.socket is None or pending not in self._pending:
            raise CommError(f"request to {pending.peer_id} is not open on node {self.node_id}")
        if not pending.socket.poll(math.ceil(1000.0 * max(timeout_s, 0.0)), zmq.POLLIN):
            self._finish_request(pending, None)
            return None
        return self._read_reply(pending)

    def _read_reply(self, pending: PendingRequest) -> str | None:
        """Read the reply a poll reported as ready and finish ``pending``."""
        assert pending.socket is not None
        try:
            frames = pending.socket.recv_multipart(zmq.NOBLOCK)
        except zmq.Again:
            self._finish_request(pending, None)
            return None
        received_at = time.perf_counter()
        try:
            reply = frames[-1].decode("utf-8")
        except UnicodeDecodeError:
            # Answered, so not a timeout; but there is no str to hand back.
            self.stats.protocol_errors += 1
            self._finish_request(pending, None, timed_out=False, answered=True)
            return None
        pending.rtt_ms = 1000.0 * (received_at - pending.sent_at)
        self.stats.rtt_ms.append(pending.rtt_ms)
        self.stats.replies += 1
        self._finish_request(pending, reply, timed_out=False, answered=True)
        return reply

    def _finish_request(
        self,
        pending: PendingRequest,
        reply: str | None,
        timed_out: bool = True,
        answered: bool = False,
    ) -> None:
        """Pool the REQ if its reply was read, otherwise close it."""
        if timed_out:
            self.stats.timeouts += 1
        if pending.socket is not None:
            if answered and not self._closed:
                # Reply read: the REQ can send again, to this target only.
                self._idle_reqs.setdefault(pending.peer_id, []).append(pending.socket)
            else:
                closing = time.perf_counter()
                pending.socket.close(linger=0)
                pending.churn_ms += 1000.0 * (time.perf_counter() - closing)
        self.stats.req_churn_ms.append(pending.churn_ms)
        pending.socket = None
        pending.reply = reply
        pending.done = True
        self._pending.discard(pending)

    def request(self, peer_id: int, body: str, timeout_s: float | None = None) -> str | None:
        """Send ``body`` to ``peer_id`` and wait for the reply; ``None`` on timeout.

        Only answered when the target's REP is served while this waits; in
        the single-threaded simulation use ``PeerNetwork.negotiation_rounds``.
        Returns within ``timeout_s`` (default ``request_timeout_s``) either way.
        """
        if timeout_s is None:
            timeout_s = self.config.request_timeout_s
        return self.collect_reply(self.send_request(peer_id, body), timeout_s)

    def serve_requests(
        self,
        handler: Callable[[str], str],
        timeout_s: float = 0.0,
        error_reply: Callable[[str, Exception], str] = default_error_reply,
    ) -> int:
        """Answer every request waiting on the REP; returns how many.

        Waits at most ``timeout_s`` for the first one, then reads without
        blocking until the REP is empty. Every request read gets a reply:
        when ``handler`` raises, ``error_reply(body, exc)`` is sent instead
        (REP must answer before it can read the next request, and the
        requester would otherwise only see a timeout).
        """
        if self._closed or self.rep is None:
            raise CommError(f"node {self.node_id} is closed")
        if not self._rep_poller.poll(math.ceil(1000.0 * max(timeout_s, 0.0))):
            return 0
        count = 0
        while True:
            try:
                frames = self.rep.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                return count
            self.rep.send(self._reply_payload(frames[-1], handler, error_reply))
            self.stats.served += 1
            count += 1

    def _reply_payload(
        self,
        raw: bytes,
        handler: Callable[[str], str],
        error_reply: Callable[[str, Exception], str],
    ) -> bytes:
        """The reply to one request, as bytes; never raises.

        Everything between the REP's recv and its send is guarded, encoding
        included: an exception escaping there would leave the REP stuck in
        its "must send" state, and every later request to this node would
        time out for the rest of the run.
        """
        try:
            return _encode_reply(handler(raw.decode("utf-8")), "handler")
        except Exception as exc:
            self.stats.handler_errors += 1
            body = raw.decode("utf-8", errors="replace")
            try:
                return _encode_reply(error_reply(body, exc), "error_reply")
            except Exception:
                # Last resort: lone surrogates in the message become "?".
                return default_error_reply(body, exc).encode("utf-8", errors="replace")

    @property
    def open_requests(self) -> int:
        """Requests sent and not yet finished (answered or timed out)."""
        return len(self._pending)

    @property
    def req_sockets(self) -> int:
        """REQ sockets this node holds: in flight plus idle in the pool."""
        return len(self._pending) + sum(len(idle) for idle in self._idle_reqs.values())

    @property
    def closed(self) -> bool:
        return self._closed

    def sockets(self) -> list[zmq.Socket]:
        return [s for s in (self.pub, self.sub, self.rep) if s is not None]

    def close(self) -> None:
        """Close all sockets, REQs included; safe to call more than once."""
        if self._closed:
            return
        for pending in list(self._pending):
            self._finish_request(pending, None, timed_out=False)
        for idle in self._idle_reqs.values():
            for sock in idle:
                sock.close(linger=0)
        self._idle_reqs.clear()
        for sock in self.sockets():
            sock.close(linger=0)
        self._closed = True
