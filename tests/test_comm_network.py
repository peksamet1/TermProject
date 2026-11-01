"""PeerNetwork readiness handshake (Phase 3, step 2) and step barrier (step 3).

Waits in these tests are bounded polls on a deadline, never fixed sleeps:
a test that sleeps "long enough" hides exactly the loss it should catch.
"""

import json
import time

import numpy as np
import pytest

from microgrid.agents.household import HouseholdState
from microgrid.comm.network import PeerNetwork
from microgrid.comm.node import (
    HANDSHAKE_STEP,
    IDLE_STATE,
    CommError,
    HandshakeTimeout,
    expected_per_step,
)
from microgrid.comm.protocol import (
    encode_message,
    make_order_message,
    make_status_message,
    make_trade_message,
    message_to_order,
)
from microgrid.config import CommConfig
from microgrid.market.base import Order, OrderSide
from microgrid.market.cda import CDAMarket

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
        before = {i: n.stats.handshake_leftovers for i, n in net.nodes.items()}
        net[1].publish(forced)
        # Own local delivery is synchronous: it reaches _deliver but must not
        # count as a leftover.
        assert 1 in arrived and net[1].stats.handshake_leftovers == before[1]
        assert _drain_until(net, lambda: arrived == {0, 1, 2, 3})
        for node_id in (0, 2, 3):
            assert net[node_id].stats.handshake_leftovers >= before[node_id] + 1
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


# --- Step 3: PUB-SUB step traffic and the per-step barrier --------------------


def _status(agent_id: int, step: int, state: str):
    return make_status_message(
        agent_id=agent_id,
        step=step,
        generation_kwh=0.0,
        consumption_kwh=0.0,
        residual_kwh=0.0,
        state=state,
    )


def _order_msg(agent_id: int, step: int, side: OrderSide, seq: int | None):
    return make_order_message(Order(agent_id, side, 1.0, 2.0, step), seq=seq)


def _publish_step(net, step, states, activation=None) -> dict[int, int]:
    """Publish one step the way Phase 4 will; returns {agent_id: seq}.

    Agents act in ``activation`` order (default: id order). A non-IDLE agent
    publishes its ORDER mid-step and every agent its STATUS at the end of
    its step, IDLE included, so each sender's ORDER precedes its STATUS. seq
    is the step's global send counter; this helper keeps it in place of
    ZmqMarket.submit. Agents left out of ``activation`` stay silent.
    """
    seqs: dict[int, int] = {}
    for agent_id in activation if activation is not None else list(net.nodes):
        state = states[agent_id]
        if state != IDLE_STATE:
            side = OrderSide.BUY if state == "BUYING" else OrderSide.SELL
            seqs[agent_id] = len(seqs)
            net[agent_id].publish_order(_order_msg(agent_id, step, side, seqs[agent_id]))
        net[agent_id].publish_status(_status(agent_id, step, state))
    return seqs


def _send_raw(node, topic: bytes, payload: bytes) -> None:
    """Put frames on a node's PUB directly, past every publish() check."""
    node.pub.send_multipart([topic, payload])


def test_idle_state_is_the_household_wire_value():
    assert HouseholdState.IDLE.value == IDLE_STATE


def test_three_nodes_get_every_status_and_order_for_five_steps(comm_config):
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        for step in range(5):
            # A different node sits each step out as IDLE.
            states = {
                i: IDLE_STATE if i == step % 3 else ("SELLING" if i % 2 else "BUYING")
                for i in net.nodes
            }
            seqs = _publish_step(net, step, states)
            report = net.await_step(step)
            assert not report.degraded, report
            for node_id, node in net.nodes.items():
                statuses = node.statuses_at(step)
                assert list(statuses) == [0, 1, 2]
                assert len(statuses) == expected_per_step(3)
                assert {i: m.state for i, m in statuses.items()} == states
                # Carried, never rewritten: each ORDER keeps the seq it was sent with.
                assert {m.agent_id: m.seq for m in node.orders_at(step)} == seqs
        stats = net.stats()
        assert stats.steps == 5 and stats.degraded_steps == 0
        assert stats.status_expected == stats.status_received == 5 * 3 * 2
        # Per step: each of the two traders expects the other's ORDER, the
        # IDLE node expects both.
        assert stats.order_expected == stats.order_received == 5 * 4
        assert stats.late == stats.protocol_errors == 0


