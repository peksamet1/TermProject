"""Communication scale experiment: 3 / 10 / 30 / 50 nodes (Phase 3, step 5).

Reproduces the comm figures of Architecture 4.7 in the repo: message loss,
timeouts and step time of the ZeroMQ layer alone. It is deliberately
independent of Mesa, the profiles and the CDA: Phase 4 does not exist yet,
real data does not either, and loss / delay depend on how many messages a
step carries, not on the energy values in them (protocol v2 fixes the
message size). A fixed-seed ``numpy.random.Generator`` builds the order
stream instead, so every run of the same seed sends the same messages.

``active_ratio`` is the share of nodes that are not IDLE in a step, i.e. that
publish an ORDER next to their STATUS. Per step the mesh carries N STATUS +
(active) ORDER broadcasts, each to N - 1 peers. ``active_ratio = 1.0`` is
the headline because it is the ceiling: every node sends both, and no data
source, mock or real, can put more on the wire. ``0.5`` is a typical load.

``seq`` is set here the way ``ZmqMarket.submit`` will set it in Phase 4: the
step's global send order, i.e. the (random) activation order. It is never a
per-node counter (see ``PeerNode._check_outbound``).

After the steps, one negotiation drive (``negotiation_rounds``) runs:
every active node asks every other active node, which at ``1.0`` is the
all-to-all worst case of N (N - 1) requests.

Step time (``step_p50_ms``) is publish + barrier: from the first publish of a
step until every node has closed it. ``barrier_p50_ms`` is the barrier part
only. A run that fails to come up (fd or socket limit) is written as a row
with ``error`` set, so the CSV shows which N needs which transport
(PSD 5.3, Architecture 4.3).

Run from the repo root (results/ is git-ignored, so the table is copied
into the README and Architecture 4.7 as text):

    .venv/bin/python -m experiments.comm_scale            # full matrix, 192 steps
    .venv/bin/python -m experiments.comm_scale --nodes 50 --transports tcp \\
        --io-threads 2 --variant io_threads=2 --append    # one tuning attempt
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zmq

from microgrid.comm.network import PeerNetwork
from microgrid.comm.node import CommStats, percentile
from microgrid.comm.protocol import make_order_message, make_status_message
from microgrid.config import CommConfig, TariffConfig
from microgrid.market.base import Order, OrderSide

NODE_COUNTS = (3, 10, 30, 50)
TRANSPORTS = ("tcp", "inproc")
ACTIVE_RATIOS = (0.5, 1.0)
STEPS = 192  # 2 days x 96 steps, the "2-day run" of Architecture 4.7
SEED = 42
RESULTS_DIR = Path("results")
CSV_PATH = RESULTS_DIR / "comm_scale.csv"
PNG_PATH = RESULTS_DIR / "comm_scale.png"
BASELINE = "baseline"

COLUMNS = (
    "variant", "num_nodes", "transport", "active_ratio", "steps",
    "io_threads", "hwm",
    "broadcasts_per_step", "deliveries_per_step",
    "status_loss", "order_loss", "loss", "degraded_steps", "status_loss_steps",
    "late", "protocol_errors", "inconsistent_steps",
    "requests", "replies", "timeouts", "req_opened",
    "latency_p50_ms", "latency_p95_ms", "rtt_p50_ms", "rtt_p95_ms",
    "step_p50_ms", "step_p95_ms", "barrier_p50_ms", "barrier_p95_ms",
    "negotiation_ms", "handshake_ms", "open_fds", "fd_limit", "wall_s",
    "error", "platform", "python", "pyzmq", "libzmq", "seed",
)  # fmt: skip


@dataclass(frozen=True)
class NodeAction:
    """What one node sends in one step; ``side is None`` means IDLE."""

    node_id: int
    side: OrderSide | None
    quantity_kwh: float
    price_tl_per_kwh: float


def active_count(num_nodes: int, active_ratio: float) -> int:
    """Non-IDLE nodes per step: ``active_ratio * N`` rounded half up, at least 1."""
    if not 0.0 < active_ratio <= 1.0:
        raise ValueError(f"active_ratio must be in (0, 1], got {active_ratio}")
    return max(1, math.floor(active_ratio * num_nodes + 0.5))


def order_stream(
    num_nodes: int, active_ratio: float, steps: int, seed: int = SEED
) -> list[list[NodeAction]]:
    """Per step, every node's action in activation (= send) order.

    Exactly ``active_count`` nodes are active each step, so the message count
    per step is constant and runs are comparable step by step. Prices fall
    inside the tariff band, like real orders would; the comm layer does not
    look at them.
    """
    rng = np.random.default_rng(seed)
    tariff = TariffConfig()
    k = active_count(num_nodes, active_ratio)
    stream = []
    for _ in range(steps):
        activation = rng.permutation(num_nodes)
        active = set(rng.choice(num_nodes, size=k, replace=False).tolist())
        actions = []
        for node_id in activation.tolist():
            if node_id in active:
                side = OrderSide.SELL if rng.random() < 0.5 else OrderSide.BUY
                qty = float(rng.uniform(0.05, 2.0))
                price = float(rng.uniform(tariff.feed_in_tl_per_kwh, tariff.retail_tl_per_kwh))
                actions.append(NodeAction(node_id, side, qty, price))
            else:
                actions.append(NodeAction(node_id, None, 0.0, 0.0))
        stream.append(actions)
    return stream


def publish_step(net: PeerNetwork, step: int, actions: list[NodeAction]) -> None:
    seq = 0  # global send order of this step, as ZmqMarket.submit will assign it
    for action in actions:
        node = net[action.node_id]
        if action.side is None:
            state, residual = "IDLE", 0.0
        elif action.side is OrderSide.SELL:
            state, residual = "SELLING", action.quantity_kwh
        else:
            state, residual = "BUYING", -action.quantity_kwh
        node.publish_status(
            make_status_message(
                agent_id=action.node_id,
                step=step,
                generation_kwh=max(residual, 0.0),
                consumption_kwh=max(-residual, 0.0),
                residual_kwh=residual,
                state=state,
            )
        )
        if action.side is not None:
            order = Order(
                action.node_id, action.side, action.quantity_kwh, action.price_tl_per_kwh, step
            )
            node.publish_order(make_order_message(order, seq=seq))
            seq += 1


def _orders_consistent(net: PeerNetwork, step: int) -> bool:
    """C6: every node's ``orders_at(step)`` is the same list, in the same order."""
    lists = [tuple(m.msg_id for m in node.orders_at(step)) for node in net.nodes.values()]
    return all(lst == lists[0] for lst in lists)


