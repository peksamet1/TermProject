"""Interactive SolaraViz dashboard (a development panel, not a public app).

Usage: solara run app.py

Two tabs:

- Simulation: the live run. Community totals and the selected household
  share this page. A day and a time pick one recorded step.
  Does not open ZeroMQ sockets.
- Saved analysis: the season comparison written by
  ``python -m experiments.analyze``. The panel does not recompute it.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pandas as pd
import solara
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from mesa.visualization import SolaraViz
from mesa.visualization import solara_viz as mesa_viz
from mesa.visualization.utils import update_counter

from microgrid.agents.household import ProsumerAgent
from microgrid.metrics import (
    balance_error_kwh,
    community_cost_tl,
    demand_fill_rate,
    grid_dependence,
    trade_success_rate,
)
from microgrid.model import MicrogridModel

_RESULTS = Path(__file__).parent / "results"
ANALYSIS_DIR = _RESULTS / "analysis"
ANALYSIS_SUMMARY = ANALYSIS_DIR / "summary.csv"
ISTANBUL_DIR = _RESULTS / "analysis_istanbul"
ISTANBUL_SUMMARY = ISTANBUL_DIR / "summary.csv"
_SENSITIVITY = (
    ("sensitivity_battery_kwh.png", "Battery capacity"),
    ("sensitivity_prosumer_share.png", "Prosumer share"),
    ("sensitivity_pv_kw.png", "PV capacity"),
    ("sensitivity_retail_tl.png", "Retail price"),
)

_GEN = "#e39b2b"
_LOAD = "#3d7ec9"
_LOCAL = "#2f9e6b"
_IMPORT = "#c4524a"
_EXPORT = "#7a6bb5"
_SOC = "#6b7280"
_GRID_ONLY = "#8aa0b8"
_LOCAL_MARKET = "#2a78d6"

model_params = {
    "num_prosumers": {
        "type": "SliderInt",
        "value": 6,
        "min": 1,
        "max": 30,
        "step": 1,
        "label": "Prosumers",
    },
    "num_consumers": {
        "type": "SliderInt",
        "value": 4,
        "min": 0,
        "max": 30,
        "step": 1,
        "label": "Consumers",
    },
    "pv_peak_kw": {
        "type": "SliderFloat",
        "value": 5.0,
        "min": 1.0,
        "max": 15.0,
        "step": 0.5,
        "label": "PV capacity (kW)",
    },
    "battery_capacity_kwh": {
        "type": "SliderFloat",
        "value": 10.0,
        "min": 2.0,
        "max": 30.0,
        "step": 1.0,
        "label": "Battery capacity (kWh)",
    },
    "start_day_of_year": {
        "type": "SliderInt",
        "value": 172,
        "min": 1,
        "max": 365,
        "step": 1,
        "label": "Day of year (172 summer, 355 winter)",
    },
    "market_backend": {
        "type": "Select",
        "value": "cda",
        "values": ["cda", "logging"],
        "label": "Market",
    },
    "seed": {
        "type": "SliderInt",
        "value": 42,
        "min": 1,
        "max": 99,
        "step": 1,
        "label": "Seed",
    },
}

_PAGE_NAMES = {0: "Simulation", 1: "Saved analysis"}
_STATE = {"IDLE": "Idle", "BUYING": "Buying", "SELLING": "Selling"}
_SEASON = {"summer": "Summer", "winter": "Winter"}
_BACKEND = {"logging": "grid only", "cda": "local market"}


def _style_ax(ax, ylabel: str, xlabel: str = "Step") -> None:
    ax.set_ylabel(ylabel)
    ax.set_xlabel(xlabel)
    ax.grid(True, axis="y", alpha=0.28)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(labelsize=8)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    if ax.lines:
        xs = ax.lines[0].get_xdata()
        if len(xs) == 1:
            x = float(xs[0])
            ax.set_xlim(x - 0.5, x + 0.5)
            ax.set_xticks([x])


def _figure(width: float, height: float) -> tuple[Figure, object]:
    fig = Figure(figsize=(width, height), layout="constrained")
    fig.patch.set_facecolor("white")
    return fig, fig.subplots()


def moment_text(profile_step: int, steps_per_day: int, start_day_of_year: int) -> str:
    """Clock for one recorded step. Step 0 is 00:00 on the start day; step 52 is 13:00."""
    day_number = profile_step // steps_per_day + 1
    slot = profile_step % steps_per_day
    minutes = slot * (24 * 60 // steps_per_day)
    hour, minute = divmod(minutes, 60)
    day_of_year = (start_day_of_year - 1 + profile_step // steps_per_day) % 365 + 1
    return (
        f"Day {day_number} · {hour:02d}:{minute:02d} · "
        f"day of year {day_of_year} · step {profile_step}"
    )


def _mark(ax, at: float | None) -> None:
    """Vertical line at the step the panel is showing."""
    if at is None:
        return
    ax.axvline(float(at), color="#111827", linewidth=1.0, alpha=0.45, zorder=0)


def _agent_history(model: MicrogridModel):
    """Return agent history as MultiIndex (Step, AgentID), or None if none yet.

    The copy is taken under the model's history lock, so Play can be writing
    the next step while this page draws.
    """
    records, names = model.copy_agent_records()
    if not records:
        return None
    return pd.DataFrame.from_records(
        records,
        columns=["Step", "AgentID", *names],
        index=["Step", "AgentID"],
    )


def _soc_text(agent) -> str:
    if agent.battery is None or agent.soc != agent.soc:  # NaN
        return "—"
    return f"{agent.soc:.2f}"


def _community_figures(frame: pd.DataFrame, at: float | None = None) -> tuple[Figure, Figure]:
    steps = frame.index.to_numpy()
    energy, ax_energy = _figure(6.4, 3.1)
    ax_energy.plot(steps, frame["TotalGeneration"], label="Generation", color=_GEN, linewidth=1.6)
    ax_energy.plot(steps, frame["TotalConsumption"], label="Consumption", color=_LOAD, linewidth=1.6)
    ax_energy.fill_between(steps, frame["LocalTradedKwh"], color=_LOCAL, alpha=0.25, label="Local trade")
    _style_ax(ax_energy, "kWh / step")
    _mark(ax_energy, at)
    ax_energy.set_title("Community energy")
    ax_energy.legend(loc="upper right", fontsize=8, frameon=False)

    grid, ax_grid = _figure(6.4, 3.1)
    ax_grid.plot(steps, frame["GridImport"], label="Grid import", color=_IMPORT, linewidth=1.6)
    ax_grid.plot(steps, frame["GridExport"], label="Grid export", color=_EXPORT, linewidth=1.6)
    _style_ax(ax_grid, "kWh / step")
    _mark(ax_grid, at)
    ax_grid.set_title("Grid flow")
    soc_ax = ax_grid.twinx()
    soc_ax.plot(steps, frame["AvgProsumerSoC"], label="Mean SoC", color=_SOC, linestyle="--", linewidth=1.4)
    soc_ax.set_ylabel("SoC")
    soc_ax.set_ylim(0.0, 1.0)
    soc_ax.spines["top"].set_visible(False)
    handles, labels = ax_grid.get_legend_handles_labels()
    extra, extra_labels = soc_ax.get_legend_handles_labels()
    ax_grid.legend(handles + extra, labels + extra_labels, loc="upper right", fontsize=8, frameon=False)
    return energy, grid


def _market_figures(frame: pd.DataFrame, at: float | None = None) -> tuple[Figure, Figure]:
    steps = frame.index.to_numpy()
    orders, ax_orders = _figure(6.4, 2.8)
    ax_orders.plot(steps, frame["BuyOrders"], label="Buy orders", color=_IMPORT, linewidth=1.6)
    ax_orders.plot(steps, frame["SellOrders"], label="Sell orders", color=_LOCAL, linewidth=1.6)
    _style_ax(ax_orders, "Orders")
    _mark(ax_orders, at)
    ax_orders.set_title("Orders")
    ax_orders.legend(loc="upper right", fontsize=8, frameon=False)

    price, ax_price = _figure(6.4, 2.8)
    ax_price.plot(steps, frame["AvgTradePrice"], color=_LOAD, linewidth=1.6)
    _style_ax(ax_price, "TL/kWh")
    _mark(ax_price, at)
    ax_price.set_title("Average clearing price")
    return orders, price


def _household_figure(agent, frame: pd.DataFrame, at: float | None = None) -> Figure:
    # Agent rows are stored at Mesa time, which is one ahead of the profile step.
    steps = frame.index.to_numpy().astype(float) - 1.0
    fig, ax = _figure(10.2, 3.6)
    ax.plot(steps, frame["Generation"], label="Generation", color=_GEN, linewidth=1.6)
    ax.plot(steps, frame["Consumption"], label="Consumption", color=_LOAD, linewidth=1.6)
    ax.plot(steps, frame["BatteryFlow"], label="Battery", color=_EXPORT, linewidth=1.4)
    ax.plot(steps, frame["Residual"], label="Residual", color=_LOCAL, linewidth=1.4)
    _style_ax(ax, "kWh / step")
    _mark(ax, at)
    ax.set_title(agent.label)
    handles, labels = ax.get_legend_handles_labels()
    if agent.battery is not None:
        soc_ax = ax.twinx()
        soc_ax.plot(steps, frame["SoC"], label="SoC", color=_SOC, linestyle="--", linewidth=1.3)
        soc_ax.set_ylabel("SoC")
        soc_ax.set_ylim(0.0, 1.0)
        soc_ax.spines["top"].set_visible(False)
        extra, extra_labels = soc_ax.get_legend_handles_labels()
        handles += extra
        labels += extra_labels
    ax.legend(handles, labels, loc="upper right", fontsize=8, frameon=False)
    return fig


def _household_table(model: MicrogridModel) -> str:
    lines = [
        "| Household | State | SoC | Cash (TL) | Residual (kWh) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for agent in model.agents:
        lines.append(
            f"| {agent.label} | {_STATE.get(agent.state.value, agent.state.value)} | "
            f"{_soc_text(agent)} | {agent.cash_tl:.2f} | {agent.residual_kwh:.2f} |"
        )
    return "\n".join(lines)


def slice_history(
    model_df: pd.DataFrame, agent_df: pd.DataFrame, index: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """History through profile step ``index``. Agent rows are stored one ahead."""
    steps = agent_df.index.get_level_values("Step")
    return model_df.iloc[: index + 1], agent_df.loc[steps <= (index + 1)]


def report_kpis(model_df: pd.DataFrame, agent_df: pd.DataFrame) -> dict[str, float]:
    """The single-run report figures. Reduction needs a paired baseline, so it is absent."""
    return {
        "demand_fill_rate": demand_fill_rate(model_df, agent_df),
        "trade_success_rate": trade_success_rate(model_df, agent_df),
        "community_cost_tl": community_cost_tl(agent_df),
        "violations": float(model_df["ConstraintViolations"].sum()),
        "balance_error_kwh": balance_error_kwh(model_df, agent_df),
    }


def _rate_text(value: float) -> str:
    if pd.isna(value):
        return "—"
    return f"{value:.3f}"


def _rows_at(agent_df: pd.DataFrame | None, index: int) -> pd.DataFrame | None:
    """Household rows for profile step ``index``. Mesa stores them at time index + 1."""
    if agent_df is None or len(agent_df) == 0:
        return None
    try:
        return agent_df.xs(float(index + 1), level="Step")
    except KeyError:
        return None


def _history_table(rows: pd.DataFrame) -> str:
    lines = [
        "| Household | State | SoC | Cash (TL) | Residual (kWh) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for _agent_id, row in rows.sort_index().iterrows():
        soc = row["SoC"]
        soc_text = "—" if pd.isna(soc) else f"{float(soc):.2f}"
        state = _STATE.get(row["State"], row["State"])
        lines.append(
            f"| {row['Label']} | {state} | {soc_text} | "
            f"{float(row['CashTL']):.2f} | {float(row['Residual']):.2f} |"
        )
    return "\n".join(lines)


@solara.component
def HouseholdPlay(agent, set_refresh):
    """Sliders for the selected home. A new model starts them at 1 and the initial SoC."""
    prosumer = isinstance(agent, ProsumerAgent)
    battery = agent.battery

    def on_generation(value: float) -> None:
        agent.generation_scale = float(value)
        set_refresh(lambda n: n + 1)

    def on_load(value: float) -> None:
        agent.load_scale = float(value)
        set_refresh(lambda n: n + 1)

    def on_soc(value: float) -> None:
        battery.set_soc(float(value))
        set_refresh(lambda n: n + 1)

    with solara.Column():
        solara.SliderFloat(
            "Generation scale",
            value=agent.generation_scale,
            min=0.0,
            max=3.0,
            step=0.1,
            on_value=on_generation,
            disabled=not prosumer,
        )
        solara.SliderFloat(
            "Load scale",
            value=agent.load_scale,
            min=0.0,
            max=3.0,
            step=0.1,
            on_value=on_load,
        )
        if battery is None:
            solara.SliderFloat("State of charge", value=0.0, min=0.0, max=1.0, step=0.01, disabled=True)
        else:
            solara.SliderFloat(
                "State of charge",
                value=battery.soc,
                min=battery.soc_min,
                max=battery.soc_max,
                step=0.01,
                on_value=on_soc,
            )


@solara.component
def Simulation(model: MicrogridModel):
    """Community totals and one household, on the same page.

    Day and time pick one recorded step. Latest follows the run. The household
    sliders stay on the live step, because they change the next step, not the past.
    """
    update_counter.get()
    _refresh, set_refresh = solara.use_state(0)
    selected, set_selected = solara.use_state("Prosumer-1")
    follow, set_follow = solara.use_state(True)
    view_day, set_view_day = solara.use_state(1)
    view_slot, set_view_slot = solara.use_state(0)
    previous_n = solara.use_ref(0)
    frame = pd.DataFrame(model.copy_model_vars())
    n = 0 if frame is None else len(frame)
    has_steps = n > 0
    spd = model.config.steps_per_day

    def watch_history() -> None:
        if n < previous_n.current:
            set_follow(True)
        previous_n.current = n

    solara.use_effect(watch_history, [n])

    if has_steps and follow:
        index = n - 1
    elif has_steps:
        n_days_now = (n - 1) // spd + 1
        day = min(max(view_day, 1), n_days_now)
        last_slot = (n - 1) % spd if day == n_days_now else spd - 1
        slot = min(max(view_slot, 0), last_slot)
        index = (day - 1) * spd + slot
    else:
        index = 0
    shown_day = index // spd + 1 if has_steps else 1
    shown_slot = index % spd if has_steps else 0
    n_days = (n - 1) // spd + 1 if has_steps else 1
    time_max = ((n - 1) % spd if shown_day == n_days else spd - 1) if has_steps else 0

    def on_day(value: int | None) -> None:
        if value is None:
            return
        set_follow(False)
        set_view_day(int(value))
        set_view_slot(shown_slot)

    def on_slot(value: int) -> None:
        set_follow(False)
        set_view_day(shown_day)
        set_view_slot(int(value))

    def go_latest() -> None:
        set_follow(True)

    solara.Markdown(
        "Pick a day and a time to read that step. Each step is 15 minutes. "
        "The charts keep the whole run; the dark line is the time you picked. "
        "The table under the charts is the household you select."
    )
    if not has_steps:
        solara.Markdown("No steps yet. Press Step to fill the charts.")
    else:
        clock = moment_text(index, spd, model.config.start_day_of_year)
        following = " · following the latest step" if follow else ""
        solara.Markdown(f"**{clock}**{following}")
        hour, minute = divmod(shown_slot * (24 * 60 // spd), 60)
        with solara.Columns([1, 2, 1]):
            solara.InputInt("Day", value=shown_day, on_value=on_day)
            solara.SliderInt(
                f"Time of day · {hour:02d}:{minute:02d}",
                value=shown_slot,
                min=0,
                max=max(time_max, 0),
                step=1,
                on_value=on_slot,
            )
            solara.Button("Latest", on_click=go_latest, disabled=follow)

        step_row = frame.iloc[index]
        so_far = frame.iloc[: index + 1]
        dependence = grid_dependence(so_far)
        dep_text = "—" if pd.isna(dependence) else f"{100 * dependence:.1f}%"
        price = step_row["AvgTradePrice"]
        price_text = "—" if pd.isna(price) else f"{float(price):.2f} TL/kWh"
        mean_soc = step_row["AvgProsumerSoC"]
        mean_soc_text = "—" if pd.isna(mean_soc) else f"{float(mean_soc):.2f}"
        solara.Markdown("**This step**")
        with solara.Columns([1, 1, 1, 1]):
            solara.Markdown(f"**Generation**\n\n{float(step_row['TotalGeneration']):.2f} kWh")
            solara.Markdown(f"**Consumption**\n\n{float(step_row['TotalConsumption']):.2f} kWh")
            solara.Markdown(f"**Grid import**\n\n{float(step_row['GridImport']):.2f} kWh")
            solara.Markdown(f"**Grid export**\n\n{float(step_row['GridExport']):.2f} kWh")
        with solara.Columns([1, 1, 1, 1]):
            solara.Markdown(f"**Local trade**\n\n{float(step_row['LocalTradedKwh']):.2f} kWh")
            solara.Markdown(f"**Mean SoC**\n\n{mean_soc_text}")
            solara.Markdown(f"**Clearing price**\n\n{price_text}")
            solara.Markdown(f"**Dependence so far**\n\n{dep_text}")
        solara.Markdown("**Through this step**")
        with solara.Columns([1, 1, 1]):
            solara.Markdown(f"**Grid import**\n\n{float(so_far['GridImport'].sum()):.2f} kWh")
            solara.Markdown(f"**Grid export**\n\n{float(so_far['GridExport'].sum()):.2f} kWh")
            solara.Markdown(f"**Local trade**\n\n{float(so_far['LocalTradedKwh'].sum()):.2f} kWh")
        agent_df = _agent_history(model)
        if agent_df is not None:
            _model_part, agent_part = slice_history(so_far, agent_df, index)
            kpis = report_kpis(_model_part, agent_part)
            with solara.Columns([1, 1, 1, 1, 1]):
                solara.Markdown(f"**Demand fill**\n\n{_rate_text(kpis['demand_fill_rate'])}")
                solara.Markdown(f"**Trade success**\n\n{_rate_text(kpis['trade_success_rate'])}")
                solara.Markdown(f"**Community cost**\n\n{kpis['community_cost_tl']:.2f} TL")
                solara.Markdown(f"**Violations**\n\n{int(kpis['violations'])}")
                solara.Markdown(f"**Balance error**\n\n{kpis['balance_error_kwh']:.3e} kWh")
            solara.Markdown(
                "Reduction and saving come from the paired runs on Saved analysis. "
                "This page is one market."
            )
        energy, grid = _community_figures(frame, at=float(index))
        orders, price_fig = _market_figures(frame, at=float(index))
        with solara.Columns([1, 1]):
            solara.FigureMatplotlib(energy)
            solara.FigureMatplotlib(grid)
        with solara.Columns([1, 1]):
            solara.FigureMatplotlib(orders)
            solara.FigureMatplotlib(price_fig)

    agents = list(model.agents)
    if not agents:
        solara.Markdown("This model has no households.")
        return
    labels = [agent.label for agent in agents]
    current = selected if selected in labels else labels[0]
    agent = next(item for item in agents if item.label == current)
    viewing_past = has_steps and index < n - 1
    if not has_steps:
        agent_df = None
    rows = _rows_at(agent_df, index) if viewing_past else None
    if rows is not None:
        solara.Markdown(_history_table(rows))
    else:
        solara.Markdown(_household_table(model))
    with solara.Columns([1, 2]):
        solara.Select("Household", value=current, values=labels, on_value=set_selected)
        if viewing_past:
            solara.Markdown(
                "Generation, load, and state of charge edit the live household. "
                "Press Latest to change them."
            )
        else:
            HouseholdPlay(agent, set_refresh)
    if rows is not None and agent.unique_id in rows.index:
        row = rows.loc[agent.unique_id]
        soc = row["SoC"]
        soc_text = "—" if pd.isna(soc) else f"{float(soc):.2f}"
        state = _STATE.get(row["State"], row["State"])
        solara.Markdown(
            f"**{row['Label']}** · {state} · SoC {soc_text} · "
            f"cash {float(row['CashTL']):.2f} TL · "
            f"generation {float(row['Generation']):.2f} kWh · "
            f"load {float(row['Consumption']):.2f} kWh · "
            f"residual {float(row['Residual']):.2f} kWh"
        )
    else:
        solara.Markdown(
            f"**{agent.label}** · {_STATE.get(agent.state.value, agent.state.value)} · "
            f"SoC {_soc_text(agent)} · cash {agent.cash_tl:.2f} TL · "
            f"generation scale {agent.generation_scale:.1f} · load scale {agent.load_scale:.1f}"
        )
    if agent_df is None:
        solara.Markdown("No steps yet. This household's chart fills in as you step.")
        return
    level = "AgentID" if "AgentID" in (agent_df.index.names or []) else 1
    home = agent_df.xs(agent.unique_id, level=level)
    if len(home) == 0:
        solara.Markdown("No steps yet. This household's chart fills in as you step.")
        return
    solara.FigureMatplotlib(_household_figure(agent, home, at=float(index) if has_steps else None))


def _season_rows(frame: pd.DataFrame) -> pd.DataFrame:
    seasons = frame[(frame["sweep"] == "season") & (frame["backend"].isin(["logging", "cda"]))].copy()
    order = {"summer": 0, "winter": 1, "logging": 0, "cda": 1}
    seasons["_order"] = seasons["season"].map(order) * 2 + seasons["backend"].map(order)
    return seasons.sort_values("_order")


def _season_report(seasons: pd.DataFrame) -> str:
    """One row per season: grid-only dependence, then the local-market result."""
    lines = [
        "| Season | Grid only | Local market | Reduction | Demand fill | Saving |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for season in ("summer", "winter"):
        grid = seasons[(seasons["season"] == season) & (seasons["backend"] == "logging")]
        local = seasons[(seasons["season"] == season) & (seasons["backend"] == "cda")]
        if grid.empty or local.empty:
            continue
        base = grid.iloc[0]
        peer = local.iloc[0]
        lines.append(
            f"| {_SEASON.get(season, season)} | "
            f"{100 * base.grid_dependence_mean:.1f}% ± {100 * base.grid_dependence_std:.1f} | "
            f"{100 * peer.grid_dependence_mean:.1f}% ± {100 * peer.grid_dependence_std:.1f} | "
            f"{peer.dependence_reduction_pct_mean:.1f}% ± {peer.dependence_reduction_pct_std:.1f} | "
            f"{peer.demand_fill_rate_mean:.3f} ± {peer.demand_fill_rate_std:.3f} | "
            f"{peer.cost_saving_tl_mean:.1f} ± {peer.cost_saving_tl_std:.1f} TL |"
        )
    return "\n".join(lines)


def _sensitivity_images(directory: Path) -> None:
    shown = False
    for name, title in _SENSITIVITY:
        path = directory / name
        if not path.is_file():
            continue
        if not shown:
            solara.Markdown("**Sensitivity**")
            shown = True
        solara.Markdown(title)
        solara.Image(path)


def _saved_source(title: str, summary: Path, directory: Path) -> None:
    if not summary.is_file():
        return
    frame = pd.read_csv(summary)
    seasons = _season_rows(frame)
    solara.Markdown(f"**{title}**")
    if seasons.empty:
        solara.Markdown("summary.csv has no season rows.")
        return
    solara.FigureMatplotlib(_analysis_figure(seasons))
    solara.Markdown(_season_report(seasons))
    _sensitivity_images(directory)


def _analysis_figure(seasons: pd.DataFrame) -> Figure:
    labels = [
        f"{_SEASON.get(row.season, row.season)}\n{_BACKEND.get(row.backend, row.backend)}"
        for row in seasons.itertuples()
    ]
    colors = [_GRID_ONLY if row.backend == "logging" else _LOCAL_MARKET for row in seasons.itertuples()]
    fig, ax = _figure(8.2, 4.2)
    bars = ax.bar(
        labels,
        seasons["grid_dependence_mean"] * 100,
        color=colors,
        yerr=seasons["grid_dependence_std"] * 100,
        capsize=4,
        width=0.72,
    )
    ax.bar_label(bars, fmt="%.1f", padding=3, fontsize=8)
    _style_ax(ax, "Grid dependence (%)")
    ax.set_title("Saved run: mean grid dependence")
    ax.set_ylim(0, max(float((seasons["grid_dependence_mean"] + seasons["grid_dependence_std"]).max()) * 100 * 1.18, 10))
    return fig


@solara.component
def AnalysisComparison(model: MicrogridModel):
    """Saved Phase 6 season comparison. Does not rerun the experiment."""
    del model
    solara.Markdown(
        "This page is not the live run. It reads the means already written by "
        "`python -m experiments.analyze`.\n\n"
        "Bar height is **grid dependence**: grid import divided by the households' "
        "total consumption. It is a percent, not kWh and not TL. A shorter bar means "
        "less of the consumption came from the macro grid.\n\n"
        "- **Grid only** (`logging`): neighbours do not trade. Surplus is sold to the "
        "grid and a deficit is bought from it.\n"
        "- **Local market** (`cda`): the same days, with households trading with each other.\n"
        "- **Reduction** is how much lower local-market dependence is than grid only. "
        "**Demand fill** is local kWh divided by buy-order volume. "
        "**Saving** is the community bill avoided over three days.\n"
        "- Summer starts on day 172 and winter on day 355, three days each. "
        "The synthetic bars are the mean of seeds 42–49. The whisker is the spread "
        "across those seeds. The Istanbul file uses the same seeds; its spread is zero "
        "because the energy series comes from the file."
    )
    if not ANALYSIS_SUMMARY.is_file() and not ISTANBUL_SUMMARY.is_file():
        solara.Markdown("No comparison file yet. Run `python -m experiments.analyze` first.")
        return
    _saved_source("Synthetic, seeds 42–49", ANALYSIS_SUMMARY, ANALYSIS_DIR)
    _saved_source("Ausgrid load with Istanbul PVGIS", ISTANBUL_SUMMARY, ISTANBUL_DIR)


@solara.component
def NamedComponentsView(components, model):
    """Two named tabs. Components stack in a column instead of a draggable grid."""
    current_tab_index, set_current_tab_index = solara.use_state(0)
    normalized = [comp if isinstance(comp, tuple) else (comp, 0) for comp in components]
    pages = collections.defaultdict(list)
    for component, page_index in normalized:
        pages[page_index].append(mesa_viz._wrap_component(component))
    sorted_page_indices = sorted(pages)
    if not normalized:
        return

    with solara.v.Tabs(v_model=current_tab_index, on_v_model=set_current_tab_index):
        for index in sorted_page_indices:
            solara.v.Tab(children=[_PAGE_NAMES.get(index, f"Page {index}")])

    with solara.v.Window(v_model=current_tab_index):
        for i, page_id in enumerate(sorted_page_indices):
            with solara.v.WindowItem():
                if i == current_tab_index:
                    with solara.Column(gap="12px", style={"padding": "8px 12px 24px"}):
                        for component in pages[page_id]:
                            component(model)


mesa_viz.ComponentsView = NamedComponentsView

model = MicrogridModel()

page = SolaraViz(
    model,
    components=[Simulation, (AnalysisComparison, 1)],
    model_params=model_params,
    name="P2P Microgrid",
)
