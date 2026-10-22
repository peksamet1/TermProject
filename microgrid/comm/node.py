"""One agent's ZeroMQ endpoint: persistent PUB, SUB and REP sockets.

Phase 3, step 1 covers only the socket lifecycle; no messages flow yet.
Sockets are split by pattern (PSD 5.1, only PUB-SUB and REQ-REP):
- PUB: this node's STATUS / ORDER broadcasts, multipart ``[topic, json]``.
- SUB: connected to every other node's PUB. A SUB never hears its own PUB,
  so later steps put a node's own messages straight into its own inbox.
- REP: answers requests from peers. REQ sockets are per request and come
  in a later step.

Lifecycle is split in two on purpose: the constructor binds PUB and REP,
``connect_peers`` connects the SUB. ``PeerNetwork`` constructs *all* nodes
before connecting any of them, because inproc does not guarantee
connect-before-bind and over tcp an early connect only produces retries.

Every socket gets ``LINGER=0`` so that closing never waits on unsent
messages; a lingering socket is what makes ``ctx.term()`` hang a test run.
"""

from __future__ import annotations

from typing import Iterable

import zmq

from microgrid.config import CommConfig

# ZeroMQ topic matching is a *prefix* match: subscribing to b"status" also
# delivers b"status2", b"statusX", ... Topics are therefore kept to exactly
# these two names; adding a topic that starts with either one would leak it
# into the inbox of every subscriber.
TOPIC_STATUS = b"status"
TOPIC_ORDER = b"order"
SUBSCRIBED_TOPICS: tuple[bytes, ...] = (TOPIC_STATUS, TOPIC_ORDER)

TRANSPORTS = ("tcp", "inproc")


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
