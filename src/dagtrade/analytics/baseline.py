"""'Is it just luck?' baseline: keep the strategy's exposure pattern (how long, how big, long/short) but move it to
random times by circularly shifting it across the price history. If the real result is not better than most of the
shifted ones, the timing skill is not demonstrated."""
from __future__ import annotations

import numpy as np
import pandas as pd


def random_timing_baseline(close: pd.Series, exposure: pd.Series, actual_total_return: float,
                           cost_bps: float = 3.0, n: int = 300, seed: int = 0) -> dict:
    r = close.pct_change().fillna(0.0).to_numpy()
    pos = exposure.shift(1).fillna(0.0).to_numpy()          # exposure held *into* each bar
    m = len(r)
    if m < 50 or not np.any(pos) or (np.abs(pos) > 0.05).mean() > 0.9:
        return {}      # always invested: shifting it changes nothing, so there is nothing to compare
    rng = np.random.default_rng(seed)
    shifts = rng.integers(m // 10, m - m // 10, size=n)
    totals = np.empty(n)
    c = cost_bps / 1e4
    for k, s in enumerate(shifts):
        p = np.roll(pos, s)
        cost = np.abs(np.diff(p, prepend=0.0)) * c
        totals[k] = np.prod(1 + p * r - cost) - 1
    return {"random_median": float(np.median(totals)), "random_p05": float(np.percentile(totals, 5)),
            "random_p95": float(np.percentile(totals, 95)),
            "percentile": float((totals < actual_total_return).mean() * 100), "n": int(n)}
