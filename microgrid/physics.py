"""Physical layer: SimPy applies the battery plans minute by minute (Phase 5).

Mesa decides, SimPy executes (Architecture 4.5):

- During its Mesa step an agent only *plans* its battery flow
  (``Battery.plan_charge`` / ``plan_discharge``) and trades the rest.
- After the market has settled, ``PhysicalSimulator.run_slot(t)`` runs SimPy
  slot ``t``: one short process per battery applies that step's plan in
  one-minute sub-steps, enforces the per-minute power limit and the SoC
  window, and applies capacity fade for the energy moved.

Time unit is the integer minute; slot ``t`` covers [t * slot, (t + 1) * slot)
with ``slot = 15`` for the default 96 steps per day.

A plan the battery cannot fully apply (in practice only when fade shrinks the
headroom inside the slot) is recorded as ``shortfall_kwh``; the agent already
traded as if it were applied, so ``MicrogridModel`` settles it with the
macro-grid in a second cash pass (Architecture 4.5, PSD 6.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import simpy

if TYPE_CHECKING:  # household.py does not import this module
    from microgrid.agents.household import HouseholdAgent

# Tolerance for float noise when checking limits.
_EPS = 1e-9


class SyncError(RuntimeError):
    """Mesa and SimPy clocks disagree, or a slot did not finish."""


@dataclass
class BatterySlotResult:
    """What one battery did in one slot (household side, kWh)."""

    agent_id: int
    planned_kwh: float  # >0 charge intake, <0 delivery (the agent's plan)
    applied_kwh: float  # same sign convention, what SimPy actually applied
    fade_loss_kwh: float  # stored energy clipped by capacity fade
    violations: int  # minutes outside the SoC window or above the power limit
    soc_end: float

    @property
    def shortfall_kwh(self) -> float:
        """Planned but not applied (>= 0); float noise from splitting the
        plan into minutes counts as fully applied."""
        missing = abs(self.planned_kwh) - abs(self.applied_kwh)
        return missing if missing > _EPS else 0.0


@dataclass
class SlotReport:
    step: int
    batteries: list[BatterySlotResult] = field(default_factory=list)

    @property
    def shortfall_kwh(self) -> float:
        return sum(r.shortfall_kwh for r in self.batteries)

    @property
    def fade_loss_kwh(self) -> float:
        return sum(r.fade_loss_kwh for r in self.batteries)

    @property
    def violations(self) -> int:
        return sum(r.violations for r in self.batteries)


class PhysicalSimulator:
    def __init__(self, *, step_hours: float) -> None:
        minutes = step_hours * 60
        if minutes < 1 or abs(minutes - round(minutes)) > _EPS:
            raise ValueError("a step must be a whole number of minutes")
        self.slot_minutes = int(round(minutes))
        self.env = simpy.Environment()
        self.reports: list[SlotReport] = []
        self.total_violations = 0

    def run_slot(self, step: int, agents: list[HouseholdAgent]) -> SlotReport:
        """Apply every battery plan of ``step``; blocks until the slot ends.

        ``agents`` are the households with a battery; each carries its plan
        in ``battery_flow_kwh``. Raises ``SyncError`` if the SimPy clock is
        not at the start of ``step`` or a battery process did not finish.
        """
        start = step * self.slot_minutes
        if self.env.now != start:
            raise SyncError(f"SimPy at minute {self.env.now}, Mesa step {step} starts at {start}")

        report = SlotReport(step=step)
        for agent in agents:
            self.env.process(self._battery_process(agent, report))
        self.env.run(until=start + self.slot_minutes)

        if self.env.now != start + self.slot_minutes:
            raise SyncError(f"slot {step} ended at minute {self.env.now}")
        if len(report.batteries) != len(agents):
            raise SyncError(
                f"slot {step}: {len(agents) - len(report.batteries)} battery process(es) unfinished"
            )
        self.total_violations += report.violations
        self.reports.append(report)
        return report

    def _battery_process(self, agent: HouseholdAgent, report: SlotReport):
        battery = agent.battery
        plan = agent.battery_flow_kwh
        minute_h = 1 / 60
        applied = fade_loss = 0.0
        violations = 0

        for minute in range(self.slot_minutes):
            # Spread what is left evenly over the minutes left, so a minute
            # that falls short is made up later when possible.
            remaining = abs(plan) - abs(applied)
            request = max(0.0, remaining / (self.slot_minutes - minute))
            before = battery.energy_kwh
            if plan > 0:
                moved = battery.charge(request, minute_h)
                applied += moved
                power_kw, limit_kw = moved / minute_h, battery.max_charge_kw
            elif plan < 0:
                moved = battery.discharge(request, minute_h)
                applied -= moved
                power_kw, limit_kw = moved / minute_h, battery.max_discharge_kw
            else:
                power_kw = limit_kw = 0.0
            fade_loss += battery.apply_fade(abs(battery.energy_kwh - before))

            soc = battery.soc
            if (
                soc < battery.soc_min - _EPS
                or soc > battery.soc_max + _EPS
                or power_kw > limit_kw + _EPS
            ):
                violations += 1

            if minute == self.slot_minutes - 1:
                # Record before the last wait: run(until=slot end) stops
                # without resuming this process at the end minute.
                report.batteries.append(BatterySlotResult(
                    agent_id=agent.unique_id,
                    planned_kwh=plan,
                    applied_kwh=applied,
                    fade_loss_kwh=fade_loss,
                    violations=violations,
                    soc_end=soc,
                ))
            yield self.env.timeout(1)
