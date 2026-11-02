"""Full-mesh network of PeerNodes on one ZeroMQ context.

Setup order is fixed:
1. raise OS / ZeroMQ resource limits (a full tcp mesh needs ~N^2 fds;
   the macOS default soft limit is 256),
2. construct every node, which binds every PUB and REP,
3. only then connect every SUB to all other PUBs (inproc does not
   guarantee connect-before-bind).

``handshake()`` then makes the mesh usable. A SUB that has just connected
misses whatever a PUB sends before the connection is up ("slow joiner"; in
the prototype 100% of the first messages were lost). A fixed sleep either
falls short (loss) or wastes time (skewed measurements), so every node
re-broadcasts a handshake STATUS each ``handshake_interval_ms`` until every
node has heard every other node. Being one process, the network checks this
globally; a node counting only what it heard itself would not know whether
its own PUB reaches the others.

``await_step(step)`` is the step barrier (Architecture 4.3). Every node waits
for a STATUS from each peer and an ORDER from each peer whose STATUS is not
IDLE; one poll over all SUBs serves every node, bounded by
``step_timeout_s``. Whatever is still missing then is counted as lost and
the step is marked degraded: the barrier never waits longer and never
raises, because a node that missed an ORDER only diverges to the safe side
(its trades get NACKed and fall back to the grid, Architecture 4.4). All
nodes close the step together, after which ``orders_at`` / ``statuses_at``
are readable on every node until the next step closes.

``negotiation_rounds(requests, handler)`` is the request / reply drive
(Architecture 4.4, item 3), in three rounds because one thread cannot wait
on a REQ while also serving the target's REP: (1) every requester sends,
(2) every target drains its REP until it has answered everything addressed
to it, (3) every requester collects. Rounds 2 and 3 share one
``request_timeout_s`` deadline; a request still unanswered then comes back
as ``None`` and counts as a timeout. It is written in terms of requester and
target only; Phase 4 makes the body a TRADE and the reply a TRADE or NACK.

Teardown closes every node and then calls ``ctx.destroy(linger=0)``.
``ctx.term()`` is never used directly: it blocks forever while any socket
is still open, which is the number one cause of a hanging pytest run.
"""

from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, replace
from functools import partial
from types import TracebackType
from typing import Callable, Iterable

import zmq

from microgrid.comm.node import (
    NODE_COUNTERS,
    NODE_SAMPLES,
    CommError,
    CommStats,
    HandshakeTimeout,
    PeerNode,
    PendingRequest,
    StepResult,
    default_error_reply,
)
from microgrid.config import CommConfig

# Soft RLIMIT_NOFILE target when the hard limit is unlimited or larger.
FD_LIMIT_TARGET = 65536
# Persistent sockets per node: PUB, SUB, REP.
SOCKETS_PER_NODE = 3


def max_sockets_needed(num_nodes: int) -> int:
    """Persistent sockets plus the worst-case REQs open at once.

    REQs are pooled per (requester, target), so the steady state is one per
    pair that has ever talked: at most N * (N - 1), reached when every node
    asks every peer. It is doubled for REQs closed after a timeout, which
    keep their slot until libzmq has reaped them, and for a second request
    to the same target in one round. libzmq's default MAX_SOCKETS (1023) is
    below this from N = 23 on.
    """
    return SOCKETS_PER_NODE * num_nodes + 2 * num_nodes * (num_nodes - 1) + 16


