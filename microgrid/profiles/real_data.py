"""Turn raw public datasets into the common profile CSV format.

Sources (download steps are in data/README.md):
- Ausgrid Solar Home Electricity Data: half-hourly household load (kWh),
  Sydney, columns ``Customer, ..., Consumption Category, date, 0:30 .. 0:00``.
  Load = GC (general consumption) + CL (controlled load, if present).
- PVGIS hourly series for Istanbul: PV power for a 1 kWp system (JSON from
  the API or CSV from the website).

Adaptation to Turkey (see the Phase 2 data plan):
1. half-hour kWh -> two equal 15-minute steps (energy is conserved);
2. scale all homes by one factor so the average is ~2,400 kWh/year
   (~200 kWh/month), keeping the differences between homes;
3. shift dates by 182 days (southern -> northern hemisphere seasons);
4. PV = PVGIS kW per kWp (linearly interpolated to 15 minutes)
   x each home's installed kWp (default 5 kW +-20%).
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

TURKEY_ANNUAL_LOAD_KWH = 2400.0
HEMISPHERE_SHIFT = pd.Timedelta(days=182)
ISTANBUL_UTC_OFFSET = pd.Timedelta(hours=3)

_TIME_COLUMN = re.compile(r"^\d{1,2}:\d{2}$")
_LOAD_CATEGORIES = ("GC", "CL")


# --- Ausgrid load --------------------------------------------------------

def read_ausgrid(path: str | Path) -> pd.DataFrame:
    """Ausgrid wide CSV -> long table (customer, timestamp, load_kwh).

    ``timestamp`` is the start of each half-hour. A title row above the
    ``Customer`` header is skipped; a ``Row Quality`` column is ignored.
    """
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    header_row = next((i for i, line in enumerate(lines) if line.startswith("Customer")), None)
    if header_row is None:
        raise ValueError(f"{path}: no 'Customer' header row; is this an Ausgrid file?")
    wide = pd.read_csv(io.StringIO("\n".join(lines[header_row:])))

    time_columns = [c for c in wide.columns if _TIME_COLUMN.match(str(c).strip())]
    if len(time_columns) != 48:
        raise ValueError(f"{path}: expected 48 half-hour columns, found {len(time_columns)}")

    wide = wide[wide["Consumption Category"].isin(_LOAD_CATEGORIES)]
    long = wide.melt(
        id_vars=["Customer", "date"], value_vars=time_columns,
        var_name="slot", value_name="load_kwh",
    )
    # Column k (0..47) covers the half-hour starting at k * 30 minutes.
    slot_index = long["slot"].map({c: k for k, c in enumerate(time_columns)})
    long["timestamp"] = (
        pd.to_datetime(long["date"], dayfirst=True) + pd.to_timedelta(slot_index * 30, unit="min")
    )
    # GC + CL for the same customer and half-hour.
    load = long.groupby(["Customer", "timestamp"], as_index=False)["load_kwh"].sum()
    return load.rename(columns={"Customer": "customer"})


def complete_customers(load: pd.DataFrame) -> list:
    """Customers that have a value for every half-hour in the file."""
    counts = load.groupby("customer")["timestamp"].nunique()
    return sorted(counts[counts == counts.max()].index)


def half_hour_to_15min(load: pd.DataFrame) -> pd.DataFrame:
    """Split each half-hour kWh into two 15-minute rows of half the energy."""
    first = load.assign(load_kwh=load["load_kwh"] / 2)
    second = first.assign(timestamp=first["timestamp"] + pd.Timedelta(minutes=15))
    return pd.concat([first, second]).sort_values(["customer", "timestamp"]).reset_index(drop=True)


def scale_to_annual(load: pd.DataFrame, annual_kwh: float = TURKEY_ANNUAL_LOAD_KWH) -> pd.DataFrame:
    """One common factor so the average home uses ``annual_kwh`` per year."""
    days = (load["timestamp"].max() - load["timestamp"].min()) / pd.Timedelta(days=1)
    per_home = load.groupby("customer")["load_kwh"].sum() * 365.0 / max(days, 1.0)
    factor = annual_kwh / per_home.mean()
    return load.assign(load_kwh=load["load_kwh"] * factor)


# --- PVGIS PV ------------------------------------------------------------

def read_pvgis(path: str | Path) -> pd.Series:
    """PVGIS hourly output -> kW per kWp indexed by Istanbul local time.

    Accepts the API JSON (``outputs.hourly``) or the website CSV export.
    PVGIS times are UTC like ``20190101:0010``; P is in W for the chosen
    peak power, so the files must be generated with peakpower=1.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        rows = json.loads(text)["outputs"]["hourly"]
        df = pd.DataFrame(rows)[["time", "P"]]
    else:
        lines = text.splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("time,")), None)
        if start is None:
            raise ValueError(f"{path}: no 'time,' header row; is this a PVGIS hourly CSV?")
        body = []
        for line in lines[start:]:
            if not line.strip():
                break  # PVGIS appends notes after a blank line
            body.append(line)
        df = pd.read_csv(io.StringIO("\n".join(body)))[["time", "P"]]

    utc = pd.to_datetime(df["time"].astype(str), format="%Y%m%d:%H%M")
    local_hour = (utc + ISTANBUL_UTC_OFFSET).dt.floor("h")
    return pd.Series(df["P"].to_numpy(dtype=float) / 1000.0, index=local_hour, name="pv_kw_per_kwp")


