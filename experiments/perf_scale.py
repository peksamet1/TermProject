"""Performance scale experiment: 10 / 30 / 50 agents x 3 backends (Phase 5, step 5).

Runs the full simulation (Mesa + market + SimPy) for every agent count and
market backend with the same seed and the same profiles, and reports per run:

- Speed: step time p50 / p95, split into the four stages of
  ``MicrogridModel.step`` (agents, market incl. ZeroMQ, SimPy physics,
  DataCollector), plus model setup time (the ZeroMQ handshake for cda_zmq).
- Correctness: constraint violations (must be 0), the largest per-step energy
  balance error (must be ~0) and the Mesa / SimPy clock check (a ``SyncError``
  ends the run and is written to ``error``).
- Benefit of P2P against the no-P2P baseline (``logging``) of the same N:
  grid dependence and its reduction, trade success rate, community cost and
  the saving in TL (``microgrid.metrics``).
- Communication (cda_zmq only): message loss, timeouts, NACKs, PUB-SUB
  latency, confirmation RTT and barrier time.

``same_as_cda`` checks the Phase 4 claim at every N: the decentralized market
must give the same trades and the same bills as the in-process CDA.

Transport for cda_zmq: ``auto`` (default) uses tcp up to 30 agents and
inproc above. The 50-node tcp handshake is slow on Linux and does not finish
within the 5 s timeout on Windows (README, Phase 3 known limits); only the
address changes, the protocol is the same.

The household mix follows the default config: 60% prosumers (with PV and a
battery), 40% consumers. Profiles are synthetic unless ``--profiles`` names
a CSV in the common format, so the same experiment runs on real data later.

Run from the repo root (results/ is git-ignored; the table is copied into
the README as text):

    python -m experiments.perf_scale                       # full matrix, 3 days
    python -m experiments.perf_scale --agents 10 --days 1  # quick look
"""

from __future__ import annotations

import argparse
import csv
import math
import platform
import sys
import time
from pathlib import Path

import zmq

from microgrid.config import CommConfig, SimulationConfig
from microgrid.metrics import StageTimer, reduction_pct, run_kpis
from microgrid.model import MARKET_BACKENDS, MicrogridModel
from microgrid.physics import SyncError

AGENT_COUNTS = (10, 30, 50)
BACKENDS = ("logging", "cda", "cda_zmq")  # baseline first: the others compare to it
BASELINE = "logging"
TRANSPORTS = ("auto", "tcp", "inproc")
TCP_MAX_AGENTS = 30  # auto: inproc above this
PROSUMER_SHARE = 0.6
DAYS = 3  # the "3-day run" of Architecture 4.7
SEED = 42
RESULTS_DIR = Path("results")
ERROR_CHARS = 160
CSV_PATH = RESULTS_DIR / "perf_scale.csv"
PNG_PATH = RESULTS_DIR / "perf_scale.png"

COLUMNS = (
    "num_agents", "prosumers", "consumers", "backend", "transport", "days", "steps",
    "step_p50_ms", "step_p95_ms",
    "agents_p50_ms", "agents_p95_ms", "market_p50_ms", "market_p95_ms",
    "physics_p50_ms", "physics_p95_ms", "collect_p50_ms", "collect_p95_ms",
    "setup_s", "wall_s",
    "violations", "balance_error_kwh", "same_as_cda",
    "generation_kwh", "consumption_kwh", "grid_import_kwh", "grid_export_kwh",
    "local_traded_kwh", "trades", "buy_volume_kwh", "sell_volume_kwh",
    "grid_dependence", "dependence_reduction_pct", "trade_success_rate", "demand_fill_rate",
    "community_cost_tl", "cost_saving_tl",
    "fallback_import_kwh", "fallback_export_kwh", "fade_loss_kwh",
    "status_loss", "order_loss", "timeouts", "late", "protocol_errors",
    "trades_proposed", "trades_confirmed", "trades_nacked", "trades_timed_out",
    "latency_p50_ms", "latency_p95_ms", "rtt_p50_ms", "rtt_p95_ms",
    "barrier_p50_ms", "barrier_p95_ms", "handshake_ms",
    "profiles", "error", "platform", "python", "pyzmq", "libzmq", "seed",
)  # fmt: skip


