"""Performance metrics. Ratios are computed on DAILY equity returns (252 trading days/yr), rf = 0 by default."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.resample import NY
from .. import tf as tfm

ANN = 252


def daily_equity(equity: pd.Series, base_tf: str) -> pd.Series:
    """Last equity value per trading day (date index)."""
    if tfm.is_intraday(base_tf):
        dates = equity.index.tz_convert(NY).tz_localize(None).normalize()
    else:
        dates = equity.index.tz_convert("UTC").tz_localize(None).normalize()
    d = equity.groupby(dates).last()
    d.index = pd.DatetimeIndex(d.index)
    return d


def _dd(eq: pd.Series) -> tuple[float, int]:
    peak = eq.cummax()
    dd = eq / peak - 1
    under = dd < 0
    longest = cur = 0
    for u in under.to_numpy():
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    return float(dd.min()) if len(dd) else 0.0, longest


def equity_stats(eq: pd.Series, rf: float = 0.0) -> dict:
    """Stats from a daily equity series."""
    eq = eq.dropna()
    if len(eq) < 3:
        return {}
    r = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    vol = r.std() * np.sqrt(ANN)
    ex = r - rf / ANN
    sharpe = ex.mean() / r.std() * np.sqrt(ANN) if r.std() > 0 else 0.0
    down = r[r < 0].std()
    sortino = ex.mean() / down * np.sqrt(ANN) if down and down > 0 else 0.0
    mdd, mdd_days = _dd(eq)
    return {"total_return": float(total), "cagr": float(cagr), "vol": float(vol), "sharpe": float(sharpe),
            "sortino": float(sortino), "max_drawdown": mdd, "max_dd_days": int(mdd_days),
            "calmar": float(cagr / abs(mdd)) if mdd < 0 else 0.0, "best_day": float(r.max()),
            "worst_day": float(r.min()), "days": int(len(eq))}


def trade_stats(trades: pd.DataFrame, exposure: pd.Series | None = None) -> dict:
    out = {"n_trades": int(len(trades))}
    if exposure is not None and len(exposure):
        out["exposure"] = float((exposure.abs() > 1e-9).mean())
    if trades.empty:
        return {**out, "win_rate": 0.0, "profit_factor": 0.0, "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "avg_bars_held": 0.0}
    w, lo = trades[trades.pnl > 0], trades[trades.pnl <= 0]
    gross_w, gross_l = w.pnl.sum(), -lo.pnl.sum()
    out.update(win_rate=float(len(w) / len(trades)),
               profit_factor=float(gross_w / gross_l) if gross_l > 0 else float("inf") if gross_w > 0 else 0.0,
               expectancy=float(trades.ret.mean()), avg_win=float(w.ret.mean()) if len(w) else 0.0,
               avg_loss=float(lo.ret.mean()) if len(lo) else 0.0, avg_bars_held=float(trades.bars_held.mean()),
               total_pnl=float(trades.pnl.sum()))
    return out


def relative_stats(eq: pd.Series, bench: pd.Series) -> dict:
    """Alpha/beta/correlation/excess return of daily equity vs a daily benchmark price series."""
    j = pd.concat([eq.pct_change(), bench.pct_change()], axis=1, join="inner").dropna()
    if len(j) < 5 or j.iloc[:, 1].var() == 0:
        return {}
    s, b = j.iloc[:, 0], j.iloc[:, 1]
    if s.std() == 0:
        return {}
    beta = float(np.cov(s, b)[0, 1] / b.var())
    alpha = float((s.mean() - beta * b.mean()) * ANN)
    bt = bench.reindex(eq.index, method="ffill").dropna()
    b_total = bt.iloc[-1] / bt.iloc[0] - 1
    return {"beta": beta, "alpha": alpha, "corr": float(s.corr(b)), "excess_return": float(
        (eq.iloc[-1] / eq.iloc[0] - 1) - b_total)}


def split_stats(eq_daily: pd.Series, trades: pd.DataFrame, holdout: float, base_tf: str) -> dict:
    """In-sample vs holdout (last `holdout` fraction of the period) - the overfitting check."""
    if holdout <= 0 or len(eq_daily) < 20:
        return {}
    cut = eq_daily.index[int(len(eq_daily) * (1 - holdout))]
    a, b = eq_daily[eq_daily.index <= cut], eq_daily[eq_daily.index >= cut]
    ta = trades[trades.entry_time.dt.tz_convert("UTC").dt.tz_localize(None) < cut] if len(trades) else trades
    tb = trades[trades.entry_time.dt.tz_convert("UTC").dt.tz_localize(None) >= cut] if len(trades) else trades
    return {"split_date": str(cut.date()), "in_sample": {**equity_stats(a), **trade_stats(ta)},
            "holdout": {**equity_stats(b), **trade_stats(tb)}}
