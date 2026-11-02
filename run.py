"""Headless simulation run: prints a daily summary and saves plots.

Usage:
    python run.py                                   # synthetic profiles
    python run.py --profiles data/profiles_istanbul.csv [--start 2024-01-15] [--days 7]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from microgrid.config import SimulationConfig
from microgrid.model import MicrogridModel

RESULTS_DIR = Path(__file__).parent / "results"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profiles", help="profile CSV (timestamp, household_id, pv_kw, load_kw)")
    parser.add_argument("--start", help="first timestamp to use from the CSV, e.g. 2024-01-15")
    parser.add_argument("--days", type=int, help="number of simulated days")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = SimulationConfig()
    if args.profiles:
        config.profile_source = "csv"
        config.profile_csv_path = args.profiles
        config.profile_start = args.start
    if args.days:
        config.num_days = args.days
    model = MicrogridModel(config=config)
    model.run()

    df = model.datacollector.get_model_vars_dataframe()
    df["Day"] = df.index // config.steps_per_day

    print(f"Simulated {config.num_days} day(s), {config.total_steps} steps "
          f"({config.step_hours * 60:.0f} min each)")
    print(f"Agents: {config.num_prosumers} prosumers, {config.num_consumers} consumers")
    print(f"Market backend: {config.market_backend}")
    print(f"Profiles: {config.profile_source}"
          + (f" ({config.profile_csv_path})" if config.profile_csv_path else ""))
    print()
    header = (f"{'Day':>3} | {'Gen kWh':>8} | {'Cons kWh':>8} | "
              f"{'Local kWh':>9} | {'Import kWh':>10} | {'Export kWh':>10} | "
              f"{'Buy':>5} | {'Sell':>5}")
    print(header)
    print("-" * len(header))
    for day, group in df.groupby("Day"):
        print(
            f"{day:>3} | {group['TotalGeneration'].sum():>8.1f} | "
            f"{group['TotalConsumption'].sum():>8.1f} | "
            f"{group['LocalTradedKwh'].sum():>9.1f} | "
            f"{group['GridImport'].sum():>10.1f} | "
            f"{group['GridExport'].sum():>10.1f} | "
            f"{int(group['BuyOrders'].sum()):>5} | "
            f"{int(group['SellOrders'].sum()):>5}"
        )
    print()
    total_orders = len(model.market.orders)
    total_local = df["LocalTradedKwh"].sum()
    print(f"Total orders: {total_orders}")
    print(f"Total local P2P energy: {total_local:.1f} kWh")
    if hasattr(model.market, "trades"):
        print(f"Total trades: {len(model.market.trades)}")

    RESULTS_DIR.mkdir(exist_ok=True)
    hours = df.index * config.step_hours

    fig, (ax_energy, ax_soc) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    ax_energy.plot(hours, df["TotalGeneration"], label="Generation", color="tab:orange")
    ax_energy.plot(hours, df["TotalConsumption"], label="Consumption", color="tab:blue")
    ax_energy.plot(hours, df["LocalTradedKwh"], label="Local P2P", color="tab:green")
    ax_energy.plot(hours, df["GridImport"], label="Grid import", color="tab:red", alpha=0.6)
    ax_energy.plot(hours, df["GridExport"], label="Grid export", color="tab:olive", alpha=0.6)
    ax_energy.set_ylabel("kWh per step")
    ax_energy.legend(loc="upper right")
    ax_energy.set_title("Community energy balance (CDA)")

    ax_soc.plot(hours, df["AvgProsumerSoC"] * 100, color="tab:purple")
    ax_soc.set_ylabel("Avg prosumer SoC (%)")
    ax_soc.set_xlabel("Hour")
    ax_soc.set_ylim(0, 100)
    ax_soc.set_title("Battery state of charge")

    fig.tight_layout()
    out_path = RESULTS_DIR / "summary.png"
    fig.savefig(out_path, dpi=120)
    print(f"Plot saved to {out_path}")


if __name__ == "__main__":
    main()