def household_mix(num_agents: int) -> tuple[int, int]:
    """(prosumers, consumers) for ``num_agents``: 60 / 40, at least one prosumer."""
    if num_agents < 1:
        raise ValueError("need at least one agent")
    prosumers = max(1, math.floor(PROSUMER_SHARE * num_agents + 0.5))
    return prosumers, num_agents - prosumers


def resolve_transport(transport: str, num_agents: int) -> str:
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r}; use one of {TRANSPORTS}")
    if transport == "auto":
        return "tcp" if num_agents <= TCP_MAX_AGENTS else "inproc"
    return transport


def make_config(
    num_agents: int,
    backend: str,
    days: int = DAYS,
    seed: int = SEED,
    transport: str = "auto",
    profiles: str | None = None,
    start: str | None = None,
) -> SimulationConfig:
    prosumers, consumers = household_mix(num_agents)
    config = SimulationConfig(
        num_days=days,
        num_prosumers=prosumers,
        num_consumers=consumers,
        market_backend=backend,
        seed=seed,
        comm=CommConfig(transport=resolve_transport(transport, num_agents)),
    )
    if profiles:
        config.profile_source = "csv"
        config.profile_csv_path = profiles
        config.profile_start = start
    return config


def _environment() -> dict[str, str]:
    return {
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "pyzmq": zmq.pyzmq_version(),
        "libzmq": zmq.zmq_version(),
    }


def run_one(
    num_agents: int,
    backend: str,
    days: int = DAYS,
    seed: int = SEED,
    transport: str = "auto",
    profiles: str | None = None,
    start: str | None = None,
) -> dict:
    """One cell of the matrix; returns its CSV row. Never raises on a run failure."""
    if backend not in MARKET_BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; use one of {MARKET_BACKENDS}")
    config = make_config(num_agents, backend, days, seed, transport, profiles, start)
    row: dict = dict.fromkeys(COLUMNS)
    row.update(
        num_agents=num_agents,
        prosumers=config.num_prosumers,
        consumers=config.num_consumers,
        backend=backend,
        # Only cda_zmq opens sockets; the in-process backends have no transport.
        transport=config.comm.transport if backend == "cda_zmq" else "",
        days=days,
        steps=config.total_steps,
        profiles=profiles or "synthetic",
        seed=seed,
        error="",
        **_environment(),
    )
    timer = StageTimer()
    started = time.perf_counter()
    try:
        model = MicrogridModel(config=config)
        row["setup_s"] = time.perf_counter() - started
        with model:
            timer.attach(model)
            timer.run(model, config.total_steps)
            row.update(run_kpis(model))
    except (SyncError, zmq.ZMQError, OSError, RuntimeError) as exc:
        # First line only, cut short: a handshake timeout lists every node.
        row["error"] = f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]
    row["wall_s"] = time.perf_counter() - started
    row.update(timer.summary())
    return row


def compare(rows: list[dict]) -> None:
    """Fill the columns that compare a run with another run of the same N."""
    by_cell = {(r["num_agents"], r["backend"]): r for r in rows if not r["error"]}
    for row in rows:
        if row["error"]:
            continue
        base = by_cell.get((row["num_agents"], BASELINE))
        if base is not None:
            row["dependence_reduction_pct"] = reduction_pct(
                row["grid_dependence"], base["grid_dependence"]
            )
            row["cost_saving_tl"] = base["community_cost_tl"] - row["community_cost_tl"]
        cda = by_cell.get((row["num_agents"], "cda"))
        if row["backend"] == "cda_zmq" and cda is not None:
            row["same_as_cda"] = (
                row["trades"] == cda["trades"]
                and math.isclose(row["local_traded_kwh"], cda["local_traded_kwh"], abs_tol=1e-9)
                and math.isclose(row["community_cost_tl"], cda["community_cost_tl"], abs_tol=1e-9)
            )


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.6g}"
    return str(value)


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _fmt(row.get(name)) for name in COLUMNS})


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# Categorical slots 1-3 of the reference palette, one per backend; markers
# differ too, so no series is told apart by color alone.
BACKEND_STYLES = {
    "logging": ("#2a78d6", "o", "logging (no P2P)"),
    "cda": ("#eb6834", "s", "cda (in-process)"),
    "cda_zmq": ("#1baf7a", "^", "cda_zmq (ZeroMQ)"),
}


