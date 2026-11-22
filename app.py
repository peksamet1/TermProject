"""Interactive SolaraViz dashboard (a development panel, not a public app).

Usage: solara run app.py
Page 0: community energy balance, grid flows, and the running KPIs.
Page 1: SoC, order counts, local P2P trade volume/price.
Page 2: per-household generation/consumption and buy/sell orders (stacked).
Page 3: Phase 6 comparison already written by ``python -m experiments.analyze``.
        The panel does not recompute it and does not open ZeroMQ sockets.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import solara
from matplotlib.figure import Figure
from mesa.visualization import SolaraViz, make_plot_component
from mesa.visualization.utils import update_counter

from microgrid.metrics import grid_dependence
from microgrid.model import MicrogridModel

ANALYSIS_SUMMARY = Path(__file__).parent / "results" / "analysis" / "summary.csv"

model_params = {
    "num_prosumers": {
        "type": "SliderInt",
        "value": 6,
        "min": 1,
        "max": 30,
        "step": 1,
        "label": "Prosumer sayisi",
    },
    "num_consumers": {
        "type": "SliderInt",
        "value": 4,
        "min": 0,
        "max": 30,
        "step": 1,
        "label": "Consumer sayisi",
    },
    "pv_peak_kw": {
        "type": "SliderFloat",
        "value": 5.0,
        "min": 1.0,
        "max": 15.0,
        "step": 0.5,
        "label": "PV kurulu guc (kW)",
    },
    "battery_capacity_kwh": {
        "type": "SliderFloat",
        "value": 10.0,
        "min": 2.0,
        "max": 30.0,
        "step": 1.0,
        "label": "Batarya kapasitesi (kWh)",
    },
    "start_day_of_year": {
        "type": "SliderInt",
        "value": 172,
        "min": 1,
        "max": 365,
        "step": 1,
        "label": "Yilin gunu (172 yaz, 355 kis)",
    },
    "market_backend": {
        "type": "Select",
        "value": "cda",
        "values": ["cda", "logging"],
        "label": "Piyasa",
    },
    "seed": 42,
}


def _agent_history(model: MicrogridModel):
    """Return agent history as MultiIndex (Step, AgentID), or None if none yet."""
    agent_df = model.datacollector.get_agent_vars_dataframe()
    if not isinstance(agent_df.index, pd.MultiIndex):
        return None
    if len(agent_df) == 0:
        return None
    return agent_df


@solara.component
def RunKpis(model: MicrogridModel):
    """Page 0: dependence and local trade of the steps played so far."""
    update_counter.get()
    frame = model.datacollector.get_model_vars_dataframe()
    if frame is None or len(frame) == 0:
        solara.Markdown("Henuz adim yok. Modeli ilerletince bagimlilik ve yerel kWh burada yazar.")
        return
    dependence = grid_dependence(frame)
    local = float(frame["LocalTradedKwh"].sum())
    imported = float(frame["GridImport"].sum())
    solara.Markdown(
        f"**Sebeke bagimliligi** {100 * dependence:.1f}% "
        f"(alim {imported:.2f} kWh) · **Yerel ticaret** {local:.2f} kWh"
    )


@solara.component
def AnalysisComparison(model: MicrogridModel):
    """Page 3: read results/analysis/summary.csv. Does not run the experiment."""
    del model  # SolaraViz always passes the live model; this page ignores it.
    if not ANALYSIS_SUMMARY.is_file():
        solara.Markdown(
            "Karsilastirma yok. Once `python -m experiments.analyze` calistirin; "
            "cikti `results/analysis/summary.csv`."
        )
        return
    frame = pd.read_csv(ANALYSIS_SUMMARY)
    seasons = frame[(frame["sweep"] == "season") & (frame["backend"].isin(["logging", "cda"]))]
    if seasons.empty:
        solara.Markdown("summary.csv icinde mevsim satiri yok.")
        return
    fig = Figure(figsize=(7.2, 4.0), layout="constrained")
    ax = fig.subplots()
    seasons = seasons.copy()
    seasons["label"] = seasons["season"] + " / " + seasons["backend"]
    ax.bar(seasons["label"], seasons["grid_dependence_mean"] * 100, color="#2a78d6")
    ax.set_ylabel("Sebeke bagimliligi (%)")
    ax.set_title("Analiz betigi: ortalama bagimlilik (yeniden hesaplanmaz)")
    ax.tick_params(axis="x", labelrotation=20)
    solara.FigureMatplotlib(fig)


@solara.component
def AgentHouseholdPlots(model: MicrogridModel):
    """Page 2: one chart block per household, stacked vertically."""
    update_counter.get()
    agent_df = _agent_history(model)
    if agent_df is None:
        solara.Markdown(
            "Henuz veri yok — modeli birkac adim ilerletince hane grafikleri burada birikir."
        )
        return

    level = "AgentID" if "AgentID" in (agent_df.index.names or []) else 1
    agent_ids = sorted(agent_df.index.get_level_values(level).unique())
    with solara.Column(gap="20px"):
        for agent_id in agent_ids:
            frame = agent_df.xs(agent_id, level=level)
            label = (
                frame["Label"].iloc[-1]
                if "Label" in frame.columns and len(frame) > 0
                else f"Agent-{agent_id}"
            )
            steps = frame.index.to_numpy()

            fig = Figure(figsize=(10, 4.2), layout="constrained")
            ax_energy, ax_orders = fig.subplots(2, 1, sharex=True)

            ax_energy.plot(steps, frame["Generation"], label="Generation", color="tab:orange")
            ax_energy.plot(steps, frame["Consumption"], label="Consumption", color="tab:blue")
            ax_energy.set_ylabel("kWh / step")
            ax_energy.set_title(f"{label} — generation / consumption")
            ax_energy.legend(loc="upper right", fontsize=8)

            ax_orders.plot(steps, frame["BuyOrder"], label="Buy order", color="tab:red")
            ax_orders.plot(steps, frame["SellOrder"], label="Sell order", color="tab:green")
            ax_orders.set_ylabel("kWh / step")
            ax_orders.set_xlabel("Step")
            ax_orders.set_title(f"{label} — buy / sell orders")
            ax_orders.legend(loc="upper right", fontsize=8)

            solara.FigureMatplotlib(fig)


energy_plot = make_plot_component(["TotalGeneration", "TotalConsumption", "LocalTradedKwh"])
grid_plot = make_plot_component(["GridImport", "GridExport"])
soc_plot = make_plot_component("AvgProsumerSoC", page=1)
orders_plot = make_plot_component(["BuyOrders", "SellOrders"], page=1)
trade_plot = make_plot_component(["LocalTradedKwh", "LocalTradeCount"], page=1)
price_plot = make_plot_component("AvgTradePrice", page=1)

model = MicrogridModel()

page = SolaraViz(
    model,
    components=[
        RunKpis,
        energy_plot,
        grid_plot,
        soc_plot,
        orders_plot,
        trade_plot,
        price_plot,
        (AgentHouseholdPlots, 2),
        (AnalysisComparison, 3),
    ],
    model_params=model_params,
    name="P2P Microgrid",
)
