import numpy as np
import pytest

from dagtrade.config import Deployment, discover_deployments
from dagtrade.runner import run_deployment
from dagtrade.store import RunStore

BUY_AND_HOLD = '''
from dagtrade import FSA, Node, Range, Trade
DEFAULTS = {}

class AlwaysBuy(Node):
    history = Range(candles=5)
    def run(self, candles, ctx):
        return {"buy": True}

def build():
    return FSA.chain(AlwaysBuy(), Trade(take_profit=1000.0, stop_loss=0.99))   # exits too far away to ever fire
'''


def test_buy_and_hold_matches_benchmark(data, tmp_path):
    d = tmp_path / "strategies" / "buy_and_hold"
    d.mkdir(parents=True)
    (d / "strategy.py").write_text(BUY_AND_HOLD)
    dep = Deployment(strategy="buy_and_hold", ticker="TEST", start="2022-01-03", end="2023-12-29",
                     sim={"commission_bps": 0, "slippage_bps": 0}, path=d / "TEST.toml")
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False)
    m = out.metrics
    # entering at the first open instead of the prior close: allow the one-bar gap but nothing more
    assert m["strategy"]["total_return"] == pytest.approx(m["buy_hold"]["total_return"], abs=0.02)
    assert out.metrics["trades"]["n_trades"] == 1


def _one_per_strategy():
    seen, out = set(), []
    for d in discover_deployments():
        if d.strategy not in seen:
            seen.add(d.strategy)
            out.append(d)
    return out


@pytest.mark.parametrize("dep", _one_per_strategy(), ids=lambda d: d.strategy)
def test_every_strategy_runs_and_is_causal(dep, data, tmp_path):
    intraday = (dep.base_tf or "1d")[-1] in "mh"
    dep.end = "2021-12-31"
    dep.start = ("2021-12-01" if dep.base_tf == "1m" else "2021-11-01") if intraday else "2019-01-01"   # minute candles: weeks
    dep.warmup_days = 10 if intraday else dep.warmup_days
    dep.params = {}
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False, check_causal=True)
    assert "OK" in out.meta["causal"], out.meta["causal"]
    assert np.isfinite(out.result.equity.iloc[-1])


def test_metrics_come_only_from_fills_and_match_the_broker(data, tmp_path):
    """The ledger rebuilds equity from the fills alone; it must reproduce the paper broker's own equity exactly."""
    from dagtrade.analytics.ledger import fills_frame, mark_to_market
    dep = next(d for d in discover_deployments() if d.strategy == "stochastic")
    dep.start, dep.end, dep.params = "2019-01-01", "2021-12-31", {}
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False)
    res = out.result
    mtm = mark_to_market(fills_frame(res.fills), res.bars, out.meta["capital"])
    j = res.start_idx
    assert np.allclose(mtm["equity"].to_numpy()[j:], res.equity.to_numpy()[j:], rtol=1e-9)
    assert out.metrics["trades"]["n_trades"] == len(out.ledger.trades) > 0


def test_deleting_backtests_frees_disk_and_never_escapes_runs_dir(data, tmp_path):
    store = RunStore(tmp_path / "runs")
    dep = next(d for d in discover_deployments() if d.strategy == "stochastic")
    dep.start, dep.end, dep.params = "2021-01-01", "2021-12-31", {}
    out = run_deployment(dep, data=data, store=store, cache=False)
    keep = tmp_path / "keep.txt"
    keep.write_text("x")
    for bad in ("", "..", "../keep.txt", "a/b"):
        with pytest.raises(ValueError):
            store.delete(bad)
    assert keep.exists()
    assert store.delete(out.run_id) > 0
    assert not store.dir(out.run_id).exists() and store.list() == []


