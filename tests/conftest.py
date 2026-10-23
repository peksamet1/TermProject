"""Shared fixtures and helpers for deterministic tests."""

from __future__ import annotations

import numpy as np
import pytest

from microgrid.config import SimulationConfig, TariffConfig
from microgrid.agents.strategy import TradingStrategy


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: heavy scale runs (30 / 50 nodes); skipped unless run with -m slow"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # The default run stays fast, or nobody runs it: slow tests only run when
    # the -m expression asks for them (pytest -m slow).
    if "slow" in (config.option.markexpr or ""):
        return
    skip_slow = pytest.mark.skip(reason="slow: run with pytest -m slow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


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
