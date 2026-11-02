"""Profiles backed by a time-series CSV (real or pre-generated data).

Common CSV format (one row per household per time step):

    timestamp,household_id,pv_kw,load_kw
    2024-06-21 00:00:00,H01,0.0,0.21

- ``pv_kw`` / ``load_kw`` are average power (kW) over the step.
- Every household must cover the same timestamps at a fixed spacing that
  equals the simulation step (15 minutes by default).
- Agent ``i`` uses household ``i mod k`` (k = households in the file), and
  a series that runs out wraps around to its first step.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from microgrid.profiles.consumption import ConsumptionProfile
from microgrid.profiles.solar import SolarProfile

CSV_COLUMNS = ("timestamp", "household_id", "pv_kw", "load_kw")


class ProfileDataError(ValueError):
    """The profile CSV is missing, malformed or too short."""


class TimeSeriesProfile:
    """Replays a fixed series of average-power values (kW) as kWh per step."""

    def __init__(self, values_kw: np.ndarray, step_hours: float) -> None:
        if len(values_kw) == 0:
            raise ProfileDataError("time series is empty")
        self.values_kw = np.asarray(values_kw, dtype=float)
        self.step_hours = step_hours

    def __len__(self) -> int:
        return len(self.values_kw)

    def energy_at(self, step: int) -> float:
        """Energy (kWh) during the step; wraps around when data runs out."""
        return float(self.values_kw[step % len(self.values_kw)]) * self.step_hours


class ProfileTable:
    """All households of one profile CSV, as per-household kW arrays."""

    def __init__(self, pv_kw: dict[str, np.ndarray], load_kw: dict[str, np.ndarray],
                 step_hours: float) -> None:
        self.household_ids = sorted(pv_kw)
        self.pv_kw = pv_kw
        self.load_kw = load_kw
        self.step_hours = step_hours

    @classmethod
    def from_csv(cls, path: str | Path, *, step_hours: float, steps_per_day: int,
                 start: str | None = None) -> "ProfileTable":
        """Read and validate a profile CSV.

        ``start`` (e.g. "2024-01-15") drops rows before that timestamp.
        At least one full day of data must remain for every household.
        """
        path = Path(path)
        if not path.is_file():
            raise ProfileDataError(f"profile CSV not found: {path}")

        df = pd.read_csv(path)
        missing = [c for c in CSV_COLUMNS if c not in df.columns]
        if missing:
            raise ProfileDataError(f"{path}: missing columns {missing}; expected {list(CSV_COLUMNS)}")

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["household_id"] = df["household_id"].astype(str)
        for column in ("pv_kw", "load_kw"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        if df[list(CSV_COLUMNS)].isna().any().any():
            raise ProfileDataError(f"{path}: empty or non-numeric values found")
        if (df[["pv_kw", "load_kw"]] < 0).any().any():
            raise ProfileDataError(f"{path}: pv_kw and load_kw must be non-negative")

        if start is not None:
            df = df[df["timestamp"] >= pd.Timestamp(start)]

        step = pd.Timedelta(hours=step_hours)
        pv_kw: dict[str, np.ndarray] = {}
        load_kw: dict[str, np.ndarray] = {}
        for household_id, group in df.groupby("household_id"):
            group = group.sort_values("timestamp")
            if len(group) < steps_per_day:
                raise ProfileDataError(
                    f"{path}: household {household_id} has {len(group)} steps, "
                    f"need at least one day ({steps_per_day})"
                )
            gaps = group["timestamp"].diff().dropna()
            if not (gaps == step).all():
                raise ProfileDataError(
                    f"{path}: household {household_id} timestamps must be evenly "
                    f"spaced every {step} with no gaps or duplicates"
                )
            pv_kw[household_id] = group["pv_kw"].to_numpy()
            load_kw[household_id] = group["load_kw"].to_numpy()

        if not pv_kw:
            raise ProfileDataError(f"{path}: no rows left (check profile_start)")
        return cls(pv_kw, load_kw, step_hours)

    def _household(self, index: int) -> str:
        return self.household_ids[index % len(self.household_ids)]

    def solar_profile(self, index: int) -> TimeSeriesProfile:
        return TimeSeriesProfile(self.pv_kw[self._household(index)], self.step_hours)

    def consumption_profile(self, index: int) -> TimeSeriesProfile:
        return TimeSeriesProfile(self.load_kw[self._household(index)], self.step_hours)


def generate_synthetic_csv(
    path: str | Path,
    *,
    households: int,
    days: int,
    seed: int = 42,
    steps_per_day: int = 96,
    pv_peak_kw: float = 5.0,
    start_date: str = "2024-06-21",
) -> Path:
    """Write synthetic profiles in the common CSV format.

    Uses the same generators as profile_source="synthetic", so mock and real
    data can be swapped through one code path.
    """
    rng = np.random.default_rng(seed)
    step_hours = 24.0 / steps_per_day
    start = pd.Timestamp(start_date)
    timestamps = pd.date_range(start, periods=steps_per_day * days, freq=pd.Timedelta(hours=step_hours))

    frames = []
    for i in range(households):
        solar = SolarProfile(
            peak_kw=pv_peak_kw * float(rng.uniform(0.8, 1.2)),
            step_hours=step_hours,
            steps_per_day=steps_per_day,
            rng=rng,
            start_day_of_year=min(start.dayofyear, 365),
        )
        load = ConsumptionProfile(
            step_hours=step_hours,
            steps_per_day=steps_per_day,
            rng=rng,
            scale=float(rng.uniform(0.7, 1.3)),
        )
        steps = range(len(timestamps))
        frames.append(pd.DataFrame({
            "timestamp": timestamps,
            "household_id": f"H{i + 1:02d}",
            # kWh per step -> average kW over the step
            "pv_kw": [round(solar.energy_at(s) / step_hours, 4) for s in steps],
            "load_kw": [round(load.energy_at(s) / step_hours, 4) for s in steps],
        }))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(frames).to_csv(path, index=False)
    return path