def test_one_strategy_any_interval_and_flat_by_close(data, tmp_path):
    from dagtrade.config import deployment_for
    from dagtrade.runner import build_fsa
    daily = deployment_for("stochastic", "SPY")
    five = deployment_for("stochastic", "SPY", interval="5m")             # picks SPY@5m.toml
    assert daily.base_tf is None and not daily.flat_by_close
    assert five.base_tf == "5m" and five.flat_by_close and five.params["stop_loss"] == 0.02
    assert build_fsa(five)[0].resolve_tfs("5m")[1] == "5m"
    with pytest.raises(ValueError, match="intraday"):
        build_fsa(deployment_for("stochastic", "SPY", flat_by_close=True))
    # a 1h day-trading run: never holds a position overnight
    dep = deployment_for("ema_9_21", "SPY", start="2021-06-01", end="2021-07-31", interval="1h", flat_by_close=True)
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False)
    tr = out.ledger.trades
    assert len(tr) and (tr["entry_time"].dt.tz_convert("America/New_York").dt.date
                        == tr["exit_time"].dt.tz_convert("America/New_York").dt.date).all()


def test_a_cancelled_backtest_stops_midway_and_saves_nothing(data, tmp_path):
    import threading
    from dagtrade.runner import Cancelled
    dep = next(d for d in discover_deployments() if d.strategy == "stochastic")
    dep.start, dep.end, dep.params = "2019-01-01", "2021-12-31", {}
    store, stop, seen = RunStore(tmp_path / "runs"), threading.Event(), []

    def progress(name, done, total):
        seen.append(name)
        if len(seen) == 3:
            stop.set()                                  # the user presses Stop while nodes are running
    with pytest.raises(Cancelled):
        run_deployment(dep, data=data, store=store, cache=False, progress=progress, cancel=stop)
    assert len(seen) == 3 and store.list() == []


def test_a_cancelled_download_caches_nothing(tmp_path):
    from dagtrade.data import BarStore, DataSource, SyntheticProvider
    from dagtrade.runner import Cancelled
    src = DataSource([SyntheticProvider()], BarStore(tmp_path / "bars"))
    calls = []

    def hook(label, done, total):
        calls.append((done, total))
        if done == 2:
            raise Cancelled("stop")
    src.on_chunk = hook
    with pytest.raises(Cancelled):
        src.ensure("TEST", "5m", "2021-01-01", "2021-07-01")      # six months of 5m candles = several chunks
    assert calls[0][1] >= 6 and src.store.load("TEST", "5m")[1] is None
    src.on_chunk = None
    src.ensure("TEST", "5m", "2021-01-01", "2021-07-01")
    assert src.store.load("TEST", "5m")[1] is not None


def test_toml_size_turns_any_strategy_into_percent_orders(data, tmp_path):
    dep = next(d for d in discover_deployments() if d.strategy == "stochastic")
    dep.start, dep.end, dep.params, dep.size = "2019-01-01", "2021-12-31", {}, 0.9
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False)
    f = out.result.fills
    first = f[f.qty > 0].iloc[0]
    assert out.meta["capital"] == 10_000 and out.meta["size"] == 0.9
    assert first.qty * first.price == pytest.approx(9_000, rel=0.02)       # sized at the signal close, filled next open
    assert (out.result.equity.dropna() > 0).all()


def test_orb_fvg_takes_at_most_one_trade_a_day_and_never_holds_overnight(data, tmp_path):
    dep = next(d for d in discover_deployments() if d.strategy == "orb_fvg")
    dep.start, dep.end = "2021-11-01", "2021-11-30"
    out = run_deployment(dep, data=data, save=False)
    tr = out.ledger.trades
    ny = lambda s: s.dt.tz_convert("America/New_York")                       # noqa: E731
    assert len(tr) > 3 and (ny(tr.entry_time).dt.date == ny(tr.exit_time).dt.date).all()
    assert ny(tr.entry_time).dt.date.is_unique
    outs = {(e["from"], e["to"], e["outcome"]) for e in out.result.events}
    assert ("retest_engulf", "gap_break", "fail") in outs or ("retest_engulf", "gap_break", "max_wait") in outs
