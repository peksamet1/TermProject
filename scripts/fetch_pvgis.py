"""Download one year of hourly PV output for a 1 kWp system in Istanbul.

Usage: python scripts/fetch_pvgis.py [--year 2019] [--out data/raw/pvgis/istanbul_2019.json]

Source: PVGIS 5.3 (EU JRC), seriescalc API, SARAH-3 radiation database.
Settings: lat 41.01, lon 28.98, 1 kWp, 14% system loss, 30 deg tilt, south.
If the API is unreachable, use the website instead (see data/README.md).
"""

from __future__ import annotations

import argparse
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
ISTANBUL = {"lat": 41.01, "lon": 28.98}


def build_url(year: int) -> str:
    params = {
        **ISTANBUL,
        "pvcalculation": 1,
        "peakpower": 1,  # 1 kWp, so P (W) / 1000 = kW per kWp
        "loss": 14,
        "angle": 30,
        "aspect": 0,  # 0 = south
        "startyear": year,
        "endyear": year,
        "outputformat": "json",
    }
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", type=int, default=2019)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or Path(f"data/raw/pvgis/istanbul_{args.year}.json")

    url = build_url(args.year)
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Saved {len(data) / 1e6:.1f} MB to {out}")


if __name__ == "__main__":
    main()
