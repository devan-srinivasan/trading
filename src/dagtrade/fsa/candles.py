"""CandleView: the candles every node may see at a given moment, at every interval the FSA uses.

Built once per backtest (or once per live cycle). For each interval it holds the candles (open-time index, `avail`
= when the candle closed) and, per node, the window its `history` Range describes for every candle. At decision time
`now` a node may only see candles with avail <= now: `latest(tf, now)` is the newest such candle, and the node's
window ends there. That is the whole no-lookahead guarantee.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .node import OHLCV, Node


class CandleView:
    def __init__(self, data, ticker: str, start, end, tfs, until: pd.Timestamp | None = None):
        self.frames: dict[str, pd.DataFrame] = {}
        self._avail: dict[str, np.ndarray] = {}
        self._ohlcv: dict[str, pd.DataFrame] = {}
        self._bounds: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        for tf in dict.fromkeys(tfs):
            df = data.bars(ticker, tf, start, end)
            if until is not None:
                df = df[df["avail"] <= until]
            self.frames[tf] = df
            self._avail[tf] = pd.DatetimeIndex(df["avail"]).asi8
            self._ohlcv[tf] = df[OHLCV]

    def frame(self, tf: str) -> pd.DataFrame:
        return self.frames[tf]

    def latest(self, tf: str, now: pd.Timestamp) -> int | None:
        """Index of the newest candle of `tf` that had closed by `now` (None if none yet)."""
        i = int(np.searchsorted(self._avail[tf], pd.Timestamp(now).value, side="right")) - 1
        return i if i >= 0 else None

    def window(self, node: Node, tf: str, idx: int) -> pd.DataFrame:
        """The node's candles when deciding at candle `idx` of `tf` (oldest first, the last row just closed)."""
        key = (id(node), tf)
        if key not in self._bounds:
            df = self.frames[tf]
            self._bounds[key] = node.history.bounds(pd.DatetimeIndex(df.index), pd.DatetimeIndex(df["avail"]))
        lo, hi = self._bounds[key]
        return self._ohlcv[tf].iloc[lo[idx]: hi[idx]]