def raise_fd_limit(target: int = FD_LIMIT_TARGET) -> int | None:
    """Raise the soft open-file limit towards ``target`` (never lowers it).

    Returns the resulting soft limit, or ``None`` where the ``resource``
    module does not exist (Windows) or the OS refuses the change; both cases
    only warn, so the network still comes up for small N.
    """
    try:
        import resource
    except ImportError:
        warnings.warn(
            "resource module unavailable (Windows?): open-file limit not raised; "
            "large tcp meshes may need transport='inproc'",
            RuntimeWarning,
            stacklevel=2,
        )
        return None

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = target if hard == resource.RLIM_INFINITY else min(hard, target)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (ValueError, OSError) as exc:
            warnings.warn(
                f"could not raise open-file limit from {soft} to {wanted}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return soft
        return wanted
    return soft


@dataclass(frozen=True)
class StepReport:
    """One ``await_step``: every node's barrier, all closed together."""

    step: int
    barrier_ms: float
    nodes: dict[int, StepResult]

    @property
    def status_loss(self) -> int:
        return sum(r.status_loss for r in self.nodes.values())

    @property
    def order_loss(self) -> int:
        return sum(r.order_loss for r in self.nodes.values())

    @property
    def degraded(self) -> bool:
        return any(r.degraded for r in self.nodes.values())


@dataclass(frozen=True)
class RequestResult:
    """One request of ``negotiation_rounds``; ``reply is None`` means timed out."""

    requester: int
    target: int
    body: str
    reply: str | None
    rtt_ms: float | None

    @property
    def answered(self) -> bool:
        return self.reply is not None


class PeerNetwork:
    """Builds, owns and tears down one PeerNode per agent id."""

    def __init__(self, node_ids: Iterable[int], config: CommConfig | None = None) -> None:
        self.config = config if config is not None else CommConfig()
        ids = list(node_ids)
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate node ids: {ids}")
        self._closed = False
        self._ready = False
        self._stats = CommStats()
        self.nodes: dict[int, PeerNode] = {}

        if self.config.raise_fd_limit:
            raise_fd_limit()
        self.ctx = zmq.Context(io_threads=self.config.io_threads)
        try:
            # MAX_SOCKETS must be set before the first socket is created.
            wanted = max_sockets_needed(len(ids))
            if self.ctx.get(zmq.MAX_SOCKETS) < wanted:
                self.ctx.set(zmq.MAX_SOCKETS, wanted)
            # Bind every PUB / REP first ...
            for node_id in ids:
                self.nodes[node_id] = PeerNode(node_id, self.ctx, self.config)
            # ... and only then connect the SUBs.
            pub_endpoints = {node_id: node.pub_endpoint for node_id, node in self.nodes.items()}
            rep_endpoints = {node_id: node.rep_endpoint for node_id, node in self.nodes.items()}
            for node in self.nodes.values():
                node.connect_peers(pub_endpoints, rep_endpoints)
            # One poller over every SUB: a wait wakes on the first arrival
            # at any node instead of sleeping a fixed time. Same for the REPs.
            self._poller = zmq.Poller()
            self._node_of_sub: dict[zmq.Socket, PeerNode] = {}
            self._rep_poller = zmq.Poller()
            self._node_of_rep: dict[zmq.Socket, PeerNode] = {}
            for node in self.nodes.values():
                self._poller.register(node.sub, zmq.POLLIN)
                self._node_of_sub[node.sub] = node
                self._rep_poller.register(node.rep, zmq.POLLIN)
                self._node_of_rep[node.rep] = node
        except Exception:
            self.close()
            raise

    def __len__(self) -> int:
        return len(self.nodes)

    def __getitem__(self, node_id: int) -> PeerNode:
        return self.nodes[node_id]

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ready(self) -> bool:
        return self._ready

    def stats(self) -> CommStats:
        """Snapshot: the network-wide counters plus every node's, summed.

        Sample lists are concatenated over nodes, so the snapshot's
        ``percentile_of`` / ``summary()`` give network-wide p50 / p95.
        """
        total = replace(
            self._stats,
            barrier_ms=list(self._stats.barrier_ms),
            **{name: [] for name in NODE_SAMPLES},
        )
        for node in self.nodes.values():
            for name in NODE_COUNTERS:
                setattr(total, name, getattr(total, name) + getattr(node.stats, name))
            for name in NODE_SAMPLES:
                getattr(total, name).extend(getattr(node.stats, name))
        return total

    def unheard(self) -> dict[int, list[int]]:
        """Per node, the peers it has not heard a handshake STATUS from."""
        ids = set(self.nodes)
        out: dict[int, list[int]] = {}
        for node_id, node in self.nodes.items():
            missing = sorted(ids - {node_id} - node.seen_peers)
            if missing:
                out[node_id] = missing
        return out

    def handshake(self) -> None:
        """Block until every node has heard every other node, or time out.

        Each round every node publishes one handshake STATUS; between rounds
        the loop waits on all SUBs at once, so it wakes as soon as anything
        arrives instead of sleeping a fixed time. Calling it again after
        success is a no-op.
        """
        if self._closed:
            raise CommError("network is closed")
        if self._ready:
            return
        interval = self.config.handshake_interval_ms / 1000.0
        start = time.perf_counter()
        deadline = start + self.config.handshake_timeout_s
        next_round = start
        rounds = 0
        while True:
            now = time.perf_counter()
            if now >= next_round:
                for node in self.nodes.values():
                    node.publish_handshake()
                rounds += 1
                next_round = now + interval
            for node in self.nodes.values():
                node.drain(0)
            unheard = self.unheard()
            if not unheard:
                break
            now = time.perf_counter()
            if now >= deadline:
                self._stats.handshake_rounds = rounds
                raise HandshakeTimeout(self._timeout_message(unheard, rounds))
            wait_ms = math.ceil(1000.0 * (min(next_round, deadline) - now))
            self._poller.poll(max(wait_ms, 0))

        self._stats.handshake_ms = 1000.0 * (time.perf_counter() - start)
        self._stats.handshake_rounds = rounds
        for node in self.nodes.values():
            node.ready = True
        self._ready = True

    def await_step(self, step: int) -> StepReport:
        """Step barrier: wait until every node holds all of ``step``, then close it.

        Returns within ``step_timeout_s`` whatever happens; a node that is
        still missing something then closes degraded and the loss is counted
        (see the module docstring). Steps close in increasing order.
        """
        if self._closed:
            raise CommError("network is closed")
        if not self._ready:
            raise CommError("await_step() needs a completed handshake()")
        if any(step <= node.last_closed_step for node in self.nodes.values()):
            raise CommError(f"step {step} is already closed")

        start = time.perf_counter()
        deadline = start + self.config.step_timeout_s
        for node in self.nodes.values():
            node.drain(0)
        waiting = {i for i, node in self.nodes.items() if not node.barrier_state(step).complete}
        while waiting:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            ready = self._poller.poll(math.ceil(1000.0 * remaining))
            # Drain every SUB the poll reported, finished node or not: one left
            # holding data (say, step + 1 traffic) would make every later poll
            # return at once. SUBs it did not report hold nothing to read.
            arrived = {
                node.node_id
                for node in (self._node_of_sub[sock] for sock, _ in ready)
                if node.drain(0)
            }
            waiting -= {i for i in waiting & arrived if self.nodes[i].barrier_state(step).complete}
        barrier_ms = 1000.0 * (time.perf_counter() - start)

        report = StepReport(
            step=step,
            barrier_ms=barrier_ms,
            nodes={i: node.close_step(step) for i, node in self.nodes.items()},
        )
        self._stats.steps += 1
        self._stats.barrier_ms.append(barrier_ms)
        if report.degraded:
            self._stats.degraded_steps += 1
        if report.status_loss:
            self._stats.status_loss_steps += 1
        return report

    def negotiation_rounds(
        self,
        requests: Iterable[tuple[int, int, str]],
        handler: Callable[[int, str], str],
        timeout_s: float | None = None,
        error_reply: Callable[[str, Exception], str] = default_error_reply,
    ) -> list[RequestResult]:
        """Run ``(requester, target, body)`` requests in three rounds.

        ``handler(target_id, body)`` builds each reply on the target. The
        results come back in input order, ``reply`` being ``None`` for a
        request that timed out. Returns within ``timeout_s`` (default
        ``request_timeout_s``) after round 1 whatever happens, and leaves no
        request in flight.
        """
        if self._closed:
            raise CommError("network is closed")
        if timeout_s is None:
            timeout_s = self.config.request_timeout_s
        triples = list(requests)
        for requester, target, _ in triples:
            if requester not in self.nodes or target not in self.nodes:
                raise ValueError(f"unknown node in request {requester} -> {target}")

        pending: list[PendingRequest] = []
        try:
            # Round 1: every requester sends; nothing waits yet.
            for requester, target, body in triples:
                pending.append(self.nodes[requester].send_request(target, body))
            deadline = time.perf_counter() + timeout_s

            def serve(node: PeerNode) -> int:
                return node.serve_requests(partial(handler, node.node_id), 0, error_reply)

            # Round 2: every target answers everything addressed to it. The
            # network knows how many that is; a target cannot tell "nothing
            # more is coming" from "not arrived yet".
            due: dict[int, int] = {}
            for p in pending:
                due[p.peer_id] = due.get(p.peer_id, 0) + 1
            while due:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                for sock, _ in self._rep_poller.poll(math.ceil(1000.0 * remaining)):
                    # Drain even a REP that owes nothing: an earlier round's
                    # leftover would otherwise keep every poll returning at once.
                    node = self._node_of_rep[sock]
                    left = due.get(node.node_id, 0) - serve(node)
                    if left <= 0:
                        due.pop(node.node_id, None)
                    else:
                        due[node.node_id] = left

            # Round 3: every requester collects; one poll over every open REQ.
            # The count of round 2 can be fooled: a request that timed out in an
            # earlier round may reach its target only now and be counted in place
            # of one of this round's, which is then still queued. The REPs of
            # targets with a reply outstanding are therefore polled here as well.
            waiting = {p.socket: p for p in pending if p.socket is not None}
            while waiting:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                poller = zmq.Poller()
                for sock, p in waiting.items():
                    poller.register(sock, zmq.POLLIN)
                    poller.register(self.nodes[p.peer_id].rep, zmq.POLLIN)
                for sock, _ in poller.poll(math.ceil(1000.0 * remaining)):
                    if sock in self._node_of_rep:
                        serve(self._node_of_rep[sock])
                    else:
                        p = waiting.pop(sock)
                        self.nodes[p.node_id].collect_reply(p, 0)
        finally:
            # Whatever ended the rounds (deadline, or an exception from a
            # send or a REP), no request stays in flight: an unanswered one
            # is finished as a timeout and its REQ closed.
            for p in pending:
                if not p.done:
                    self.nodes[p.node_id].collect_reply(p, 0)
            # A closed (timed-out) REQ is only reaped once the target's REP has
            # processed the pipe teardown, and a REP processes commands only when
            # it is touched. Before REQs were pooled, skipping this poll made
            # every REQ of a round hold its socket slot until the next round, and
            # at N = 50 with everyone asking everyone that round failed with
            # EMFILE (MAX_SOCKETS); one-off in-session observation (macOS,
            # ad-hoc script not in the repo). Step 5 did not re-measure this
            # one: its experiment always runs with this poll, and N = 50
            # per-request rounds with it ended with no error (see node.py).
            self._rep_poller.poll(0)

        return [
            RequestResult(p.node_id, p.peer_id, p.body, p.reply, p.rtt_ms) for p in pending
        ]

    def _timeout_message(self, unheard: dict[int, list[int]], rounds: int) -> str:
        detail = "; ".join(
            f"node {node_id} has not heard {missing}" for node_id, missing in unheard.items()
        )
        return (
            f"readiness handshake timed out after {self.config.handshake_timeout_s} s "
            f"({rounds} rounds, {len(self.nodes)} nodes): {detail}"
        )

    def close(self) -> None:
        """Close every node, then destroy the context; safe to call twice."""
        if self._closed:
            return
        for node in self.nodes.values():
            node.close()
        # destroy() also closes any socket a node failed to track before
        # terminating, so it cannot block the way a bare term() can.
        self.ctx.destroy(linger=0)
        self._closed = True

    def __enter__(self) -> PeerNetwork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
