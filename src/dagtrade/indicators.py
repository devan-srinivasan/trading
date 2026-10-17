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


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series]:
    """(macd line, signal line)."""
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return line, line.ewm(span=signal, adjust=False).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(lower, middle, upper) bands."""
    mid, sd = close.rolling(n).mean(), close.rolling(n).std()
    return mid - k * sd, mid, mid + k * sd


def stochastic(candles: pd.DataFrame, n: int = 14, smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    """(%K, %D), 0..100."""
    lo, hi = candles["low"].rolling(n).min(), candles["high"].rolling(n).max()
    k = 100 * (candles["close"] - lo) / (hi - lo).replace(0, np.nan)
    k = k.rolling(smooth).mean()
    return k, k.rolling(smooth).mean()


def supertrend(candles: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    """Supertrend direction per candle: +1 uptrend, -1 downtrend (NaN during warm-up)."""
    h, l, c = candles["high"].to_numpy(), candles["low"].to_numpy(), candles["close"].to_numpy()
    a = atr(candles, n).to_numpy()
    mid = (h + l) / 2
    up_band, dn_band = mid - mult * a, mid + mult * a
    direction = np.full(len(c), np.nan)
    fu = fd = np.nan
    d = 1.0
    for i in range(len(c)):
        if np.isnan(a[i]):
            continue
        fu = up_band[i] if np.isnan(fu) or c[i - 1] < fu else max(up_band[i], fu)
        fd = dn_band[i] if np.isnan(fd) or c[i - 1] > fd else min(dn_band[i], fd)
        if d < 0 and c[i] > fd:
            d = 1.0
        elif d > 0 and c[i] < fu:
            d = -1.0
        direction[i] = d
    return pd.Series(direction, index=candles.index)
