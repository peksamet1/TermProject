"""CommStats aggregation and the comm scale experiment (Phase 3, step 5).

The fast subset (N = 3 and 10, 20 steps, active_ratio = 1.0, the worst
case) runs in every ``pytest``. The 30 / 50 node runs are marked slow and
only run with ``pytest -m slow``: the default suite has to stay fast, or
nobody runs it.
"""

import csv
import dataclasses

import pytest

from experiments.comm_scale import (
    COLUMNS,
    NEGOTIATION_ROUNDS,
    publish_step,
    active_count,
    main,
    order_stream,
    run_one,
)
from microgrid.comm.network import PeerNetwork
from microgrid.agents.household import HouseholdState
from microgrid.comm.node import (
    BUYING_STATE,
    IDLE_STATE,
    NODE_COUNTERS,
    NODE_SAMPLES,
    SELLING_STATE,
    CommStats,
    percentile,
)
from microgrid.config import CommConfig

FAST_STEPS = 20
SLOW_STEPS = 192


def _assert_clean(row: dict) -> None:
    assert row["error"] == ""
    assert row["loss"] == 0
    assert row["status_loss"] == 0 and row["order_loss"] == 0
    assert row["timeouts"] == 0
    assert row["degraded_steps"] == 0
    assert row["late"] == 0
    assert row["protocol_errors"] == 0
    assert row["inconsistent_steps"] == 0  # C6 on every step
    assert row["replies"] == row["requests"]


def test_percentile_on_known_samples():
    samples = [4.0, 1.0, 3.0, 2.0]  # unsorted on purpose
    assert percentile(samples, 50) == pytest.approx(2.5)
    assert percentile(samples, 95) == pytest.approx(3.85)  # 1 + 0.95 * 3 = position 2.85
    assert percentile(samples, 0) == 1.0
    assert percentile(samples, 100) == 4.0
    assert percentile(list(range(1, 102)), 50) == 51
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None
    with pytest.raises(ValueError):
        percentile(samples, 101)


def test_summary_reports_percentiles_and_losses():
    stats = CommStats(barrier_ms=[1.0, 2.0, 3.0], status_expected=4, status_received=3)
    summary = stats.summary()
    assert summary["barrier_n"] == 3
    assert summary["barrier_p50_ms"] == 2.0
    assert summary["latency_p50_ms"] is None  # no samples: None, never 0
    assert summary["status_loss"] == 1
    with pytest.raises(ValueError):
        stats.percentile_of("handshake_ms", 50)


def test_network_stats_sum_the_node_counters():
    run = order_stream(3, 1.0, 5)
    with PeerNetwork(range(3), CommConfig(transport="inproc")) as net:
        net.handshake()
        for step, actions in enumerate(run):
            publish_step(net, step, actions)
            net.await_step(step)
        net.negotiation_rounds([(0, 1, "a"), (2, 1, "b")], lambda target, body: body)
        total = net.stats()
        for name in NODE_COUNTERS:
            assert getattr(total, name) == sum(getattr(n.stats, name) for n in net.nodes.values())
        for name in NODE_SAMPLES:
            assert sorted(getattr(total, name)) == sorted(
                x for n in net.nodes.values() for x in getattr(n.stats, name)
            )
        # The same totals from first principles, not from the tuples above:
        # 3 nodes x 5 steps x 2 peers, every node active at ratio 1.0.
        assert total.status_expected == total.status_received == 3 * 5 * 2
        assert total.order_expected == total.order_received == 3 * 5 * 2
        assert (total.requests, total.replies, total.served, total.timeouts) == (2, 2, 2, 0)
        assert total.req_opened == 2
        assert len(total.rtt_ms) == len(total.req_churn_ms) == 2
        assert len(total.latency_ms) == 3 * 5 * 2 * 2  # STATUS + ORDER per peer delivery
        # One node alone: its own counters, as recorded.
        node = net[1]
        assert node.stats.status_expected == 5 * 2  # 2 peers x 5 steps
        assert node.stats.served == 2
        assert total.steps == 5 and len(total.barrier_ms) == 5
        assert total.percentile_of("rtt_ms", 50) == percentile(total.rtt_ms, 50)


