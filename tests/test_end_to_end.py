import numpy as np
import pandas as pd
import pytest

from dagtrade.config import Deployment, discover_deployments, load_deployment
from dagtrade.runner import run_deployment
from dagtrade.store import RunStore


def test_buy_and_hold_matches_benchmark(data, tmp_path):
    dep = Deployment(strategy="buy_and_hold", ticker="TEST", start="2022-01-03", end="2023-12-29",
                     sim={"commission_bps": 0, "slippage_bps": 0})
    from pathlib import Path
    dep.path = Path("strategies/buy_and_hold/TEST.toml").resolve()
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False)
    m = out.metrics
    # entering at the first open instead of the prior close: allow the one-bar gap but nothing more
    assert m["strategy"]["total_return"] == pytest.approx(m["buy_hold"]["total_return"], abs=0.02)
    assert out.metrics["trades"]["n_trades"] == 1


@pytest.mark.parametrize("dep", [d for d in discover_deployments() if d.strategy != "trend_pullback"],
                         ids=lambda d: d.label)
def test_demo_strategies_run_and_are_causal(dep, data, tmp_path):
    dep.end = "2021-12-31"
    dep.start = "2019-01-01"
    dep.params = {}
    out = run_deployment(dep, data=data, store=RunStore(tmp_path / "runs"), cache=False, check_causal=True)
    assert "OK" in out.meta["causal"], out.meta["causal"]
    assert np.isfinite(out.sim.equity.iloc[-1])
