"""Parameter sweeps that rank on the in-sample period only and show the holdout beside it."""
from __future__ import annotations

import itertools
import logging
from dataclasses import replace

import pandas as pd

from .config import Deployment
from .data import DataSource
from .runner import run_deployment

log = logging.getLogger("dagtrade.sweep")


def parse_grid(items: list[str]) -> dict[str, list]:
    """['fast=10,20,30', 'slow=50,100'] -> {'fast': [10,20,30], 'slow': [50,100]}"""
    grid = {}
    for it in items:
        k, _, v = it.partition("=")
        vals = []
        for x in v.split(","):
            x = x.strip()
            try:
                vals.append(int(x))
            except ValueError:
                try:
                    vals.append(float(x))
                except ValueError:
                    vals.append({"true": True, "false": False}.get(x.lower(), x))
        grid[k.strip()] = vals
    return grid


def sweep(dep: Deployment, grid: dict[str, list], data: DataSource | None = None) -> pd.DataFrame:
    data = data or DataSource()
    keys = list(grid)
    rows = []
    for combo in itertools.product(*grid.values()):
        params = dict(zip(keys, combo))
        try:
            out = run_deployment(dep, data=data, params=params, save=False)
        except Exception as e:  # noqa: BLE001
            log.warning("combo %s failed: %s", params, e)
            continue
        m = out.metrics
        sp = m.get("split") or {}
        ins, ho = sp.get("in_sample", m["strategy"]), sp.get("holdout", {})
        rows.append({**params, "is_sharpe": ins.get("sharpe"), "is_return": ins.get("total_return"),
                     "ho_sharpe": ho.get("sharpe"), "ho_return": ho.get("total_return"),
                     "max_dd": m["strategy"].get("max_drawdown"), "trades": m["trades"]["n_trades"],
                     "luck_pct": (m.get("baseline") or {}).get("percentile")})
    df = pd.DataFrame(rows)
    return df.sort_values("is_sharpe", ascending=False).reset_index(drop=True) if len(df) else df
