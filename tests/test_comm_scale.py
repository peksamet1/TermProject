"""CommStats aggregation and the comm scale experiment (Phase 3, step 5).

The fast subset (N = 3 and 10, 20 steps, active_ratio = 1.0, the worst
case) runs in every ``pytest``. The 30 / 50 node runs are marked slow and
only run with ``pytest -m slow``: the default suite has to stay fast, or
nobody runs it.
"""

import csv

import pytest

from experiments.comm_scale import (
    COLUMNS,
    publish_step,
    active_count,
    main,
    order_stream,
    run_one,
)
from microgrid.comm.network import PeerNetwork
from microgrid.comm.node import NODE_COUNTERS, NODE_SAMPLES, CommStats, percentile
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
        # One node alone: its own counters, as recorded.
        node = net[1]
        assert node.stats.status_expected == 5 * 2  # 2 peers x 5 steps
        assert node.stats.served == 2
        assert total.steps == 5 and len(total.barrier_ms) == 5
        assert total.percentile_of("rtt_ms", 50) == percentile(total.rtt_ms, 50)


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
    assert row["requests"] == num_nodes * (num_nodes - 1)  # all-to-all at 1.0
    assert row["broadcasts_per_step"] == 2 * num_nodes
    assert row["step_p50_ms"] is not None and row["step_p50_ms"] > 0


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
