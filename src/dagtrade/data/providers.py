"""Market-data providers. Each returns the standard bars frame (UTC open-time index, OHLCV columns),
split/dividend adjusted, regular trading hours only."""
from __future__ import annotations

import logging
import os
from typing import Protocol

import numpy as np
import pandas as pd

from .. import tf as tfm
from .resample import OHLCV, regular_hours

log = logging.getLogger("dagtrade.data")


class Provider(Protocol):
    name: str

    def fetch(self, ticker: str, tf: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...


def _clean(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    df = df[OHLCV].astype(float).dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
    if tfm.is_intraday(tf):
        df = regular_hours(df)
    return df


class YahooProvider:
    """yfinance: no key needed. Unofficial and limited intraday history (1m~7d, 5m-30m~60d, 1h~730d)."""
    name = "yahoo"
    _INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}

    def fetch(self, ticker, tf, start, end):
        import yfinance as yf
        if tf not in self._INTERVAL:
            raise ValueError(f"yahoo cannot serve {tf}")
        now = pd.Timestamp.now(tz="UTC")
        lookback = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "1h": 729}.get(tf)
        chunk = pd.Timedelta(days=7 if tf == "1m" else 3650)
        if lookback:
            start = max(start, now - pd.Timedelta(days=lookback))
        end = min(end, now + pd.Timedelta(days=1))
        parts, a = [], start
        while a < end:
            b = min(a + chunk, end)
            d = yf.download(ticker, start=a.tz_localize(None), end=b.tz_localize(None),
                            interval=self._INTERVAL[tf], auto_adjust=True, progress=False, threads=False)
            if d is not None and not d.empty:
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                parts.append(d.rename(columns=str.lower))
            a = b
        if not parts:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        d = pd.concat(parts)
        if tf == "1d":
            d.index = pd.DatetimeIndex(d.index).tz_localize(None).tz_localize("UTC")  # date @ 00:00 UTC
        return _clean(d, tf)


class AlpacaProvider:
    """Alpaca market data (free account). Tries the consolidated SIP feed (15-min delayed on the free plan)
    and falls back to the single-venue IEX feed if the plan rejects it."""
    name = "alpaca"

    def __init__(self, key: str | None = None, secret: str | None = None, feed: str | None = None):
        self.key = key or os.getenv("ALPACA_KEY")
        self.secret = secret or os.getenv("ALPACA_SECRET")
        self.feed = (feed or os.getenv("ALPACA_FEED") or "sip").lower()
        if not (self.key and self.secret):
            raise RuntimeError("ALPACA_KEY / ALPACA_SECRET not set")

    def fetch(self, ticker, tf, start, end):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import Adjustment, DataFeed

        units = {"m": TimeFrameUnit.Minute, "h": TimeFrameUnit.Hour, "d": TimeFrameUnit.Day}
        n, u = int(tf[:-1]), tf[-1]
        client = StockHistoricalDataClient(self.key, self.secret)
        # keep the end >15 min in the past so SIP is accepted on the free plan
        if self.feed != "iex":
            end = min(end, pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=16))

        def run(feed):
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame(n, units[u]),
                                   start=start.to_pydatetime(), end=end.to_pydatetime(),
                                   adjustment=Adjustment.ALL, feed=DataFeed(feed))
            return client.get_stock_bars(req).df

        try:
            raw = run(self.feed)
        except Exception as e:  # noqa: BLE001  (plan/permission errors surface as APIError)
            if self.feed != "iex":
                log.warning("alpaca %s feed failed (%s); falling back to iex", self.feed, e)
                raw = run("iex")
            else:
                raise
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))
        raw = raw.reset_index(level=0, drop=True) if isinstance(raw.index, pd.MultiIndex) else raw
        if tf == "1d":  # alpaca stamps daily bars at 04:00/05:00 UTC; normalise to date @ 00:00 UTC
            raw.index = pd.DatetimeIndex(raw.index.tz_convert("America/New_York").tz_localize(None).normalize()).tz_localize("UTC")
        return _clean(raw, tf)


class SyntheticProvider:
    """Deterministic fake market (regime-switching geometric random walk) for tests and offline demos."""
    name = "synthetic"

    def __init__(self, seed: int = 7):
        self.seed = seed

    def fetch(self, ticker, tf, start, end):
        seed = (self.seed + sum(map(ord, ticker))) % (2**32)
        rng = np.random.default_rng(seed)
        days = pd.bdate_range(start.tz_localize(None).normalize(), end.tz_localize(None).normalize())
        if tf == "1d":
            idx = pd.DatetimeIndex(days).tz_localize("UTC")
            n = len(idx)
        else:
            step = tfm.minutes(tf)
            per_day = (390) // step
            offs = pd.to_timedelta(9 * 60 + 30 + np.arange(per_day) * step, unit="m")
            ny = (days.to_numpy()[:, None] + offs.to_numpy()[None, :]).ravel()
            idx = pd.DatetimeIndex(ny).tz_localize("America/New_York").tz_convert("UTC")
            n = len(idx)
        bars_per_year = 252 if tf == "1d" else 252 * (390 // tfm.minutes(tf))
        # slow regime process: drift flips every few hundred bars
        regime = np.repeat(rng.choice([-1.0, 0.3, 1.0], size=n // 250 + 2), 250)[:n]
        vol = 0.22 / np.sqrt(bars_per_year)
        ret = regime * 0.25 / bars_per_year + vol * rng.standard_normal(n)
        close = 100 * np.exp(np.cumsum(ret))
        open_ = np.r_[100.0, close[:-1]] * np.exp(vol * 0.2 * rng.standard_normal(n))
        spread = np.abs(vol * rng.standard_normal(n)) * close
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        volu = rng.integers(10_000, 100_000, n).astype(float)
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volu}, index=idx)


def default_providers() -> list[Provider]:
    """Preference order: Alpaca (if keys) then Yahoo."""
    out: list[Provider] = []
    try:
        out.append(AlpacaProvider())
    except RuntimeError:
        pass
    out.append(YahooProvider())
    return out
