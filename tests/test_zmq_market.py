"""cda_zmq backend: decentralized CDA copies over ZeroMQ (Phase 4)."""

import pytest

from microgrid.comm.protocol import OrderMessage
from microgrid.config import SimulationConfig
from microgrid.market.base import OrderSide
from microgrid.model import MicrogridModel

from .conftest import market_grid_net

STEPS = 48  # 00:00-12:00 covers the first trades of the day


def run_model(backend: str, steps: int = STEPS, **comm) -> MicrogridModel:
    config = SimulationConfig(market_backend=backend)
    for key, value in comm.items():
        setattr(config.comm, key, value)
    model = MicrogridModel(config=config)
    model.run(steps)
    return model


@pytest.fixture
def cda_run():
    return run_model("cda")


def cash(model) -> list[float]:
    return [a.cash_tl for a in sorted(model.agents, key=lambda a: a.unique_id)]


def test_lossless_run_matches_in_process_cda(cda_run):
    with run_model("cda_zmq") as zmq_model:
        assert len(cda_run.market.trades) > 0
        assert zmq_model.market.trades == cda_run.market.trades
        assert cash(zmq_model) == cash(cda_run)
        a = cda_run.datacollector.get_model_vars_dataframe()
        b = zmq_model.datacollector.get_model_vars_dataframe()
        for column in ("GridImport", "GridExport", "LocalTradedKwh", "LocalTradeCount"):
            assert b[column].tolist() == pytest.approx(a[column].tolist(), abs=1e-9)
        summary = zmq_model.market.comm_summary()
        assert summary["status_loss"] == summary["order_loss"] == 0
        assert summary["trades_confirmed"] == len(cda_run.market.trades)
        assert summary["trades_nacked"] == summary["trades_timed_out"] == 0


def test_energy_balance_each_step():
    with MicrogridModel(config=SimulationConfig(market_backend="cda_zmq")) as model:
        for _ in range(STEPS):
            model.step()
            orders = model.market.orders_at(model.current_step - 1)
            order_net = sum(
                o.quantity_kwh if o.side is OrderSide.SELL else -o.quantity_kwh for o in orders
            )
            assert order_net == pytest.approx(
                market_grid_net(model), abs=1e-9
            )


def test_every_node_hears_every_status_with_cached_values():
    with MicrogridModel(config=SimulationConfig(market_backend="cda_zmq")) as model:
        model.step()
        net = model.market.net
        for node_id in net.nodes:
            statuses = net[node_id].statuses_at(0)
            assert sorted(statuses) == sorted(a.unique_id for a in model.agents)
            for agent in model.agents:
                status = statuses[agent.unique_id]
                assert status.residual_kwh == agent.residual_kwh
                assert status.state == agent.state.value


def test_lost_orders_are_nacked_and_fall_back_to_grid(cda_run):
    """One node misses every peer ORDER of a step: its copy diverges, the
    trades it would sell in are NACKed and that volume goes to the grid."""
    first = cda_run.market.trades[0]
    step, victim = first.step, first.seller_id

    config = SimulationConfig(market_backend="cda_zmq")
    config.comm.step_timeout_s = 0.2
    with MicrogridModel(config=config) as model:
        node = model.market.net[victim]
        deliver = node._deliver

        def lossy(msg, _deliver=deliver):
            if isinstance(msg, OrderMessage) and msg.step == step and msg.agent_id != victim:
                return  # dropped on the way in
            _deliver(msg)

        node._deliver = lossy
        model.run(step + 1)

        report = model.market.reports[step]
        assert report.degraded
        assert all(victim not in (t.buyer_id, t.seller_id) for t in model.market.trades_at(step))
        assert model.market.negotiations[step].nacked >= 1
        # Still balanced: unconfirmed volume settles with the grid.
        orders = model.market.orders_at(step)
        order_net = sum(
            o.quantity_kwh if o.side is OrderSide.SELL else -o.quantity_kwh for o in orders
        )
        assert order_net == pytest.approx(market_grid_net(model), abs=1e-9)
        # Earlier steps were untouched.
        earlier = [t for t in cda_run.market.trades if t.step < step]
        assert [t for t in model.market.trades if t.step < step] == earlier


def test_same_seed_reproducible():
    def trades():
        with run_model("cda_zmq", steps=STEPS) as model:
            return model.market.trades

    assert trades() == trades()


def test_close_releases_sockets_and_unknown_backend_is_rejected():
    model = MicrogridModel(config=SimulationConfig(market_backend="cda_zmq"))
    model.close()
    assert model.market.net.closed
    with pytest.raises(ValueError, match="cda_zmq"):
        MicrogridModel(market_backend="zmq")
