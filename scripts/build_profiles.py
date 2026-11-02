"""Build the simulation profile CSV from raw data, or write mock data.

Usage:
    # Real data (Ausgrid load + PVGIS Istanbul PV)
    python scripts/build_profiles.py --ausgrid "data/raw/ausgrid/2012-2013 Solar home electricity data v2.csv" \
        --pvgis data/raw/pvgis/istanbul_2019.json --out data/profiles_istanbul.csv

    # Mock data in the same format
    python scripts/build_profiles.py --synthetic --days 7 --out data/profiles_synthetic.csv

Then run: python run.py --profiles data/profiles_istanbul.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from microgrid.profiles.real_data import build_profiles  # noqa: E402
from microgrid.profiles.timeseries import generate_synthetic_csv  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ausgrid", type=Path, help="Ausgrid Solar Home half-hour CSV")
    parser.add_argument("--pvgis", type=Path, help="PVGIS hourly JSON or CSV (1 kWp)")
    parser.add_argument("--synthetic", action="store_true", help="write mock data instead")
    parser.add_argument("--households", type=int, default=20)
    parser.add_argument("--days", type=int, default=7, help="synthetic mode only")
    parser.add_argument("--pv-peak-kw", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("data/profiles_istanbul.csv"))
    args = parser.parse_args()

    if args.synthetic:
        path = generate_synthetic_csv(args.out, households=args.households, days=args.days,
                                      seed=args.seed, pv_peak_kw=args.pv_peak_kw)
        print(f"Wrote synthetic profiles to {path}")
        return
    if not (args.ausgrid and args.pvgis):
        parser.error("give --ausgrid and --pvgis, or use --synthetic")

    profiles = build_profiles(args.ausgrid, args.pvgis, households=args.households,
                              pv_peak_kw=args.pv_peak_kw, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    profiles.to_csv(args.out, index=False)

    homes = profiles["household_id"].nunique()
    days = len(profiles) / homes / 96
    print(f"Wrote {len(profiles)} rows ({homes} homes, {days:.0f} days) to {args.out}")
    print(f"Average load: {profiles['load_kw'].sum() * 0.25 / homes / days:.2f} kWh/day per home")
    print(f"Average PV:   {profiles['pv_kw'].sum() * 0.25 / homes / days:.2f} kWh/day per home")


if __name__ == "__main__":
    main()