def test_every_commstats_field_is_aggregated():
    # A field a node fills but that is missing from NODE_COUNTERS /
    # NODE_SAMPLES would be silently dropped by PeerNetwork.stats(). Fields
    # the network fills itself are listed here by hand, on purpose.
    network_fields = {
        "handshake_ms", "handshake_rounds", "steps", "degraded_steps",
        "status_loss_steps", "barrier_ms",
    }  # fmt: skip
    node_fields = set(NODE_COUNTERS) | set(NODE_SAMPLES)
    assert not node_fields & network_fields
    assert {f.name for f in dataclasses.fields(CommStats)} == node_fields | network_fields


def test_wire_states_match_household_state():
    # The experiment sends STATUS without Mesa, through these constants; the
    # barrier's ORDER expectation hinges on IDLE_STATE being the real value.
    assert {IDLE_STATE, SELLING_STATE, BUYING_STATE} == {s.value for s in HouseholdState}
    assert IDLE_STATE == HouseholdState.IDLE.value


def test_order_stream_is_deterministic_with_exact_active_count():
    a = order_stream(10, 0.5, 8, seed=7)
    b = order_stream(10, 0.5, 8, seed=7)
    assert a == b
    assert a != order_stream(10, 0.5, 8, seed=8)
    for actions in a:
        assert sorted(x.node_id for x in actions) == list(range(10))
        assert sum(x.side is not None for x in actions) == active_count(10, 0.5) == 5
    assert active_count(3, 0.5) == 2  # half up
    assert active_count(3, 1.0) == 3


@pytest.mark.parametrize("transport", ["tcp", "inproc"])
@pytest.mark.parametrize("num_nodes", [3, 10])
def test_fast_subset_is_lossless(num_nodes, transport):
    row = run_one(num_nodes, transport, active_ratio=1.0, steps=FAST_STEPS)
    _assert_clean(row)
    pairs = num_nodes * (num_nodes - 1)  # all-to-all at 1.0
    assert row["requests"] == NEGOTIATION_ROUNDS * pairs
    # Pooled: one REQ per pair, reused by every later round.
    assert row["req_opened"] == pairs
    assert row["req_churn_p50_ms"] is not None
    assert row["broadcasts_per_step"] == 2 * num_nodes
    assert row["step_p50_ms"] is not None and row["step_p50_ms"] > 0


@pytest.mark.parametrize("transport", ["tcp", "inproc"])
def test_per_request_mode_opens_a_req_every_request(transport):
    row = run_one(3, transport, active_ratio=1.0, steps=2, req_mode="per-request")
    _assert_clean(row)
    assert row["requests"] == row["req_opened"] == NEGOTIATION_ROUNDS * 6
    assert row["req_close_ms"] is not None and row["req_close_ms"] >= 0


def test_close_idle_requests_empties_the_pool():
    with PeerNetwork([0, 1, 2], CommConfig(transport="inproc")) as net:
        net.negotiation_rounds([(0, 1, "a"), (0, 2, "b")], lambda target, body: body)
        assert net[0].req_sockets == 2
        assert net[0].close_idle_requests() == 2
        assert net[0].req_sockets == 0
        net.negotiation_rounds([(0, 1, "c")], lambda target, body: body)
        assert net[0].stats.req_opened == 3  # a fresh REQ, not a pooled one


def test_main_writes_csv_and_png(tmp_path):
    out_csv, out_png = tmp_path / "scale.csv", tmp_path / "scale.png"
    argv = ["--nodes", "3", "--transports", "inproc", "--steps", "3",
            "--csv", str(out_csv), "--png", str(out_png)]  # fmt: skip
    assert main(argv) == 0
    assert main([*argv, "--active-ratios", "1.0", "--variant", "retry", "--append"]) == 0
    with out_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert tuple(rows[0].keys()) == COLUMNS
    assert [r["variant"] for r in rows] == ["baseline", "baseline", "retry"]
    assert all(r["error"] == "" and r["loss"] == "0" for r in rows)
    assert out_png.stat().st_size > 0


@pytest.mark.slow
@pytest.mark.parametrize("transport", ["tcp", "inproc"])
@pytest.mark.parametrize("num_nodes", [30, 50])
def test_large_mesh_is_lossless(num_nodes, transport):
    row = run_one(num_nodes, transport, active_ratio=1.0, steps=SLOW_STEPS)
    _assert_clean(row)
