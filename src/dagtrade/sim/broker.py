"""Simulation settings and round-trip bookkeeping.

The fill rules themselves live in `paper.PaperBroker` (shared by backtests and the local live paper engine);
the backtest loop is `fsa.engine.backtest`.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class SimConfig:
    capital: float = 1000.0            # starting cash (runner: the order size for fixed $ orders, $10k for % orders)
    commission_bps: float = 1.0        # per side, of notional
    slippage_bps: float = 2.0          # per market fill, against you
    allow_short: bool = True
    whole_shares: bool = False         # backtests: fractional shares; live always trades whole shares


def build_trades(bars: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    """Round trips (flat -> flat) from fills, with net PnL, return, MAE/MFE."""
    cols = ["entry_time", "exit_time", "side", "qty", "entry_price", "exit_price", "pnl", "ret", "bars_held",
            "exit_reason", "mae", "mfe"]
    if fills.empty:
        return pd.DataFrame(columns=cols)
    rows, pos = [], 0.0
    cur = None
    for f in fills.itertuples():
        if cur is None:
            cur = dict(entry_bar=f.bar, side=1.0 if f.qty > 0 else -1.0, in_q=0.0, in_cost=0.0, out_q=0.0,
                       out_val=0.0, comm=0.0, peak=0.0)
        same_dir = (f.qty > 0) == (cur["side"] > 0)
        if same_dir:
            cur["in_q"] += abs(f.qty)
            cur["in_cost"] += abs(f.qty) * f.price
        else:
            cur["out_q"] += abs(f.qty)
            cur["out_val"] += abs(f.qty) * f.price
        cur["comm"] += f.commission
        cur["peak"] = max(cur["peak"], cur["in_q"])
        pos += f.qty
        if abs(pos) < 1e-9:
            ep = cur["in_cost"] / cur["in_q"]
            xp = cur["out_val"] / cur["out_q"]
            pnl = cur["side"] * (cur["out_val"] - cur["in_cost"]) - cur["comm"]
            seg = bars.iloc[cur["entry_bar"]: f.bar + 1]
            if cur["side"] > 0:
                mae, mfe = seg["low"].min() / ep - 1, seg["high"].max() / ep - 1
            else:
                mae, mfe = 1 - seg["high"].max() / ep, 1 - seg["low"].min() / ep
            rows.append(dict(entry_time=bars.index[cur["entry_bar"]], exit_time=bars.index[f.bar],
                             side="long" if cur["side"] > 0 else "short", qty=cur["peak"], entry_price=ep,
                             exit_price=xp, pnl=pnl, ret=pnl / cur["in_cost"], bars_held=f.bar - cur["entry_bar"],
                             exit_reason=f.reason, mae=mae, mfe=mfe))
            cur, pos = None, 0.0
    return pd.DataFrame(rows, columns=cols)