def plot(rows: list[dict], path: Path) -> None:
    """N against step time p50 and against grid dependence.

    Two panels, never two y-scales on one: ms and a ratio share no unit.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ok = [r for r in rows if not r["error"]]
    fig, (ax_t, ax_g) = plt.subplots(1, 2, figsize=(11, 4.2), constrained_layout=True)
    counts = sorted({int(r["num_agents"]) for r in ok})
    for backend, (color, marker, label) in BACKEND_STYLES.items():
        cell = sorted((r for r in ok if r["backend"] == backend), key=lambda r: int(r["num_agents"]))
        if not cell:
            continue
        ns = [int(r["num_agents"]) for r in cell]
        kw = {"color": color, "marker": marker, "linewidth": 2, "markersize": 8, "label": label}
        ax_t.plot(ns, [float(r["step_p50_ms"]) for r in cell], **kw)
        if backend == "cda_zmq":
            # auto switches transport with N; say which one each point used.
            for r in cell:
                ax_t.annotate(r["transport"], (int(r["num_agents"]), float(r["step_p50_ms"])),
                              textcoords="offset points", xytext=(0, 9), ha="center",
                              fontsize=8, color="#52514e")
        # cda and cda_zmq give the same dependence; dashed so both stay visible.
        style = "--" if backend == "cda_zmq" else "-"
        ax_g.plot(ns, [100 * float(r["grid_dependence"]) for r in cell], linestyle=style, **kw)
    ax_t.set_yscale("log")
    ax_t.set_title("Step time p50 (all four stages)")
    ax_t.set_ylabel("ms (log scale)")
    ax_g.set_title("Grid dependence (grid import / consumption)")
    ax_g.set_ylabel("%")
    ax_g.set_ylim(bottom=0)
    for ax in (ax_t, ax_g):
        ax.set_xlabel("agents (N)")
        ax.set_xticks(counts)
        ax.grid(True, color="#e4e3dc", linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.legend(frameon=False, fontsize=9)
    env = ok[0] if ok else {}
    fig.suptitle(
        f"Performance scale, {env.get('days', '?')} days, seed {env.get('seed', '?')}, "
        f"{env.get('profiles', '?')} profiles ({env.get('platform', '?')}, "
        f"Python {env.get('python', '?')})",
        fontsize=10,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _line(row: dict) -> str:
    if row["error"]:
        return f"ERROR {row['error']}"
    text = (
        f"step p50={row['step_p50_ms']:.2f} ms p95={row['step_p95_ms']:.2f} ms "
        f"(market p50={row['market_p50_ms']:.2f}, physics p50={row['physics_p50_ms']:.2f}) "
        f"violations={row['violations']} balance_err={row['balance_error_kwh']:.1e} "
        f"dependence={100 * row['grid_dependence']:.1f}%"
    )
    if row["backend"] == "cda_zmq":
        text += (
            f" loss={row['status_loss'] + row['order_loss']} timeouts={row['timeouts']} "
            f"nacked={row['trades_nacked']}"
        )
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agents", type=int, nargs="+", default=list(AGENT_COUNTS))
    parser.add_argument("--backends", nargs="+", default=list(BACKENDS), choices=MARKET_BACKENDS)
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--transport", default="auto", choices=TRANSPORTS,
                        help="cda_zmq transport; auto = tcp up to 30 agents, inproc above")
    parser.add_argument("--profiles", help="profile CSV (timestamp, household_id, pv_kw, load_kw)")
    parser.add_argument("--start", help="first timestamp to use from the CSV")
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--png", type=Path, default=PNG_PATH)
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)

    # Baseline first in every N, so the comparison columns have a reference.
    backends = sorted(args.backends, key=lambda b: BACKENDS.index(b) if b in BACKENDS else 99)
    rows = []
    for n in args.agents:
        for backend in backends:
            row = run_one(n, backend, args.days, args.seed, args.transport,
                          args.profiles, args.start)
            rows.append(row)
            print(f"N={n:>2} {backend:<8}{row['transport']:<7} {_line(row)}", flush=True)
    compare(rows)
    write_csv(rows, args.csv)
    if not args.no_plot:
        plot(read_csv(args.csv), args.png)
    failed = [r for r in rows if r["error"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