def pv_to_15min(pv_hourly: pd.Series) -> pd.Series:
    """Linear interpolation of hourly kW values to 15-minute steps."""
    pv_hourly = pv_hourly[~pv_hourly.index.duplicated()].sort_index()
    grid = pd.date_range(pv_hourly.index[0], pv_hourly.index[-1] + pd.Timedelta(minutes=45),
                         freq="15min")
    return pv_hourly.reindex(grid).interpolate(method="time", limit_direction="both").clip(lower=0)


def pv_lookup(pv_15min: pd.Series, timestamps: pd.Series) -> np.ndarray:
    """PV for any timestamp by (day of year, time of day) of the PVGIS year."""
    key = lambda ts: ts.dt.dayofyear.clip(upper=365) * 1440 + ts.dt.hour * 60 + ts.dt.minute
    table = pd.Series(pv_15min.to_numpy(), index=key(pv_15min.index.to_series()))
    table = table[~table.index.duplicated()]
    return table.reindex(key(timestamps)).fillna(0.0).to_numpy()


# --- Combine -------------------------------------------------------------

def build_profiles(
    ausgrid_path: str | Path,
    pvgis_path: str | Path,
    *,
    households: int = 20,
    pv_peak_kw: float = 5.0,
    annual_load_kwh: float = TURKEY_ANNUAL_LOAD_KWH,
    seed: int = 42,
) -> pd.DataFrame:
    """Full pipeline -> DataFrame in the common profile CSV format."""
    load = read_ausgrid(ausgrid_path)
    customers = complete_customers(load)[:households]
    if not customers:
        raise ValueError(f"{ausgrid_path}: no customer has complete data")
    load = load[load["customer"].isin(customers)]
    load = scale_to_annual(half_hour_to_15min(load), annual_load_kwh)
    load["timestamp"] = load["timestamp"] + HEMISPHERE_SHIFT

    rng = np.random.default_rng(seed)
    kwp = {c: pv_peak_kw * float(rng.uniform(0.8, 1.2)) for c in customers}
    pv_per_kwp = pv_lookup(pv_to_15min(read_pvgis(pvgis_path)), load["timestamp"])

    ids = {c: f"H{n:02d}" for n, c in enumerate(customers, start=1)}
    out = pd.DataFrame({
        "timestamp": load["timestamp"].to_numpy(),
        "household_id": load["customer"].map(ids).to_numpy(),
        "pv_kw": (pv_per_kwp * load["customer"].map(kwp).to_numpy()).round(4),
        # kWh per 15 minutes -> average kW
        "load_kw": (load["load_kwh"].to_numpy() / 0.25).round(4),
    })
    return out.sort_values(["household_id", "timestamp"]).reset_index(drop=True)