def test_orders_at_follows_seq_not_arrival_or_agent_id(comm_config):
    # Activation order 2, 1, 0 gives seq 0, 1, 2, but the nodes publish in id
    # order, so neither arrival order nor agent_id order matches seq.
    seq = {2: 0, 1: 1, 0: 2}
    side = {2: OrderSide.SELL, 1: OrderSide.SELL, 0: OrderSide.BUY}
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        for agent_id in (0, 1, 2):
            net[agent_id].publish_order(_order_msg(agent_id, 0, side[agent_id], seq[agent_id]))
            state = "SELLING" if side[agent_id] is OrderSide.SELL else "BUYING"
            net[agent_id].publish_status(_status(agent_id, 0, state))
        assert not net.await_step(0).degraded

        lists = {i: node.orders_at(0) for i, node in net.nodes.items()}
        for orders in lists.values():
            assert [(m.agent_id, m.seq) for m in orders] == [(2, 0), (1, 1), (0, 2)]
            assert [m.msg_id for m in orders] == [m.msg_id for m in lists[0]]  # C6

        # Each node feeds its own CDA copy, as Phase 4 will. Both asks are at
        # 2.0, so time priority decides: agent 2 (seq 0) must sell. Sorting
        # by agent_id, the symptom of a per-node seq counter, would pick 1.
        trades = {}
        for node_id, orders in lists.items():
            copy = CDAMarket()
            for m in orders:
                copy.submit(message_to_order(m))
            trades[node_id] = copy.trades
        assert trades[0] == trades[1] == trades[2]
        assert [(t.seller_id, t.buyer_id) for t in trades[0]] == [(2, 0)]


def test_idle_peer_is_not_waited_for(comm_config):
    # A long step timeout: if the barrier waited for an ORDER from the IDLE
    # node, barrier_ms would show it.
    comm_config.step_timeout_s = 5.0
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        _publish_step(net, 0, {0: "BUYING", 1: "SELLING", 2: IDLE_STATE})
        report = net.await_step(0)
        assert not report.degraded
        assert report.barrier_ms < 1000.0
        assert report.nodes[0].order_expected == 1  # node 1's only
        assert report.nodes[2].order_expected == 2
        for node in net.nodes.values():
            assert [m.agent_id for m in node.orders_at(0)] == [0, 1]

        # An all-IDLE step has no ORDER at all and still closes cleanly.
        _publish_step(net, 1, dict.fromkeys(net.nodes, IDLE_STATE))
        report = net.await_step(1)
        assert not report.degraded and report.barrier_ms < 1000.0
        assert all(node.orders_at(1) == [] for node in net.nodes.values())


def test_order_arriving_before_its_status_is_held(comm_config):
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        net[1].publish_order(_order_msg(1, 0, OrderSide.SELL, seq=0))
        assert _drain_until(
            net, lambda: all(len(net[i].inbox.get(0, {}).get("orders", [])) == 1 for i in (0, 2))
        )
        # The ORDER is held, but without its STATUS it is neither due nor missing.
        early = net[0].barrier_state(0)
        assert early.missing_status == (1, 2)
        assert early.order_expected == 0 and early.missing_orders == ()

        net[1].publish_status(_status(1, 0, "SELLING"))
        for i in (0, 2):
            net[i].publish_status(_status(i, 0, IDLE_STATE))
        report = net.await_step(0)
        assert not report.degraded
        assert report.nodes[0].order_expected == report.nodes[2].order_expected == 1
        for node in net.nodes.values():
            assert [m.agent_id for m in node.orders_at(0)] == [1]


def test_next_step_traffic_is_buffered_not_dropped(comm_config):
    states = {0: "BUYING", 1: "SELLING", 2: IDLE_STATE}
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        # Step 1 traffic is out and fully received while step 0 is still open.
        _publish_step(net, 1, states)
        assert _drain_until(net, lambda: all(n.barrier_state(1).complete for n in net.nodes.values()))
        _publish_step(net, 0, states)
        assert not net.await_step(0).degraded
        assert all(len(n.inbox[1]["status"]) == 3 for n in net.nodes.values())

        # Nothing new is published for step 1: the buffer alone completes it.
        report = net.await_step(1)
        assert not report.degraded
        for node in net.nodes.values():
            assert [m.agent_id for m in node.orders_at(1)] == [0, 1]
        stats = net.stats()
        assert stats.status_loss == stats.order_loss == stats.late == 0


