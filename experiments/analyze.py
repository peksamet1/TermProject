"""Phase 6 analysis: seasons, seeds, and one-factor sensitivity.

Reuses ``experiments.perf_scale`` (``make_config`` / ``run_one`` / ``compare``)
and ``microgrid.metrics``. Does not redefine KPIs.

Default run (from the repo root):

    python -m experiments.analyze

writes ``results/analysis/``:

- ``runs.csv`` — one row per finished simulation
- ``summary.csv`` — mean and sample standard deviation across seeds
- ``seasons.png``, ``sensitivity_*.png``
- ``evidence.md`` — PSD 9.1 criteria, the measured value, and the test that
  backs it

Economy runs use the in-process markets (``logging`` baseline and ``cda``).
One ``cda_zmq`` run per season (first seed, 10 agents, tcp) checks that the
bill matches ``cda``. Fifty-node tcp is not used.

Synthetic profiles: a seed changes cloud noise, load noise and Mesa
activation order. A CSV profile is fixed, so a seed changes only activation
order. Summer is day 172, winter day 355, three days, matching ``perf_scale``.
With ``--profiles``, those seasons are two windows in the file (day-of-year
172 and 355, or ``--summer-start`` / ``--winter-start``).

Battery capacity cannot be zero (``Battery`` rejects it); the sweep starts
at 2 kWh. Start-of-step SoC sets both the price and the battery's share of
the net. At the default 0.25 margins every bid still clears every ask. The
share changes the residual, so grid dependence moves with SoC.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from pathlib import Path

import pandas as pd

from experiments.perf_scale import DAYS, SEED, compare, run_one

AGENTS = 10
SEASON_DAYS = {"summer": 172, "winter": 355}
SEASON_ORDER = ("summer", "winter")
ECONOMY = ("logging", "cda")  # baseline first, so compare() has a reference
SEEDS = tuple(range(SEED, SEED + 8))  # 42..49
SENSITIVITY_SEEDS = SEEDS[:5]
# Capacity must be positive. 2 kWh is the near-empty end of the sweep.
BATTERY_KWH = (2.0, 5.0, 10.0, 20.0)
PROSUMER_SHARES = (0.2, 0.4, 0.6, 0.8, 1.0)
PV_KW = (2.0, 5.0, 8.0)
RETAIL_TL = (2.0, 3.5, 5.0)
DEFAULTS = {
    "start_day_of_year": SEASON_DAYS["summer"],
    "pv_peak_kw": 5.0,
    "battery_capacity_kwh": 10.0,
    "retail_tl_per_kwh": 3.5,
    "prosumer_share": 0.6,
}
SWEEPS = {
    "battery_kwh": ("battery_capacity_kwh", BATTERY_KWH),
    "prosumer_share": ("prosumer_share", PROSUMER_SHARES),
    "pv_kw": ("pv_peak_kw", PV_KW),
    "retail_tl": ("retail_tl_per_kwh", RETAIL_TL),
}
SUMMARY_METRICS = (
    "grid_dependence",
    "dependence_reduction_pct",
    "trade_success_rate",
    "demand_fill_rate",
    "community_cost_tl",
    "cost_saving_tl",
    "local_traded_kwh",
)
RESULTS_DIR = Path("results") / "analysis"
COLORS = {"logging": "#2a78d6", "cda": "#eb6834"}


def scenario_kwargs(overrides: dict | None = None) -> dict:
    """Keyword arguments for ``run_one`` / ``make_config``."""
    values = dict(DEFAULTS)
    if overrides:
        values.update(overrides)
    return values


def season_windows(
    profiles: str | None,
    days: int,
    summer_start: str | None,
    winter_start: str | None,
) -> dict[str, str | None]:
    """CSV timestamp that opens each season, or None for synthetic profiles.

    Picks the timestamp whose day-of-year is closest to 172 / 355 among
    rows that still leave ``days`` of data. Explicit starts win.
    """
    if not profiles:
        return {"summer": None, "winter": None}
    if summer_start or winter_start:
        if not (summer_start and winter_start):
            raise ValueError("give both --summer-start and --winter-start, or neither")
        return {"summer": summer_start, "winter": winter_start}

    frame = pd.read_csv(profiles, usecols=["timestamp"])
    stamps = pd.to_datetime(frame["timestamp"], errors="coerce").dropna()
    stamps = pd.Series(sorted(stamps.unique()))
    need = days * 96
    if len(stamps) < need:
        raise ValueError(f"{profiles} has {len(stamps)} steps; need {need} for {days} days")
    candidates = stamps.iloc[: len(stamps) - need + 1]

    def pick(target: int) -> str:
        doy = candidates.dt.dayofyear.to_numpy()
        distance = abs(doy - target)
        distance = [min(d, 365 - d) for d in distance]
        chosen = candidates.iloc[int(pd.Series(distance).idxmin())]
        return pd.Timestamp(chosen).strftime("%Y-%m-%d %H:%M:%S")

    return {"summer": pick(SEASON_DAYS["summer"]), "winter": pick(SEASON_DAYS["winter"])}


def _label(row: dict, sweep: str, value) -> None:
    row["sweep"] = sweep
    row["sweep_value"] = value
    row["profile_start"] = row.get("profile_start") or ""


def run_group(rows: list[dict], specs: list[tuple], **common) -> None:
    """Run every (backend, seed, extra) spec and compare inside matching cells.

    A cell is one seed and one factor value. ``compare`` only sees that cell,
    so two battery sizes with the same agent count do not overwrite each other.
    """
    fresh: list[dict] = []
    for backend, seed, sweep, value, extra in specs:
        kwargs = scenario_kwargs(extra)
        kwargs.pop("season", None)
        row = run_one(AGENTS, backend, seed=seed, **common, **kwargs)
        row["season"] = extra.get("season", "")
        row["profile_start"] = common.get("start") or ""
        _label(row, sweep, value)
        fresh.append(row)
        flag = "ERROR " + row["error"] if row["error"] else (
            f"dependence={100 * row['grid_dependence']:.1f}%"
        )
        print(
            f"{sweep}={value} seed={seed} {row['season'] or '-':<6} {backend:<8} {flag}",
            flush=True,
        )
    _compare_cells(fresh)
    rows.extend(fresh)


def _compare_cells(rows: list[dict]) -> None:
    cells: dict[tuple, list[dict]] = {}
    for row in rows:
        if row["error"]:
            continue
        key = (row["sweep"], row["sweep_value"], row["season"], row["seed"])
        cells.setdefault(key, []).append(row)
    for cell in cells.values():
        compare(cell)


def season_specs(seeds: tuple[int, ...], include_zmq: bool) -> list[tuple]:
    specs = []
    for season in SEASON_ORDER:
        day = SEASON_DAYS[season]
        for seed in seeds:
            for backend in ECONOMY:
                specs.append((backend, seed, "season", season, {
                    "season": season, "start_day_of_year": day,
                }))
        if include_zmq:
            specs.append(("cda_zmq", seeds[0], "season", season, {
                "season": season, "start_day_of_year": day,
            }))
    return specs


def sensitivity_specs(seeds: tuple[int, ...]) -> list[tuple]:
    specs = []
    for sweep, (field, values) in SWEEPS.items():
        for value in values:
            for seed in seeds:
                for backend in ECONOMY:
                    specs.append((backend, seed, sweep, value, {
                        "season": "summer", field: value,
                    }))
    return specs


def _numbers(rows: list[dict], name: str) -> list[float]:
    values = []
    for row in rows:
        raw = row.get(name)
        if raw in (None, ""):
            continue
        value = float(raw)
        if math.isfinite(value):
            values.append(value)
    return values


def summarize(rows: list[dict]) -> list[dict]:
    """Mean and sample standard deviation of each KPI, per sweep point and backend.

    One seed has no sample deviation; that cell's std is 0. Failed runs are
    left out. ``cda_zmq`` is a check, not part of the mean.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("error") or row["backend"] not in ECONOMY:
            continue
        key = (row["sweep"], row["sweep_value"], row["season"], row["backend"])
        groups.setdefault(key, []).append(row)

    summary = []
    for (sweep, value, season, backend), cell in sorted(groups.items(), key=_summary_sort):
        item = {
            "sweep": sweep,
            "sweep_value": value,
            "season": season,
            "backend": backend,
            "n_seeds": len(cell),
            "violations_max": max(int(row["violations"]) for row in cell),
            "balance_error_max": max(float(row["balance_error_kwh"]) for row in cell),
        }
        for name in SUMMARY_METRICS:
            values = _numbers(cell, name)
            item[f"{name}_mean"] = statistics.fmean(values) if values else math.nan
            item[f"{name}_std"] = statistics.stdev(values) if len(values) >= 2 else 0.0
        summary.append(item)
    return summary


