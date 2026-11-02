"""Common interface for every energy profile (synthetic or real data)."""

from __future__ import annotations

from typing import Protocol


class EnergyProfile(Protocol):
    def energy_at(self, step: int) -> float:
        """Energy (kWh) for the given simulation step."""
        ...
