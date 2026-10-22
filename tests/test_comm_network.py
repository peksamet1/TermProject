"""PeerNetwork readiness handshake (Phase 3, step 2): the slow joiner fix.

Waits in these tests are bounded polls on a deadline, never fixed sleeps:
a test that sleeps "long enough" hides exactly the loss it should catch.
"""

import time

import pytest

from microgrid.comm.network import PeerNetwork
from microgrid.comm.node import (
    HANDSHAKE_STEP,
    CommError,
    HandshakeTimeout,
    expected_per_step,
)
from microgrid.comm.protocol import make_order_message, make_status_message
from microgrid.config import CommConfig
from microgrid.market.base import Order, OrderSide

DELIVERY_TIMEOUT_S = 2.0


@pytest.fixture(params=["tcp", "inproc"])
def comm_config(request) -> CommConfig:
    return CommConfig(transport=request.param)


def _drain_until(net: PeerNetwork, done, timeout_s: float = DELIVERY_TIMEOUT_S) -> bool:
    """Drain every node until ``done()`` holds or the deadline passes."""
    deadline = time.perf_counter() + timeout_s
    while not done():
        if time.perf_counter() >= deadline:
            return False
        for node in net.nodes.values():
            node.drain(net.config.poll_interval_ms)
    return True


def _order(node_id: int, step: int = 0):
    side = OrderSide.SELL if node_id % 2 else OrderSide.BUY
    # seq is a placeholder here: the real one is the global send order and is
    # assigned by ZmqMarket.submit in Phase 4, never by the node.
    return make_order_message(Order(node_id, side, 1.0, 2.0, step), seq=node_id)


def test_expected_per_step_counts_peers_plus_own():
    assert expected_per_step(1) == 1
    assert expected_per_step(3) == 3
    assert expected_per_step(50) == 50


def test_three_node_handshake_completes(comm_config):
    with PeerNetwork([0, 1, 2], comm_config) as net:
        assert not net.ready
        net.handshake()
        assert net.ready
        for node_id, node in net.nodes.items():
            assert node.ready
            assert node.seen_peers == {0, 1, 2} - {node_id}
        assert net.unheard() == {}
        stats = net.stats()
        assert stats.handshake_rounds >= 1
        assert 0 < stats.handshake_ms < 1000 * comm_config.handshake_timeout_s


@pytest.mark.parametrize("num_nodes", [3, 10])
@pytest.mark.parametrize("repeat", range(3))
def test_first_message_after_handshake_is_not_lost(comm_config, num_nodes, repeat):
    # Regression test for C3 (slow joiner): every node publishes an ORDER the
    # instant handshake() returns, with no wait in between, and every node
    # must receive every one of them. Without the handshake this message is
    # the one a freshly connected SUB drops.
    # Only the tcp arm discriminates: inproc shows no slow joiner (measured).
    with PeerNetwork(range(num_nodes), comm_config) as net:
        net.handshake()
        for node_id, node in net.nodes.items():
            node.publish(_order(node_id))

        # Every node published exactly one ORDER. Not expected_per_step():
        # that is the STATUS count, and ORDERs follow non-IDLE STATUSes.
        want = num_nodes

        def all_received():
            return all(
                len(n.inbox.get(0, {}).get("orders", [])) == want for n in net.nodes.values()
            )

        assert _drain_until(net, all_received), {
            node_id: len(n.inbox.get(0, {}).get("orders", [])) for node_id, n in net.nodes.items()
        }
        for node in net.nodes.values():
            senders = sorted(m.agent_id for m in node.inbox[0]["orders"])
            assert senders == list(range(num_nodes))


def test_silent_node_times_out_and_is_named(comm_config):
    comm_config.handshake_timeout_s = 0.2
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net[2].publish_handshake = lambda: None  # node 2 never announces itself
        started = time.perf_counter()
        with pytest.raises(HandshakeTimeout) as excinfo:
            net.handshake()
        assert time.perf_counter() - started < 2.0  # bounded, does not hang
        message = str(excinfo.value)
        assert "node 0 has not heard [2]" in message
        assert "node 1 has not heard [2]" in message
        assert "node 2" not in message.split(":", 1)[1]  # node 2 heard everyone
        assert net.unheard() == {0: [2], 1: [2]}
        assert not net.ready


def test_handshake_traffic_never_enters_step_inboxes(comm_config):
    with PeerNetwork(range(4), comm_config) as net:
        net.handshake()
        # A leftover handshake STATUS arriving after readiness: forced here so
        # the drop path is exercised deterministically. Real leftovers from
        # the last round may arrive too, so waiting on the aggregate counter
        # would not prove this one arrived; track it by msg_id instead.
        forced = make_status_message(
            agent_id=1,
            step=HANDSHAKE_STEP,
            generation_kwh=0.0,
            consumption_kwh=0.0,
            residual_kwh=0.0,
            state="IDLE",
            msg_id="forced-leftover",
        )
        arrived: set[int] = set()
        for node_id, node in net.nodes.items():

            def spy(msg, node_id=node_id, deliver=node._deliver):
                if msg.msg_id == forced.msg_id:
                    arrived.add(node_id)
                deliver(msg)

            node._deliver = spy
        before = {i: n.handshake_leftovers for i, n in net.nodes.items()}
        net[1].publish(forced)
        # Own local delivery is synchronous: it reaches _deliver but must not
        # count as a leftover.
        assert 1 in arrived and net[1].handshake_leftovers == before[1]
        assert _drain_until(net, lambda: arrived == {0, 1, 2, 3})
        for node_id in (0, 2, 3):
            assert net[node_id].handshake_leftovers >= before[node_id] + 1
        for node in net.nodes.values():
            assert HANDSHAKE_STEP not in node.inbox
            assert node.inbox == {}
        # Dropped leftovers do not touch readiness state.
        assert all(n.seen_peers == set(range(4)) - {i} for i, n in net.nodes.items())


def test_ten_node_handshake_finishes_within_timeout(comm_config):
    with PeerNetwork(range(10), comm_config) as net:
        net.handshake()
        assert net.unheard() == {}
        assert net.stats().handshake_ms < 1000 * comm_config.handshake_timeout_s


def test_handshake_is_idempotent_and_refuses_closed_network(comm_config):
    net = PeerNetwork([0, 1], comm_config)
    net.handshake()
    rounds = net.stats().handshake_rounds
    net.handshake()
    assert net.stats().handshake_rounds == rounds
    net.close()
    with pytest.raises(CommError, match="closed"):
        net.handshake()