def _summary_sort(item: tuple) -> tuple:
    (sweep, value, season, backend), _ = item
    season_index = SEASON_ORDER.index(season) if season in SEASON_ORDER else 99
    backend_index = ECONOMY.index(backend) if backend in ECONOMY else 99
    return (sweep, float(value) if _is_number(value) else 0, season_index, backend_index)


def _is_number(value) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.6g}"
    return str(value)


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _fmt(row.get(name)) for name in fields})


def _style(ax) -> None:
    ax.grid(True, color="#e4e3dc", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def plot_seasons(summary: list[dict], path: Path) -> None:
    """Mean grid dependence of baseline and P2P, summer and winter, with std."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.2, 4.2), constrained_layout=True)
    width = 0.36
    for index, backend in enumerate(ECONOMY):
        means, stds = [], []
        for season in SEASON_ORDER:
            cell = _cell(summary, "season", season, season, backend)
            means.append(100 * cell["grid_dependence_mean"])
            stds.append(100 * cell["grid_dependence_std"])
        positions = [i + (index - 0.5) * width for i in range(len(SEASON_ORDER))]
        ax.bar(positions, means, width=width, yerr=stds, capsize=4,
               color=COLORS[backend], label=backend, ecolor="#52514e")
    ax.set_xticks(range(len(SEASON_ORDER)), ["summer (day 172)", "winter (day 355)"])
    ax.set_ylabel("Grid dependence (%)")
    ax.set_ylim(bottom=0)
    ax.set_title("Grid import / consumption, mean ± sample std")
    ax.legend(frameon=False)
    _style(ax)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_sensitivity(summary: list[dict], sweep: str, path: Path) -> None:
    """One factor against the P2P reduction in grid dependence.

    The tariff sweep adds a second panel for the community saving in TL.
    Reduction and lira do not share an axis.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    points = [
        row for row in summary
        if row["sweep"] == sweep and row["backend"] == "cda" and row["season"] == "summer"
    ]
    points.sort(key=lambda row: float(row["sweep_value"]))
    xs = [float(row["sweep_value"]) for row in points]
    # Baseline import can already be zero (every home is a prosumer). The
    # reduction is then undefined; draw that point at 0.
    reduction = [_finite(row["dependence_reduction_pct_mean"]) for row in points]
    reduction_std = [_finite(row["dependence_reduction_pct_std"]) for row in points]
    tariff = sweep == "retail_tl"
    fig, axes = plt.subplots(1, 2 if tariff else 1, figsize=(10 if tariff else 6.4, 4.2),
                             constrained_layout=True)
    ax = axes[0] if tariff else axes
    ax.errorbar(xs, reduction, yerr=reduction_std, color=COLORS["cda"], marker="o",
                linewidth=2, capsize=4)
    ax.set_xlabel(_axis_label(sweep))
    ax.set_ylabel("Dependence reduction vs logging (%)")
    ax.set_title("P2P benefit, summer, mean ± sample std")
    _style(ax)
    if tariff:
        saving = [_finite(row["cost_saving_tl_mean"]) for row in points]
        saving_std = [_finite(row["cost_saving_tl_std"]) for row in points]
        axes[1].errorbar(xs, saving, yerr=saving_std, color="#1baf7a", marker="s",
                         linewidth=2, capsize=4)
        axes[1].set_xlabel(_axis_label(sweep))
        axes[1].set_ylabel("Community saving vs logging (TL / 3 days)")
        axes[1].set_title("Money kept inside the community")
        _style(axes[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _finite(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _axis_label(sweep: str) -> str:
    return {
        "battery_kwh": "Battery capacity (kWh)",
        "prosumer_share": "Prosumer share",
        "pv_kw": "PV peak (kW)",
        "retail_tl": "Retail price (TL/kWh)",
    }[sweep]


def _cell(summary: list[dict], sweep: str, value, season: str, backend: str) -> dict:
    for row in summary:
        if (row["sweep"] == sweep and str(row["sweep_value"]) == str(value)
                and row["season"] == season and row["backend"] == backend):
            return row
    raise KeyError((sweep, value, season, backend))


def write_evidence(rows: list[dict], summary: list[dict], path: Path, profiles: str | None) -> None:
    """PSD 9.1 table: criterion, what this run measured, which test backs it."""
    ok = [row for row in rows if not row.get("error")]
    economy = [row for row in ok if row["backend"] in ECONOMY]
    violations = max((int(row["violations"]) for row in economy), default=-1)
    balance = max((float(row["balance_error_kwh"]) for row in economy), default=math.nan)
    zmq = [row for row in ok if row["backend"] == "cda_zmq"]
    zmq_match = all(row.get("same_as_cda") is True for row in zmq) if zmq else False
    loss = sum(int(row["status_loss"] or 0) + int(row["order_loss"] or 0) for row in zmq)

    def reduction(season: str) -> str:
        cell = _cell(summary, "season", season, season, "cda")
        return (f"{cell['dependence_reduction_pct_mean']:.1f}% "
                f"± {cell['dependence_reduction_pct_std']:.1f} "
                f"(n={cell['n_seeds']})")

    source = profiles or "synthetic (SoC price and battery share; margins 0.25 still fully cross)"
    lines = [
        "# PSD 9.1 kanıt tablosu",
        "",
        f"Kaynak: `{source}`. Süre: {DAYS if not economy else economy[0]['days']} gün, "
        f"{AGENTS} ajan. Adım başı SoC fiyatı ve batarya payını belirler; marj 0,25 iken "
        "her alış her satışı keser. Pay, piyasaya kalan kWh'yi değiştirir.",
        "Sentetik koşuda seed profil gürültüsünü de değiştirir. CSV koşuda enerji serisi "
        "dosyadan gelir; seed yalnız Mesa aktivasyon sırasını değiştirir.",
        "",
        "| Kriter | Ölçülen | Kanıt |",
        "| --- | --- | --- |",
        f"| 1. Ajan modeli: enerji dengesi ve SoC | Bu koşuda en büyük denge hatası "
        f"{balance:.2e} kWh, kısıt ihlali {violations} | `tests/test_model.py`, `tests/test_physics.py`, `runs.csv` |",
        f"| 2. İletişim: kayıpsız değişim | `cda_zmq` satırlarında kayıp {loss}; "
        f"onay `cda` ile aynı fatura: {zmq_match} | `tests/test_comm_network.py`, `tests/test_zmq_market.py`, `tests/test_perf_scale.py` |",
        f"| 3. CDA: işlem kaydı ve kopya eşitliği | `cda_zmq` = `cda` (işlem ve fatura): {zmq_match} | `tests/test_cda.py`, `tests/test_zmq_market.py` |",
        f"| 4. SimPy: kısıt ihlali 0, saatler birlikte | Kısıt ihlali {violations} | `tests/test_physics.py`, `tests/test_perf_scale.py` |",
        f"| 5. Performans: baseline'a göre şebeke bağımlılığı | Yaz {reduction('summer')}; "
        f"kış {reduction('winter')}. Hız tablosu Faz 5 `perf_scale` (README) | `summary.csv`, `experiments/perf_scale.py` |",
        "",
        "İşlem başarı oranı yazın satış hacmi alışı aştığı için düşüktür. Ayırt edici sonuç "
        "bağımlılık azalması ve talep karşılamadır (`demand_fill_rate` sütunu).",
        "",
        "Windows'ta 50 düğüm tcp el sıkışması zaman aşımına düşer. Bu deney 10 ajan kullanır; "
        "50 ajan ölçümü `perf_scale` içinde `inproc` ile yapılmıştır.",
        "",
        "Gerçek profil hattı (`microgrid/profiles/real_data.py`, CC BY 4.0 Ausgrid + PVGIS): "
        "yarım saatlik kWh iki eşit 15 dakikaya bölünür; tüketim ortalama ~6.6 kWh/gün olacak "
        "şekilde tek katsayıyla ölçeklenir; tarihler 182 gün kaydırılır (Avustralya kışı, İstanbul yazı); "
        "PV = İstanbul PVGIS kW/kWp × hanenin kWp'si. OPSD ana sonuç değildir.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def plot_all(summary: list[dict], directory: Path) -> None:
    plot_seasons(summary, directory / "seasons.png")
    for sweep in SWEEPS:
        if any(row["sweep"] == sweep for row in summary):
            plot_sensitivity(summary, sweep, directory / f"sensitivity_{sweep}.png")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", type=int, nargs="+", default=list(SEEDS))
    parser.add_argument("--sensitivity-seeds", type=int, nargs="+", default=list(SENSITIVITY_SEEDS))
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--profiles", help="profile CSV; seasons become windows in this file")
    parser.add_argument("--summer-start")
    parser.add_argument("--winter-start")
    parser.add_argument("--skip-sensitivity", action="store_true")
    parser.add_argument("--skip-zmq", action="store_true")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    seeds = tuple(args.seeds)
    if len(seeds) < 1:
        parser.error("need at least one seed")
    windows = season_windows(args.profiles, args.days, args.summer_start, args.winter_start)
    if args.profiles and windows["summer"] == windows["winter"]:
        print("warning: summer and winter windows are the same timestamp", flush=True)

    rows: list[dict] = []
    for season in SEASON_ORDER:
        specs = [spec for spec in season_specs(seeds, not args.skip_zmq) if spec[3] == season]
        run_group(rows, specs, days=args.days, profiles=args.profiles, start=windows[season])
    if not args.skip_sensitivity:
        run_group(
            rows, sensitivity_specs(tuple(args.sensitivity_seeds)),
            days=args.days, profiles=args.profiles, start=windows["summer"],
        )

    failed = [row for row in rows if row["error"]]
    summary = summarize(rows)
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.out / "runs.csv")
    write_csv(summary, args.out / "summary.csv")
    if summary and not failed:
        plot_all(summary, args.out)
        write_evidence(rows, summary, args.out / "evidence.md", args.profiles)
    elif summary:
        write_csv(summary, args.out / "summary.csv")
    print(f"Wrote {args.out} ({len(rows) - len(failed)} ok, {len(failed)} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
