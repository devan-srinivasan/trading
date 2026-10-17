"""Lookahead check for an FSA: replay with the future hidden and compare.

The engine only ever hands a node candles that had closed, so a correct pipeline cannot leak. This check guards that
promise end to end (data loading, resampling, windows): it backtests once on everything, then again with all candles
after a cutoff removed. Every state transition and every fill up to the cutoff must be identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .engine import backtest


@dataclass
class CausalReport:
    ok: bool
    leaks: list = field(default_factory=list)          # (what, first difference)
    checked: int = 0

    def __str__(self):
        if self.ok:
            return f"causal: OK ({self.checked} cutoffs checked, nothing changed when the future was hidden)"
        lines = ["LOOKAHEAD DETECTED - the replay changed when future candles were hidden:"]
        lines += [f"  - {w}: {d}" for w, d in self.leaks]
        return "\n".join(lines)


def _events(res, t):
    return [(round(e["t"]), e["from"], e["to"], e["outcome"]) for e in res.events if e["t"] <= t]


def _fills(res, t):
    f = res.fills[(res.fills["time"].astype("int64") / 1e9 < t) & (res.fills["reason"] != "end")]
    return [(int(x.time.value // 10**9), round(float(x.qty), 6), round(float(x.price), 4)) for x in f.itertuples()]


def verify_causal(make_fsa, data, ticker, start, end, n_cutoffs: int = 4, seed: int = 0, **kw) -> CausalReport:
    """make_fsa: a function returning a FRESH FSA (nodes may keep state between calls of a run)."""
    full = backtest(make_fsa(), data, ticker, start, end, **kw)
    grid = pd.DatetimeIndex(full.bars["avail"])[full.start_idx:]
    if len(grid) < 50:
        return CausalReport(True, [], 0)
    rng = np.random.default_rng(seed)
    lo, hi = int(len(grid) * 0.3), len(grid) - 2
    cuts = sorted(rng.choice(np.arange(lo, hi), size=min(n_cutoffs, hi - lo), replace=False))
    leaks = []
    for c in cuts:
        cut = grid[c]
        part = backtest(make_fsa(), data, ticker, start, end, until=cut, **kw)
        t = cut.timestamp()
        for what, a, b in (("transitions", _events(full, t), _events(part, t)), ("fills", _fills(full, t), _fills(part, t))):
            if a != b:
                k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                leaks.append((f"{what} before {cut}", f"first difference at item {k}"))
    return CausalReport(not leaks, leaks, len(cuts))