def test_missing_peer_never_hangs_the_barrier(comm_config):
    comm_config.step_timeout_s = 0.2
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()

        # Step 0: node 2 sends nothing at all.
        _publish_step(net, 0, {0: "BUYING", 1: "SELLING"}, activation=[0, 1])
        started = time.perf_counter()
        report = net.await_step(0)
        assert comm_config.step_timeout_s <= time.perf_counter() - started < 1.0
        assert report.degraded
        assert report.nodes[0].missing_status == report.nodes[1].missing_status == (2,)
        assert report.nodes[2].complete  # node 2 itself heard both peers
        # Node 2's ORDER is unknown, not lost: no STATUS said one was coming.
        assert report.status_loss == 2 and report.order_loss == 0

        # Step 1: node 2's STATUS promises an ORDER that never comes.
        _publish_step(net, 1, {0: "BUYING", 1: "SELLING"}, activation=[0, 1])
        net[2].publish_status(_status(2, 1, "SELLING"))
        report = net.await_step(1)
        assert report.nodes[0].missing_orders == report.nodes[1].missing_orders == (2,)
        assert report.status_loss == 0 and report.order_loss == 2

        # Step 2: node 2's ORDER arrives, its STATUS does not. The ORDER is
        # kept for the CDA copies but not counted: it was never due.
        _publish_step(net, 2, {0: "BUYING", 1: "SELLING"}, activation=[0, 1])
        net[2].publish_order(_order_msg(2, 2, OrderSide.SELL, seq=2))
        assert _drain_until(
            net,
            lambda: all(
                any(m.agent_id == 2 for m in net[i].inbox.get(2, {}).get("orders", []))
                for i in (0, 1)
            ),
        )
        report = net.await_step(2)
        assert report.degraded and report.status_loss == 2 and report.order_loss == 0
        assert report.nodes[0].order_expected == 1
        assert [m.agent_id for m in net[0].orders_at(2)] == [0, 1, 2]

        # Step 3: everyone is back and the barrier is not left wedged.
        _publish_step(net, 3, {0: "BUYING", 1: "SELLING", 2: "SELLING"})
        assert not net.await_step(3).degraded
        stats = net.stats()
        assert stats.degraded_steps == 3
        assert stats.status_loss_steps == 2  # steps 0 and 2; step 1 lost only an ORDER
        assert stats.status_loss == 4 and stats.order_loss == 2


def test_message_after_close_is_late_and_never_injected(comm_config):
    comm_config.step_timeout_s = 0.2
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        _publish_step(net, 0, {0: "BUYING", 1: "SELLING"}, activation=[0, 1])
        assert net.await_step(0).degraded
        seen = {
            i: ([m.msg_id for m in n.orders_at(0)], list(n.statuses_at(0)))
            for i, n in net.nodes.items()
        }

        # Node 2's step-0 traffic finally arrives. publish() refuses a closed
        # step, so it goes on the wire directly, as a message stuck in flight would.
        for msg in (_order_msg(2, 0, OrderSide.SELL, seq=2), _status(2, 0, "SELLING")):
            _send_raw(net[2], msg.topic.encode(), encode_message(msg).encode())
        assert _drain_until(net, lambda: net[0].stats.late == net[1].stats.late == 2)
        for i, n in net.nodes.items():
            assert ([m.msg_id for m in n.orders_at(0)], list(n.statuses_at(0))) == seen[i]
        assert net.stats().late == 4

        # Late traffic does not leak into the next step either.
        _publish_step(net, 1, {0: "BUYING", 1: "SELLING", 2: "SELLING"})
        assert not net.await_step(1).degraded
        assert [m.agent_id for m in net[0].orders_at(1)] == [0, 1, 2]


def _malformed(kind: str) -> tuple[bytes, bytes]:
    """[topic, payload] of a message node 1 should never have sent."""

    def wire(msg, **changes) -> bytes:
        payload = json.loads(encode_message(msg))
        payload.update(changes)
        return json.dumps(payload).encode()

    status = _status(1, 0, "SELLING")
    order = _order_msg(1, 0, OrderSide.SELL, seq=0)
    trade = make_trade_message(
        buyer_id=0, seller_id=1, quantity_kwh=1.0, price_tl_per_kwh=2.0, step=0, sender_id="1"
    )
    return {
        "not json": (b"order", b"{not json"),
        "not utf-8": (b"order", b"\xff\xfe\x00"),
        "old protocol": (b"status", wire(status, v=1)),
        "not pub-sub traffic": (b"order", encode_message(trade).encode()),
        "string step": (b"status", wire(status, step="0")),
        "unknown sender": (b"status", wire(status, agent_id=99)),
        "order without seq": (b"order", wire(order, seq=None)),
        "string sent_at": (b"order", wire(order, sent_at="now")),
    }[kind]


@pytest.mark.parametrize(
    "kind",
    [
        "not json",
        "not utf-8",
        "old protocol",
        "not pub-sub traffic",
        "string step",
        "unknown sender",
        "order without seq",
        "string sent_at",
    ],
)
def test_malformed_message_is_counted_not_fatal(comm_config, kind):
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        _send_raw(net[1], *_malformed(kind))
        assert _drain_until(net, lambda: all(net[i].stats.protocol_errors == 1 for i in (0, 2)))
        assert all(n.inbox == {} for n in net.nodes.values())

        _publish_step(net, 0, {0: "BUYING", 1: "SELLING", 2: IDLE_STATE})
        assert not net.await_step(0).degraded
        assert net.stats().protocol_errors == 2


