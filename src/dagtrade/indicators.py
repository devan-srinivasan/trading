"""A few plain helper functions for use INSIDE a node's run(): they take pandas Series, return pandas Series."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n).mean()


def ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI, 0..100."""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(100.0).where(up.notna())


def atr(candles: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range: the typical candle size in price units."""
    pc = candles["close"].shift(1)
    tr = pd.concat([candles["high"] - candles["low"], (candles["high"] - pc).abs(), (candles["low"] - pc).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