def _open_fds() -> int | None:
    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return None


def _fd_limit() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def _environment() -> dict[str, str]:
    return {
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "pyzmq": zmq.pyzmq_version(),
        "libzmq": zmq.zmq_version(),
    }


def run_one(
    num_nodes: int,
    transport: str = "tcp",
    active_ratio: float = 1.0,
    steps: int = STEPS,
    seed: int = SEED,
    io_threads: int = 1,
    hwm: int | None = None,
    variant: str = BASELINE,
) -> dict:
    """One cell of the matrix; returns its CSV row. Never raises on comm failure."""
    base = CommConfig()
    hwm = base.sndhwm if hwm is None else hwm
    config = CommConfig(transport=transport, io_threads=io_threads, sndhwm=hwm, rcvhwm=hwm)
    k = active_count(num_nodes, active_ratio)
    row: dict = {name: None for name in COLUMNS}
    row.update(
        variant=variant,
        num_nodes=num_nodes,
        transport=transport,
        active_ratio=active_ratio,
        steps=steps,
        io_threads=io_threads,
        hwm=hwm,
        broadcasts_per_step=num_nodes + k,
        deliveries_per_step=(num_nodes + k) * (num_nodes - 1),
        seed=seed,
        error="",
        **_environment(),
    )
    stream = order_stream(num_nodes, active_ratio, steps, seed)
    step_ms: list[float] = []
    inconsistent = 0
    started = time.perf_counter()
    try:
        with PeerNetwork(range(num_nodes), config) as net:
            row["fd_limit"] = _fd_limit()
            net.handshake()
            for step, actions in enumerate(stream):
                t0 = time.perf_counter()
                publish_step(net, step, actions)
                net.await_step(step)
                step_ms.append(1000.0 * (time.perf_counter() - t0))
                if not _orders_consistent(net, step):
                    inconsistent += 1
            active = [a.node_id for a in stream[-1] if a.side is not None] if stream else []
            requests = [(a, b, f"{a}->{b}") for a in active for b in active if a != b]
            t0 = time.perf_counter()
            net.negotiation_rounds(requests, lambda target, body: body)
            row["negotiation_ms"] = 1000.0 * (time.perf_counter() - t0)
            row["open_fds"] = _open_fds()
            stats = net.stats()
    except (zmq.ZMQError, OSError, RuntimeError) as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["wall_s"] = time.perf_counter() - started
        return row
    row["wall_s"] = time.perf_counter() - started
    row["inconsistent_steps"] = inconsistent
    row["step_p50_ms"] = percentile(step_ms, 50)
    row["step_p95_ms"] = percentile(step_ms, 95)
    _fill_from_stats(row, stats)
    return row


