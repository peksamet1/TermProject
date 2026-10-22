"""Full-mesh network of PeerNodes on one ZeroMQ context.

Phase 3, step 1: setup and teardown only. Setup order is fixed:
1. raise OS / ZeroMQ resource limits (a full tcp mesh needs ~N^2 fds;
   the macOS default soft limit is 256),
2. construct every node, which binds every PUB and REP,
3. only then connect every SUB to all other PUBs (inproc does not
   guarantee connect-before-bind).

Teardown closes every node and then calls ``ctx.destroy(linger=0)``.
``ctx.term()`` is never used directly: it blocks forever while any socket
is still open, which is the number one cause of a hanging pytest run.
"""

from __future__ import annotations

import warnings
from types import TracebackType
from typing import Iterable

import zmq

from microgrid.comm.node import PeerNode
from microgrid.config import CommConfig

# Soft RLIMIT_NOFILE target when the hard limit is unlimited or larger.
FD_LIMIT_TARGET = 65536
# Sockets per node: PUB, SUB, REP, plus one transient REQ per request.
SOCKETS_PER_NODE = 4


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


class PeerNetwork:
    """Builds, owns and tears down one PeerNode per agent id."""

    def __init__(self, node_ids: Iterable[int], config: CommConfig | None = None) -> None:
        self.config = config if config is not None else CommConfig()
        ids = list(node_ids)
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate node ids: {ids}")
        self._closed = False
        self.nodes: dict[int, PeerNode] = {}

        if self.config.raise_fd_limit:
            raise_fd_limit()
        self.ctx = zmq.Context(io_threads=self.config.io_threads)
        try:
            # MAX_SOCKETS must be set before the first socket is created.
            wanted = SOCKETS_PER_NODE * len(ids) + 16
            if self.ctx.get(zmq.MAX_SOCKETS) < wanted:
                self.ctx.set(zmq.MAX_SOCKETS, wanted)
            # Bind every PUB / REP first ...
            for node_id in ids:
                self.nodes[node_id] = PeerNode(node_id, self.ctx, self.config)
            # ... and only then connect the SUBs.
            pub_endpoints = [node.pub_endpoint for node in self.nodes.values()]
            for node in self.nodes.values():
                node.connect_peers(pub_endpoints)
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
