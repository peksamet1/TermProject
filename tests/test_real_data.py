"""Tests for the raw-data pipeline, using tiny fake Ausgrid and PVGIS files."""

import json

import pandas as pd
import pytest

from microgrid.profiles.real_data import (
    build_profiles,
    half_hour_to_15min,
    read_ausgrid,
    read_pvgis,
    scale_to_annual,
)
from microgrid.profiles.timeseries import ProfileTable

SLOTS = [f"{h}:{m:02d}" for h in range(24) for m in (0, 30)][1:] + ["0:00"]


def write_fake_ausgrid(path):
    """Customer 1: GC 0.2 + CL 0.1 per half-hour; customer 2: GC 0.4;
    customer 3 has only one of the two days (incomplete)."""
    header = "Customer,Generator Capacity,Postcode,Consumption Category,date," + ",".join(SLOTS) + ",Row Quality"
    rows = []
    for date in ("01/07/2012", "02/07/2012"):
        rows.append(["1", "1.5", "2076", "GC", date] + ["0.2"] * 48 + [""])
        rows.append(["1", "1.5", "2076", "CL", date] + ["0.1"] * 48 + [""])
        rows.append(["1", "1.5", "2076", "GG", date] + ["9.9"] * 48 + [""])
        rows.append(["2", "2.0", "2076", "GC", date] + ["0.4"] * 48 + [""])
    rows.append(["3", "2.0", "2076", "GC", "01/07/2012"] + ["0.4"] * 48 + [""])
    lines = ["Solar home electricity data (title row)", header] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n")
    return path


def write_fake_pvgis(path):
    """One year, 1 kWp: 600 W from 10:00 to 15:00 Istanbul time, else 0."""
    hours = pd.date_range("2019-01-01", "2019-12-31 23:00", freq="h")  # UTC
    rows = [
        {"time": t.strftime("%Y%m%d:%H10"), "P": 600.0 if 10 <= (t.hour + 3) % 24 < 15 else 0.0}
        for t in hours
    ]
    path.write_text(json.dumps({"outputs": {"hourly": rows}}))
    return path


def test_read_ausgrid_sums_gc_and_cl_only(tmp_path):
    load = read_ausgrid(write_fake_ausgrid(tmp_path / "a.csv"))
    one = load[load["customer"] == 1]
    assert len(one) == 96  # 2 days x 48 half-hours
    assert one["load_kwh"].iloc[0] == pytest.approx(0.3)  # GG ignored
    assert one["timestamp"].iloc[0] == pd.Timestamp("2012-07-01 00:00")


def test_half_hour_split_conserves_energy(tmp_path):
    load = read_ausgrid(write_fake_ausgrid(tmp_path / "a.csv"))
    fine = half_hour_to_15min(load)
    assert len(fine) == 2 * len(load)
    assert fine["load_kwh"].sum() == pytest.approx(load["load_kwh"].sum())
    one = fine[fine["customer"] == 1]["timestamp"]
    assert (one.diff().dropna() == pd.Timedelta(minutes=15)).all()


def test_scale_keeps_ratio_and_hits_target(tmp_path):
    load = read_ausgrid(write_fake_ausgrid(tmp_path / "a.csv"))
    load = load[load["customer"].isin([1, 2])]
    scaled = scale_to_annual(load, annual_kwh=2400.0)
    per_home = scaled.groupby("customer")["load_kwh"].sum()
    assert per_home[2] / per_home[1] == pytest.approx(0.4 / 0.3)
    days = (load["timestamp"].max() - load["timestamp"].min()) / pd.Timedelta(days=1)
    assert per_home.mean() * 365 / days == pytest.approx(2400.0)


def test_read_pvgis_converts_to_local_kw_per_kwp(tmp_path):
    pv = read_pvgis(write_fake_pvgis(tmp_path / "p.json"))
    assert pv[pd.Timestamp("2019-06-01 12:00")] == pytest.approx(0.6)
    assert pv[pd.Timestamp("2019-06-01 03:00")] == 0.0


def test_read_pvgis_website_csv(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text(
        "Latitude (decimal degrees):\t41.01\n\n"
        "time,P,G(i),H_sun,T2m,WS10m,Int\n"
        "20190601:0910,500.0,600,50,20,2,0\n"
        "20190601:1010,700.0,800,60,21,2,0\n"
        "\nP: PV system power (W)\n"
    )
    pv = read_pvgis(path)
    assert list(pv.round(3)) == [0.5, 0.7]
    assert pv.index[0] == pd.Timestamp("2019-06-01 12:00")


def test_build_profiles_end_to_end(tmp_path):
    out = build_profiles(
        write_fake_ausgrid(tmp_path / "a.csv"), write_fake_pvgis(tmp_path / "p.json"),
        households=5, pv_peak_kw=5.0, seed=1,
    )
    # Customer 3 is incomplete and dropped; 2 homes x 2 days x 96 steps.
    assert sorted(out["household_id"].unique()) == ["H01", "H02"]
    assert len(out) == 2 * 2 * 96
    # Southern July becomes northern late December / January.
    assert out["timestamp"].min() == pd.Timestamp("2012-12-30 00:00")
    assert (out.loc[out["timestamp"].dt.hour < 6, "pv_kw"] == 0).all()
    assert out["pv_kw"].max() == pytest.approx(0.6 * 6.0, rel=0.21)

    path = tmp_path / "profiles.csv"
    out.to_csv(path, index=False)
    table = ProfileTable.from_csv(path, step_hours=0.25, steps_per_day=96)
    assert table.household_ids == ["H01", "H02"]