def _fill_from_stats(row: dict, stats: CommStats) -> None:
    summary = stats.summary()
    for name in (
        "status_loss", "order_loss", "degraded_steps", "status_loss_steps", "late",
        "protocol_errors", "requests", "replies", "timeouts", "req_opened",
        "latency_p50_ms", "latency_p95_ms", "rtt_p50_ms", "rtt_p95_ms",
        "barrier_p50_ms", "barrier_p95_ms", "handshake_ms",
    ):  # fmt: skip
        row[name] = summary[name]
    row["loss"] = stats.status_loss + stats.order_loss


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def write_csv(rows: list[dict], path: Path, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not (append and path.exists())
    with path.open("w" if new else "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        for row in rows:
            writer.writerow({name: _fmt(row.get(name)) for name in COLUMNS})


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# Categorical slots 1 and 2 of the reference palette: identity = transport.
# Line style carries active_ratio, so no series is told apart by color alone.
TRANSPORT_COLORS = {"tcp": "#2a78d6", "inproc": "#eb6834"}
RATIO_STYLES = {1.0: "-", 0.5: "--"}


def plot(rows: list[dict], path: Path) -> None:
    """N against step time p50 and against loss, baseline rows only.

    Two panels, never two y-scales on one: time and message counts do not
    share a unit.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    base = [r for r in rows if r["variant"] == BASELINE and not r["error"]]
    fig, (ax_t, ax_l) = plt.subplots(1, 2, figsize=(11, 4.2), constrained_layout=True)
    for transport, color in TRANSPORT_COLORS.items():
        for ratio, style in RATIO_STYLES.items():
            cell = sorted(
                (r for r in base if r["transport"] == transport and float(r["active_ratio"]) == ratio),
                key=lambda r: int(r["num_nodes"]),
            )
            if not cell:
                continue
            ns = [int(r["num_nodes"]) for r in cell]
            label = f"{transport}, active_ratio={ratio}"
            kw = dict(color=color, linestyle=style, linewidth=2, marker="o", markersize=7)
            ax_t.plot(ns, [float(r["step_p50_ms"]) for r in cell], label=label, **kw)
            ax_l.plot(ns, [int(r["loss"]) + int(r["timeouts"]) for r in cell], label=label, **kw)
    ax_t.set_yscale("log")
    ax_t.set_title("Step time p50 (publish + barrier)")
    ax_t.set_ylabel("ms (log scale)")
    ax_l.set_title("Lost messages + request timeouts")
    ax_l.set_ylabel("count over the run")
    ax_l.set_ylim(bottom=-0.5, top=max(5, ax_l.get_ylim()[1]))
    if base and all(int(r["loss"]) + int(r["timeouts"]) == 0 for r in base):
        # Every series sits on 0 and hides the ones drawn before it.
        ax_l.text(0.5, 0.5, f"0 in all {len(base)} runs", transform=ax_l.transAxes,
                  ha="center", color="#52514e", fontsize=11)  # fmt: skip
    for ax in (ax_t, ax_l):
        ax.set_xlabel("nodes (N)")
        ax.set_xticks(list(NODE_COUNTS))
        ax.grid(True, color="#e4e3dc", linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    ax_t.legend(frameon=False, fontsize=9)
    env = base[0] if base else {}
    fig.suptitle(
        f"ZeroMQ comm scale, {env.get('steps', '?')} steps, seed {env.get('seed', '?')} "
        f"({env.get('platform', '?')}, Python {env.get('python', '?')}, "
        f"libzmq {env.get('libzmq', '?')})",
        fontsize=10,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--nodes", type=int, nargs="+", default=list(NODE_COUNTS))
    parser.add_argument("--transports", nargs="+", default=list(TRANSPORTS), choices=TRANSPORTS)
    parser.add_argument("--active-ratios", type=float, nargs="+", default=list(ACTIVE_RATIOS))
    parser.add_argument("--steps", type=int, default=STEPS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--io-threads", type=int, default=1)
    parser.add_argument("--hwm", type=int, default=None, help="SNDHWM and RCVHWM")
    parser.add_argument("--variant", default=BASELINE, help="label of a tuning attempt")
    parser.add_argument("--append", action="store_true", help="add rows to an existing CSV")
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    parser.add_argument("--png", type=Path, default=PNG_PATH)
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(argv)

    rows = []
    for n in args.nodes:
        for transport in args.transports:
            for ratio in args.active_ratios:
                row = run_one(
                    n, transport, ratio, args.steps, args.seed,
                    io_threads=args.io_threads, hwm=args.hwm, variant=args.variant,
                )  # fmt: skip
                rows.append(row)
                print(
                    f"N={n:>2} {transport:<6} ratio={ratio:<3} "
                    + (
                        f"ERROR {row['error']}"
                        if row["error"]
                        else f"loss={row['loss']} timeouts={row['timeouts']} "
                        f"degraded={row['degraded_steps']} late={row['late']} "
                        f"step p50={row['step_p50_ms']:.2f} ms p95={row['step_p95_ms']:.2f} ms "
                        f"rtt p50={_fmt(row['rtt_p50_ms'])} ms"
                    ),
                    flush=True,
                )
    write_csv(rows, args.csv, append=args.append)
    if not args.no_plot:
        plot(read_csv(args.csv), args.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
