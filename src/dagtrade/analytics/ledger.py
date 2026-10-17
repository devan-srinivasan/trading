"""The ledger: FILLS ARE THE SINGLE SOURCE OF TRUTH for results.

Backtests and live trading both end up as a list of fills (time, side, qty, price, fee). Everything reported -
equity curve, round-trip trades, return, Sharpe, drawdown, win rate, alpha/beta vs SPY, holdout split, the
random-timing check - is computed here from those fills plus the candles (to value an open position), over any
time window. Where the fills came from (the backtest simulator, the local paper engine, Alpaca) does not matter.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .baseline import random_timing_baseline
from .metrics import daily_equity, equity_stats, relative_stats, split_stats, trade_stats


def fills_frame(fills) -> pd.DataFrame:
    """Normalise fills (list of dicts or DataFrame) to: time (UTC), qty (signed: + buy / - sell), price, fee, reason."""
    if isinstance(fills, pd.DataFrame) and "qty" in fills and "side" not in fills:
        df = fills.rename(columns={"commission": "fee"}).copy()          # simulator fills: signed qty already
    else:
        rows = []
        for f in (fills.to_dict("records") if isinstance(fills, pd.DataFrame) else fills or []):
            t = f["time"]
            t = pd.Timestamp(t, unit="s", tz="UTC") if isinstance(t, (int, float, np.integer, np.floating)) else pd.Timestamp(t)
            q = float(f["qty"]) * (1 if f["side"] == "buy" else -1)
            rows.append({"time": t, "qty": q, "price": float(f["price"]), "fee": float(f.get("fee") or 0.0),
                         "reason": f.get("reason") or f.get("role") or ""})
        df = pd.DataFrame(rows, columns=["time", "qty", "price", "fee", "reason"])
    if len(df):
        t = pd.DatetimeIndex(df["time"])
        df["time"] = t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
        df = df.sort_values("time", kind="stable").reset_index(drop=True)
    for c, d in (("fee", 0.0), ("reason", "")):
        if c not in df:
            df[c] = d
    return df[["time", "qty", "price", "fee", "reason"]]


def mark_to_market(fills: pd.DataFrame, candles: pd.DataFrame, cash0: float) -> pd.DataFrame:
    """Cash, position, equity and exposure at the close of every candle (fills apply in the candle they fall in)."""
    idx = pd.DatetimeIndex(candles.index)
    n = len(idx)
    dcash, dpos = np.zeros(n), np.zeros(n)
    if len(fills):
        bar = np.clip(idx.searchsorted(pd.DatetimeIndex(fills["time"]), side="right") - 1, 0, n - 1)
        np.add.at(dcash, bar, -(fills["qty"] * fills["price"] + fills["fee"]).to_numpy())
        np.add.at(dpos, bar, fills["qty"].to_numpy())
    cash, pos = cash0 + np.cumsum(dcash), np.cumsum(dpos)
    pos[np.abs(pos) < 1e-9] = 0.0
    close = candles["close"].to_numpy(float)
    equity = cash + pos * close
    with np.errstate(divide="ignore", invalid="ignore"):
        expo = np.where(equity != 0, pos * close / equity, 0.0)
    return pd.DataFrame({"cash": cash, "position": pos, "equity": equity, "exposure": expo}, index=idx)


def round_trips(fills: pd.DataFrame, candles: pd.DataFrame) -> pd.DataFrame:
    """Flat-to-flat trades (with MAE/MFE) built from the fills."""
    from ..sim.broker import build_trades
    idx = pd.DatetimeIndex(candles.index)
    f = fills.copy()
    f["bar"] = np.clip(idx.searchsorted(pd.DatetimeIndex(f["time"]), side="right") - 1, 0, max(len(idx) - 1, 0)) if len(f) else []
    return build_trades(candles, f.rename(columns={"fee": "commission"}))


def _rebase(series: pd.Series, dates: pd.DatetimeIndex, base: float) -> pd.Series:
    s = series.reindex(series.index.union(dates)).ffill()
    before = series[series.index < dates[0]]
    ref = before.iloc[-1] if len(before) else s.loc[dates[0]]
    return s.loc[dates] / ref * base


@dataclass
class LedgerReport:
    equity: pd.Series                      # per candle, inside the window
    exposure: pd.Series
    daily: pd.DataFrame                    # strategy (+ benchmarks) per day, starting at the window's base value
    trades: pd.DataFrame                   # round trips that ended inside the window
    fills: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def ledger_report(fills, candles: pd.DataFrame, cash0: float, base_tf: str, start=None, end=None,
                  benchmarks: dict[str, pd.Series] | None = None, holdout: float = 0.0,
                  cost_bps: float = 6.0) -> LedgerReport:
    """Everything we report about a run, computed only from its fills (+ candles to value open positions).

    candles: OHLC with candle OPEN time index, covering the window (and ideally the first fill).
    benchmarks: name -> daily close series (e.g. {"buy_hold": ticker closes, "spy": SPY closes}).
    """
    f = fills_frame(fills)
    mtm = mark_to_market(f, candles, cash0)
    idx = mtm.index
    lo = 0 if start is None else int(idx.searchsorted(pd.Timestamp(start)))
    hi = len(idx) if end is None else int(idx.searchsorted(pd.Timestamp(end)))
    w = mtm.iloc[lo:hi]
    if len(w) == 0:
        return LedgerReport(pd.Series(dtype=float), pd.Series(dtype=float), pd.DataFrame(), pd.DataFrame(), f,
                            {"strategy": {}, "trades": {"n_trades": 0}})
    base = float(mtm["equity"].iloc[lo - 1]) if lo > 0 else float(cash0)
    ed = daily_equity(w["equity"], base_tf)
    first = ed.index[0] - pd.Timedelta(days=1)
    ed = pd.concat([pd.Series({first: base}), ed]).rename("strategy")
    ed.index = pd.DatetimeIndex(ed.index)

    trades = round_trips(f, candles)
    if len(trades):
        t0 = idx[lo]
        trades = trades[pd.DatetimeIndex(trades["exit_time"]) >= t0]
    metrics: dict = {"strategy": equity_stats(ed), "trades": trade_stats(trades, w["exposure"])}
    daily = {"strategy": ed}
    for key, ser in (benchmarks or {}).items():
        try:
            b = pd.concat([pd.Series({first: base}), _rebase(ser, ed.index[1:], base)])
            daily[key] = b.reindex(ed.index).ffill().rename(key)
            metrics[key] = equity_stats(daily[key])
        except Exception:  # noqa: BLE001
            continue
    for key, name in (("spy", "vs_spy"), ("buy_hold", "vs_buy_hold")):
        if key in daily:
            metrics[name] = relative_stats(ed, daily[key])
    metrics["split"] = split_stats(ed, trades, holdout, base_tf) if holdout else {}
    metrics["baseline"] = random_timing_baseline(candles["close"].iloc[lo:hi], w["exposure"],
                                                 metrics["strategy"].get("total_return", 0.0), cost_bps=cost_bps)
    metrics["fills"] = int(len(f[(f["time"] >= idx[lo])])) if len(f) else 0
    return LedgerReport(w["equity"], w["exposure"], pd.concat(list(daily.values()), axis=1), trades, f, metrics)