def test_ten_nodes_ten_steps_lossless_and_identical(comm_config):
    rng = np.random.default_rng(7)
    ids = list(range(10))
    with PeerNetwork(ids, comm_config) as net:
        net.handshake()
        for step in range(10):
            activation = [int(i) for i in rng.permutation(ids)]
            states = {
                i: str(rng.choice(["BUYING", "SELLING", IDLE_STATE], p=[0.4, 0.4, 0.2]))
                for i in ids
            }
            seqs = _publish_step(net, step, states, activation)
            report = net.await_step(step)
            assert not report.degraded, report
            reference = [m.msg_id for m in net[0].orders_at(step)]
            for node in net.nodes.values():
                orders = node.orders_at(step)
                assert [m.msg_id for m in orders] == reference  # C6
                assert [m.seq for m in orders] == list(range(len(seqs)))
                assert len(node.statuses_at(step)) == expected_per_step(10)
        stats = net.stats()
        assert stats.status_expected == stats.status_received == 10 * 10 * 9
        assert stats.order_expected == stats.order_received > 0
        assert stats.late == stats.protocol_errors == stats.degraded_steps == 0
        assert stats.status_loss_steps == 0


def test_latency_is_sampled_once_per_received_message(comm_config):
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        assert net.stats().latency_ms == []  # handshake traffic is not sampled
        _publish_step(net, 0, {0: "BUYING", 1: "SELLING", 2: "SELLING"})
        assert not net.await_step(0).degraded
        for node in net.nodes.values():
            # Two peers x (STATUS + ORDER); the node's own pair never travels.
            assert len(node.stats.latency_ms) == 4
        samples = net.stats().latency_ms
        assert len(samples) == 12
        # Both ends use time.time(); a monotonic clock on either end would
        # put these off by decades, not milliseconds.
        assert all(0.0 <= s < 1000.0 * comm_config.step_timeout_s for s in samples)


def test_closed_steps_are_released_and_only_the_last_is_readable(comm_config):
    states = {0: "BUYING", 1: "SELLING", 2: IDLE_STATE}
    with PeerNetwork([0, 1, 2], comm_config) as net:
        net.handshake()
        _publish_step(net, 0, states)
        with pytest.raises(CommError, match="not readable"):
            net[0].orders_at(0)  # still open: the set may differ per node
        net.await_step(0)
        for step in range(1, 20):
            _publish_step(net, step, states)
            net.await_step(step)
            assert all(list(n.inbox) == [step] for n in net.nodes.values())
        with pytest.raises(CommError, match="not readable"):
            net[0].orders_at(18)
        assert len(net[0].orders_at(19)) == 2


def test_publish_refuses_what_would_split_the_copies(comm_config):
    with PeerNetwork([0, 1], comm_config) as net:
        order = _order_msg(0, 0, OrderSide.BUY, seq=0)
        with pytest.raises(CommError, match="handshake"):
            net[0].publish_order(order)
        net.handshake()
        with pytest.raises(ValueError, match="seq"):
            net[0].publish_order(_order_msg(0, 0, OrderSide.BUY, seq=None))
        with pytest.raises(ValueError, match="agent 1"):
            net[0].publish_status(_status(1, 0, IDLE_STATE))
        with pytest.raises(TypeError):
            net[0].publish_order(_status(0, 0, "BUYING"))
        assert net[0].inbox == {}

        net[0].publish_order(order)
        net[0].publish_status(_status(0, 0, "BUYING"))
        with pytest.raises(CommError, match="already published"):
            net[0].publish_order(_order_msg(0, 0, OrderSide.BUY, seq=1))
        with pytest.raises(CommError, match="already published"):
            net[0].publish_status(_status(0, 0, IDLE_STATE))
        net[1].publish_status(_status(1, 0, IDLE_STATE))
        assert not net.await_step(0).degraded
        assert [(m.agent_id, m.seq) for m in net[1].orders_at(0)] == [(0, 0)]
        assert net[1].statuses_at(0)[0].state == "BUYING"
        with pytest.raises(CommError, match="already closed"):
            net[0].publish_status(_status(0, 0, IDLE_STATE))


def test_await_step_refuses_misuse(comm_config):
    with PeerNetwork([0, 1], comm_config) as net:
        with pytest.raises(CommError, match="handshake"):
            net.await_step(0)
        net.handshake()
        _publish_step(net, 0, {0: IDLE_STATE, 1: IDLE_STATE})
        net.await_step(0)
        with pytest.raises(CommError, match="already closed"):
            net.await_step(0)
        net.close()
        with pytest.raises(CommError, match="closed"):
            net.await_step(1)
