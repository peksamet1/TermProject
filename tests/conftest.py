"""Shared fixtures and helpers for deterministic tests."""

from __future__ import annotations

import numpy as np
import pytest

from microgrid.config import SimulationConfig, TariffConfig
from microgrid.agents.strategy import TradingStrategy


class ConstantProfile:
    """Hand-written profile stub: returns the same kWh at every step.

    Duck-types SolarProfile / ConsumptionProfile so agent tests do not depend
    on synthetic randomness.
    """

    def __init__(self, kwh: float) -> None:
        self.kwh = kwh

    def energy_at(self, step: int) -> float:
        return self.kwh


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(0)


@pytest.fixture
def config() -> SimulationConfig:
    return SimulationConfig()


@pytest.fixture
def strategy() -> TradingStrategy:
    return TradingStrategy(tariff=TariffConfig())
