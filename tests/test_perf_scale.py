"""Performance scale experiment (Phase 5, step 5).

The 10-agent, 1-day subset runs in every ``pytest``; the full 3-day matrix
at 30 and 50 agents is marked slow (``pytest -m slow``).
"""

import csv

import pytest

import experiments.perf_scale as perf
from experiments.perf_scale import (
    COLUMNS,
    compare,
    household_mix,
    main,
    resolve_transport,
    run_one,
)


def _assert_clean(row: dict) -> None:
    assert row["error"] == ""
    assert row["violations"] == 0
    assert row["balance_error_kwh"] < 1e-9
    assert row["step_p50_ms"] > 0
    if row["backend"] == "cda_zmq":
        assert row["status_loss"] == 0 and row["order_loss"] == 0
        assert row["timeouts"] == 0 and row["late"] == 0 and row["protocol_errors"] == 0
        assert row["trades_nacked"] == 0 and row["trades_timed_out"] == 0
        assert row["trades_confirmed"] == row["trades_proposed"] == row["trades"]


def test_household_mix_keeps_the_default_ratio():
    assert household_mix(10) == (6, 4)  # SimulationConfig defaults
    assert household_mix(30) == (18, 12)
    assert household_mix(50) == (30, 20)
    assert household_mix(1) == (1, 0)
    with pytest.raises(ValueError):
        household_mix(0)


def test_auto_transport_switches_above_30():
    assert resolve_transport("auto", 10) == "tcp"
    assert resolve_transport("auto", 30) == "tcp"
    assert resolve_transport("auto", 50) == "inproc"
    assert resolve_transport("tcp", 50) == "tcp"
    with pytest.raises(ValueError):
        resolve_transport("udp", 10)


@pytest.fixture(scope="module")
def ten_agent_rows():
    rows = [run_one(10, backend, days=1) for backend in perf.BACKENDS]
    compare(rows)
    return {row["backend"]: row for row in rows}


def test_ten_agents_one_day(ten_agent_rows):
    for row in ten_agent_rows.values():
        _assert_clean(row)
        assert set(row) == set(COLUMNS)
    zmq_row = ten_agent_rows["cda_zmq"]
    assert zmq_row["transport"] == "tcp"
    assert zmq_row["same_as_cda"] is True
    assert ten_agent_rows["cda"]["transport"] == ""


def test_p2p_reduces_dependence_and_cost(ten_agent_rows):
    base = ten_agent_rows["logging"]
    assert base["dependence_reduction_pct"] == 0 and base["cost_saving_tl"] == 0
    for backend in ("cda", "cda_zmq"):
        row = ten_agent_rows[backend]
        assert row["dependence_reduction_pct"] > 0
        assert row["cost_saving_tl"] > 0
        assert row["dependence_reduction_pct"] == ten_agent_rows["cda"]["dependence_reduction_pct"]


def test_a_failed_run_is_a_row_not_a_crash(monkeypatch):
    class Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("handshake " + "x" * 500)

    monkeypatch.setattr(perf, "MicrogridModel", Broken)
    row = run_one(10, "cda_zmq", days=1)
    assert row["error"].startswith("RuntimeError: handshake")
    assert len(row["error"]) == perf.ERROR_CHARS
    compare([row])  # skips failed rows
    assert row["same_as_cda"] is None


def test_main_writes_csv_and_plot(tmp_path):
    csv_path, png_path = tmp_path / "perf.csv", tmp_path / "perf.png"
    code = main(["--agents", "10", "--days", "1", "--backends", "cda", "logging",
                 "--csv", str(csv_path), "--png", str(png_path)])
    assert code == 0
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["backend"] for r in rows] == ["logging", "cda"]  # baseline first
    assert float(rows[1]["dependence_reduction_pct"]) > 0
    assert png_path.stat().st_size > 0


@pytest.mark.slow
@pytest.mark.parametrize("num_agents", [30, 50])
def test_full_matrix_at_scale(num_agents):
    rows = [run_one(num_agents, backend) for backend in perf.BACKENDS]
    compare(rows)
    for row in rows:
        _assert_clean(row)
        assert row["steps"] == 3 * 96
    zmq_row = rows[-1]
    assert zmq_row["transport"] == ("tcp" if num_agents <= 30 else "inproc")
    assert zmq_row["same_as_cda"] is True
    assert zmq_row["dependence_reduction_pct"] > 0
