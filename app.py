"""Interactive SolaraViz dashboard (a development panel, not a public app).

Usage: solara run app.py

Two tabs:

- Simulation: the live run. Community totals and the selected household
  share this page. Does not open ZeroMQ sockets.
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
from microgrid.metrics import grid_dependence
from microgrid.model import MicrogridModel

ANALYSIS_SUMMARY = Path(__file__).parent / "results" / "analysis" / "summary.csv"

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


def _agent_history(model: MicrogridModel):
    """Return agent history as MultiIndex (Step, AgentID), or None if none yet."""
    agent_df = model.datacollector.get_agent_vars_dataframe()
    if not isinstance(agent_df.index, pd.MultiIndex) or len(agent_df) == 0:
        return None
    return agent_df


def _soc_text(agent) -> str:
    if agent.battery is None or agent.soc != agent.soc:  # NaN
        return "—"
    return f"{agent.soc:.2f}"


def _community_figures(frame: pd.DataFrame) -> tuple[Figure, Figure]:
    steps = frame.index.to_numpy()
    energy, ax_energy = _figure(6.4, 3.1)
    ax_energy.plot(steps, frame["TotalGeneration"], label="Generation", color=_GEN, linewidth=1.6)
    ax_energy.plot(steps, frame["TotalConsumption"], label="Consumption", color=_LOAD, linewidth=1.6)
    ax_energy.fill_between(steps, frame["LocalTradedKwh"], color=_LOCAL, alpha=0.25, label="Local trade")
    _style_ax(ax_energy, "kWh / step")
    ax_energy.set_title("Community energy")
    ax_energy.legend(loc="upper right", fontsize=8, frameon=False)

    grid, ax_grid = _figure(6.4, 3.1)
    ax_grid.plot(steps, frame["GridImport"], label="Grid import", color=_IMPORT, linewidth=1.6)
    ax_grid.plot(steps, frame["GridExport"], label="Grid export", color=_EXPORT, linewidth=1.6)
    _style_ax(ax_grid, "kWh / step")
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


def _market_figures(frame: pd.DataFrame) -> tuple[Figure, Figure]:
    steps = frame.index.to_numpy()
    orders, ax_orders = _figure(6.4, 2.8)
    ax_orders.plot(steps, frame["BuyOrders"], label="Buy orders", color=_IMPORT, linewidth=1.6)
    ax_orders.plot(steps, frame["SellOrders"], label="Sell orders", color=_LOCAL, linewidth=1.6)
    _style_ax(ax_orders, "Orders")
    ax_orders.set_title("Orders")
    ax_orders.legend(loc="upper right", fontsize=8, frameon=False)

    price, ax_price = _figure(6.4, 2.8)
    ax_price.plot(steps, frame["AvgTradePrice"], color=_LOAD, linewidth=1.6)
    _style_ax(ax_price, "TL/kWh")
    ax_price.set_title("Average clearing price")
    return orders, price


def _household_figure(agent, frame: pd.DataFrame) -> Figure:
    steps = frame.index.to_numpy()
    fig, ax = _figure(10.2, 3.6)
    ax.plot(steps, frame["Generation"], label="Generation", color=_GEN, linewidth=1.6)
    ax.plot(steps, frame["Consumption"], label="Consumption", color=_LOAD, linewidth=1.6)
    ax.plot(steps, frame["BatteryFlow"], label="Battery", color=_EXPORT, linewidth=1.4)
    ax.plot(steps, frame["Residual"], label="Residual", color=_LOCAL, linewidth=1.4)
    _style_ax(ax, "kWh / step")
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
    """Community totals and one household, on the same page."""
    update_counter.get()
    _refresh, set_refresh = solara.use_state(0)
    selected, set_selected = solara.use_state("Prosumer-1")
    frame = model.datacollector.get_model_vars_dataframe()
    has_steps = frame is not None and len(frame) > 0

    solara.Markdown(
        "Community totals are the charts above the table. "
        "The table and the chart under it are the household you select."
    )
    if not has_steps:
        solara.Markdown("No steps yet. Press Step to fill the charts.")
    else:
        dependence = grid_dependence(frame)
        with solara.Columns([1, 1, 1, 1]):
            solara.Markdown(f"**Grid dependence**\n\n{100 * dependence:.1f}%")
            solara.Markdown(f"**Grid import**\n\n{float(frame['GridImport'].sum()):.2f} kWh")
            solara.Markdown(f"**Grid export**\n\n{float(frame['GridExport'].sum()):.2f} kWh")
            solara.Markdown(f"**Local trade**\n\n{float(frame['LocalTradedKwh'].sum()):.2f} kWh")
        energy, grid = _community_figures(frame)
        orders, price = _market_figures(frame)
        with solara.Columns([1, 1]):
            solara.FigureMatplotlib(energy)
            solara.FigureMatplotlib(grid)
        with solara.Columns([1, 1]):
            solara.FigureMatplotlib(orders)
            solara.FigureMatplotlib(price)

    agents = list(model.agents)
    if not agents:
        solara.Markdown("This model has no households.")
        return
    labels = [agent.label for agent in agents]
    current = selected if selected in labels else labels[0]
    agent = next(item for item in agents if item.label == current)
    solara.Markdown(_household_table(model))
    with solara.Columns([1, 2]):
        solara.Select("Household", value=current, values=labels, on_value=set_selected)
        HouseholdPlay(agent, set_refresh)
    solara.Markdown(
        f"**{agent.label}** · {_STATE.get(agent.state.value, agent.state.value)} · "
        f"SoC {_soc_text(agent)} · cash {agent.cash_tl:.2f} TL · "
        f"generation scale {agent.generation_scale:.1f} · load scale {agent.load_scale:.1f}"
    )
    agent_df = _agent_history(model)
    if agent_df is None:
        solara.Markdown("No steps yet. This household's chart fills in as you step.")
        return
    level = "AgentID" if "AgentID" in (agent_df.index.names or []) else 1
    home = agent_df.xs(agent.unique_id, level=level)
    if len(home) == 0:
        solara.Markdown("No steps yet. This household's chart fills in as you step.")
        return
    solara.FigureMatplotlib(_household_figure(agent, home))


def _season_rows(frame: pd.DataFrame) -> pd.DataFrame:
    seasons = frame[(frame["sweep"] == "season") & (frame["backend"].isin(["logging", "cda"]))].copy()
    order = {"summer": 0, "winter": 1, "logging": 0, "cda": 1}
    seasons["_order"] = seasons["season"].map(order) * 2 + seasons["backend"].map(order)
    return seasons.sort_values("_order")


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
        "`python -m experiments.analyze` (`results/analysis/summary.csv`).\n\n"
        "Bar height is **grid dependence**: grid import divided by the households' "
        "total consumption. It is a percent, not kWh and not TL. A shorter bar means "
        "less of the consumption came from the macro grid.\n\n"
        "- **Grid only** (`logging`): neighbours do not trade. Surplus is sold to the "
        "grid and a deficit is bought from it.\n"
        "- **Local market** (`cda`): the same days, with households trading with each other.\n"
        "- Summer starts on day 172 and winter on day 355, three days each. "
        "Each bar is the mean of seeds 42–49. The whisker is the spread across those seeds."
    )
    if not ANALYSIS_SUMMARY.is_file():
        solara.Markdown("No comparison file yet. Run `python -m experiments.analyze` first.")
        return
    frame = pd.read_csv(ANALYSIS_SUMMARY)
    seasons = _season_rows(frame)
    if seasons.empty:
        solara.Markdown("summary.csv has no season rows.")
        return
    solara.FigureMatplotlib(_analysis_figure(seasons))
    lines = ["| Run | Grid dependence |", "| --- | --- |"]
    for row in seasons.itertuples():
        lines.append(
            f"| {_SEASON.get(row.season, row.season)}, {_BACKEND.get(row.backend, row.backend)} | "
            f"{100 * row.grid_dependence_mean:.1f}% ± {100 * row.grid_dependence_std:.1f} |"
        )
    solara.Markdown("\n".join(lines))


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
