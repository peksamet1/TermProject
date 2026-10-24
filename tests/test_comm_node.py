"""PeerNode / PeerNetwork socket lifecycle (Phase 3, step 1): no message flow.

Every test closes what it opens; a leaked socket would make ``ctx.term()``
block and hang the whole suite, which is exactly what these tests guard.
"""

import os
import sys

import pytest
import zmq

from microgrid.comm.network import FD_LIMIT_TARGET, PeerNetwork, raise_fd_limit
from microgrid.comm.node import PeerNode, SUBSCRIBED_TOPICS
from microgrid.config import CommConfig, SimulationConfig


@pytest.fixture(params=["tcp", "inproc"])
def comm_config(request) -> CommConfig:
    return CommConfig(transport=request.param)


def _open_fds() -> int | None:
    """Open file descriptors of this process, where the OS exposes them."""
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return None


def _fd_baseline(config: CommConfig) -> int | None:
    """fd count after a warm-up network has been opened and closed.

    libzmq opens /dev/urandom once per process and keeps it open. Without
    the warm-up, a leak test that happens to be the first zmq user in the
    process (``pytest -k``, ``--lf``) would report that fd as a false leak.
    """
    with PeerNetwork([0, 1], config):
        pass
    return _open_fds()


def test_simulation_config_carries_comm_defaults():
    comm = SimulationConfig().comm
    assert comm.transport == "tcp"
    assert comm.bind_host == "127.0.0.1"
    assert 0 < comm.sndhwm < 10**9 and 0 < comm.rcvhwm < 10**9  # finite HWM
    assert SimulationConfig().comm is not comm  # no shared mutable default


def test_three_node_network_opens_and_closes(comm_config):
    net = PeerNetwork([0, 1, 2], comm_config)
    assert len(net) == 3
    for node in net.nodes.values():
        assert len(node.connected_peers) == 2
        assert node.pub_endpoint not in node.connected_peers
        for sock in node.sockets():
            assert sock.getsockopt(zmq.LINGER) == 0
            assert sock.getsockopt(zmq.SNDHWM) == comm_config.sndhwm
            assert sock.getsockopt(zmq.RCVHWM) == comm_config.rcvhwm
    net.close()
    assert net.closed and net.ctx.closed


def test_close_is_idempotent(comm_config):
    net = PeerNetwork([0, 1, 2], comm_config)
    net.close()
    net.close()
    node = net[0]
    node.close()
    assert node.closed


def test_context_manager_leaves_no_open_sockets(comm_config):
    fds_before = _fd_baseline(comm_config)
    # Several rounds: a per-network leak would make the count grow.
    for _ in range(3):
        with PeerNetwork(range(5), comm_config) as net:
            # Close after the handshake, as every real run does. Closed while
            # its SUB connects are still in flight, libzmq 4.3.5 itself now and
            # then keeps one tcp fd (state CLOSED) for good: about 1 in 300
            # immediate open/close rounds, reproduced with pyzmq alone, none of
            # this code (one-off, macOS arm64, script not in the repo). That
            # is not a leak of this layer, so this test does not count it.
            net.handshake()
            sockets = [s for node in net.nodes.values() for s in node.sockets()]
            assert len(sockets) == 15
        assert all(s.closed for s in sockets)
        assert net.ctx.closed
        if fds_before is not None:
            assert _open_fds() == fds_before


def test_close_destroys_untracked_sockets(comm_config):
    # A socket the nodes do not track (e.g. a per-request REQ left open on an
    # error path) must still be closed by close(); with a bare ctx.term()
    # instead of destroy() this test would hang rather than pass.
    net = PeerNetwork([0, 1], comm_config)
    stray = net.ctx.socket(zmq.REQ)
    stray.connect(net[1].rep_endpoint)
    net.close()
    assert stray.closed
    assert net.ctx.closed


def test_close_runs_when_block_raises(comm_config):
    with pytest.raises(RuntimeError, match="boom"):
        with PeerNetwork([0, 1], comm_config) as net:
            raise RuntimeError("boom")
    assert net.closed and net.ctx.closed


def test_endpoints_are_distinct(comm_config):
    with PeerNetwork(range(4), comm_config) as net:
        endpoints = [e for n in net.nodes.values() for e in (n.pub_endpoint, n.rep_endpoint)]
        assert len(set(endpoints)) == len(endpoints) == 8
        prefix = "inproc://" if comm_config.transport == "inproc" else "tcp://127.0.0.1:"
        assert all(e.startswith(prefix) for e in endpoints)


def test_two_networks_coexist(comm_config):
    # Same node ids in both: random tcp ports and per-network contexts
    # (inproc names are context-local) keep them apart.
    with PeerNetwork([0, 1, 2], comm_config) as a, PeerNetwork([0, 1, 2], comm_config) as b:
        assert a.ctx is not b.ctx
        if comm_config.transport == "tcp":
            ends_a = {n.pub_endpoint for n in a.nodes.values()}
            ends_b = {n.pub_endpoint for n in b.nodes.values()}
            assert not ends_a & ends_b


def test_thirty_node_network_opens_and_closes(comm_config):
    with PeerNetwork(range(30), comm_config) as net:
        assert len(net) == 30
        assert all(len(n.connected_peers) == 29 for n in net.nodes.values())
    assert net.ctx.closed


def test_sub_topics_are_status_and_order_only():
    assert SUBSCRIBED_TOPICS == (b"status", b"order")


def test_invalid_input_is_rejected_without_leaking():
    with pytest.raises(ValueError, match="duplicate"):
        PeerNetwork([1, 1])
    ctx = zmq.Context()
    try:
        with pytest.raises(ValueError, match="transport"):
            PeerNode(0, ctx, CommConfig(transport="ipc"))
    finally:
        ctx.destroy(linger=0)


def test_failed_bind_closes_everything():
    # 192.0.2.1 (TEST-NET-1) is not a local address, so bind fails: setup
    # must close what it opened instead of leaving a half-open context.
    fds_before = _fd_baseline(CommConfig())
    with pytest.raises(zmq.ZMQError):
        PeerNetwork([0, 1], CommConfig(bind_host="192.0.2.1"))
    if fds_before is not None:
        assert _open_fds() == fds_before


def test_raise_fd_limit_warns_without_resource_module(monkeypatch):
    # Simulates Windows, where the resource module does not exist.
    monkeypatch.setitem(sys.modules, "resource", None)
    with pytest.warns(RuntimeWarning, match="resource module unavailable"):
        assert raise_fd_limit() is None


@pytest.mark.skipif(sys.platform == "win32", reason="resource is POSIX-only")
def test_raise_fd_limit_never_lowers_soft_limit():
    import resource

    soft_before, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    raise_fd_limit(target=16)
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == soft_before


@pytest.mark.skipif(sys.platform == "win32", reason="resource is POSIX-only")
def test_raise_fd_limit_lifts_low_soft_limit():
    # A fresh macOS terminal starts at 256, too few for a full tcp mesh.
    import resource

    soft_before, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = (
        FD_LIMIT_TARGET if hard == resource.RLIM_INFINITY else min(hard, FD_LIMIT_TARGET)
    )
    if wanted <= 256:
        pytest.skip(f"hard limit {hard} leaves nothing to raise above 256")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        assert raise_fd_limit() == wanted
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == wanted
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_before, hard))
