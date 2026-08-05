"""Interactive SolaraViz dashboard.

Usage: solara run app.py
Page 0: community energy balance and macro-grid dependency.
Page 1: battery SoC and market order counts.
Page 2: per-household generation/consumption and buy/sell orders (stacked).
"""

from __future__ import annotations

import pandas as pd
import solara
from matplotlib.figure import Figure
from mesa.visualization import SolaraViz, make_plot_component
from mesa.visualization.utils import update_counter

from microgrid.model import MicrogridModel

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
    "seed": 42,
}


def _agent_history(model: MicrogridModel):
    """Return agent history as MultiIndex (Step, AgentID), or None if none yet.

    Mesa's empty DataCollector returns a quirky non-empty DataFrame whose index
    is literally ``Index(['Step', 'AgentID'])`` — not a MultiIndex — so ``.empty``
    is the wrong guard.
    """
    agent_df = model.datacollector.get_agent_vars_dataframe()
    if not isinstance(agent_df.index, pd.MultiIndex):
        return None
    if len(agent_df) == 0:
        return None
    return agent_df


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

    # Prefer named level; fall back to position 1 if Mesa renames the index.
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


energy_plot = make_plot_component(["TotalGeneration", "TotalConsumption"])
grid_plot = make_plot_component(["GridImport", "GridExport"])
soc_plot = make_plot_component("AvgProsumerSoC", page=1)
orders_plot = make_plot_component(["BuyOrders", "SellOrders"], page=1)

model = MicrogridModel()

page = SolaraViz(
    model,
    components=[
        energy_plot,
        grid_plot,
        soc_plot,
        orders_plot,
        (AgentHouseholdPlots, 2),
    ],
    model_params=model_params,
    name="P2P Microgrid (Phase 2)",
)
