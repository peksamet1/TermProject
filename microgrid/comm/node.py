"""One agent's ZeroMQ endpoint: persistent PUB, SUB and REP sockets.

Sockets are split by pattern (PSD 5.1, only PUB-SUB and REQ-REP):
- PUB: this node's STATUS / ORDER broadcasts, multipart ``[topic, json]``.
- SUB: connected to every other node's PUB. A SUB never hears its own PUB,
  so ``publish`` also delivers the node's own message straight into its own
  inbox; ``expected_per_step`` is the one place that count is defined.
- REP: answers requests from peers. REQ sockets are per request and come
  in a later step.

Readiness handshake (step 2): a STATUS with ``step == HANDSHAKE_STEP`` only
signals "my PUB reaches you". It is recorded in ``seen_peers`` and never
enters a step inbox. Once the network declares the node ready, leftover
handshake STATUSes still in flight are dropped and only tallied in
``handshake_leftovers``: they are neither lost nor late, and counting them
as such would dirty the loss counters before the first real step.

Lifecycle is split in two on purpose: the constructor binds PUB and REP,
``connect_peers`` connects the SUB. ``PeerNetwork`` constructs *all* nodes
before connecting any of them, because inproc does not guarantee
connect-before-bind and over tcp an early connect only produces retries.

Every socket gets ``LINGER=0`` so that closing never waits on unsent
messages; a lingering socket is what makes ``ctx.term()`` hang a test run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import zmq

from microgrid.comm.protocol import (
    Message,
    OrderMessage,
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


def expected_per_step(num_nodes: int) -> int:
    """STATUS barrier count: STATUSes a node holds for one complete step.

    N - 1 arrive from peers over the SUB; the node's own one never does (a
    SUB does not hear its own PUB) and is delivered locally by ``publish``.
    The STATUS count derives from this function and nowhere else. It is
    *not* the ORDER count: an IDLE peer sends no ORDER, so expected ORDERs
    are derived from the non-IDLE STATUSes actually received.
    """
    from_peers = num_nodes - 1
    own = 1
    return from_peers + own


@dataclass
class CommStats:
    """Communication counters of one network (Architecture 4.3)."""

    handshake_ms: float | None = None
    handshake_rounds: int = 0
    # Peer handshake STATUSes that arrived after readiness; dropped, not
    # lost/late. A node's own local delivery never arrives, so never counts.
    handshake_leftovers: int = 0


class CommError(RuntimeError):
    """Base class for communication layer failures."""


class HandshakeTimeout(CommError):
    """Raised when the readiness handshake does not complete in time."""


class StepTimeout(CommError):
    """Raised when a step barrier cannot be completed in time."""


class PeerNode:
    """PUB + SUB + REP sockets for one agent, on a context owned by the network."""

    def __init__(self, node_id: int, ctx: zmq.Context, config: CommConfig) -> None:
        if config.transport not in TRANSPORTS:
            raise ValueError(
                f"unknown transport: {config.transport!r} (expected one of {TRANSPORTS})"
            )
        self.node_id = node_id
        self.config = config
        self._closed = False
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
        self._poller = zmq.Poller()
        self._poller.register(self.sub, zmq.POLLIN)
        # Readiness handshake state: ids of peers whose PUB reached this SUB.
        self.seen_peers: set[int] = set()
        self.ready = False
        self.handshake_leftovers = 0
        # inbox[step] = {"status": {agent_id: StatusMessage}, "orders": [OrderMessage]}
        self.inbox: dict[int, dict] = {}

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

    def connect_peers(self, pub_endpoints: Iterable[str]) -> None:
        """Connect the SUB to every given peer PUB (never to its own)."""
        if self._closed or self.sub is None:
            raise CommError(f"node {self.node_id} is closed")
        for endpoint in pub_endpoints:
            if endpoint == self.pub_endpoint:
                continue
            self.sub.connect(endpoint)
            self.connected_peers.append(endpoint)

    def publish(self, msg: StatusMessage | OrderMessage) -> None:
        """Broadcast ``msg`` to all peers and deliver it to this node too."""
        if self._closed or self.pub is None:
            raise CommError(f"node {self.node_id} is closed")
        self.pub.send_multipart(
            [msg.topic.encode("ascii"), encode_message(msg).encode("utf-8")]
        )
        self._deliver(msg)

    def publish_handshake(self) -> None:
        """Broadcast one readiness STATUS; its payload is meaningless."""
        self.publish(
            make_status_message(
                agent_id=self.node_id,
                step=HANDSHAKE_STEP,
                generation_kwh=0.0,
                consumption_kwh=0.0,
                residual_kwh=0.0,
                state="IDLE",
            )
        )

    def drain(self, timeout_ms: int = 0) -> int:
        """Read every message waiting on the SUB; returns how many were read.

        Waits at most ``timeout_ms`` for the first one, then reads without
        blocking, so a silent peer can never stall the single thread.
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
            self._deliver(decode_message(frames[-1].decode("utf-8")))
            count += 1

    def _deliver(self, msg: Message) -> None:
        if msg.step == HANDSHAKE_STEP:
            if self.ready:
                if msg.agent_id != self.node_id:
                    self.handshake_leftovers += 1
            elif isinstance(msg, StatusMessage) and msg.agent_id != self.node_id:
                self.seen_peers.add(msg.agent_id)
            return
        box = self.inbox.setdefault(msg.step, {"status": {}, "orders": []})
        if isinstance(msg, StatusMessage):
            box["status"][msg.agent_id] = msg
        elif isinstance(msg, OrderMessage):
            box["orders"].append(msg)

    @property
    def closed(self) -> bool:
        return self._closed

    def sockets(self) -> list[zmq.Socket]:
        return [s for s in (self.pub, self.sub, self.rep) if s is not None]

    def close(self) -> None:
        """Close all sockets; safe to call more than once."""
        if self._closed:
            return
        for sock in self.sockets():
            sock.close(linger=0)
        self._closed = True
