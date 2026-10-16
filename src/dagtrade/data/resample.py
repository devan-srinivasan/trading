"""Session-aware bar resampling.

Bars frame convention (everywhere in dagtrade):
  index  : bar OPEN time, tz-aware UTC
  columns: open high low close volume
  plus   : `avail` column = the moment the bar's data is fully known (its close).
Intraday bars are anchored to the 09:30 America/New_York open, so 1h bars are 9:30-10:30 etc.
Daily bars are indexed at 00:00 UTC of the trading date and become available at the next 00:00 UTC
(conservative: an intraday strategy can never peek at today's daily bar during today's session).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import tf as tfm

NY = "America/New_York"
SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60

OHLCV = ["open", "high", "low", "close", "volume"]


def regular_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Keep bars whose open falls within 09:30-16:00 New York time."""
    ny = df.index.tz_convert(NY)
    mins = ny.hour * 60 + ny.minute
    return df[(mins >= SESSION_OPEN_MIN) & (mins < SESSION_CLOSE_MIN)]


def add_avail(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    df = df.copy()
    df["avail"] = df.index + tfm.delta(tf)
    return df


def _agg(g) -> pd.DataFrame:
    return g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                 close=("close", "last"), volume=("volume", "sum"))


def resample(df: pd.DataFrame, from_tf: str, to_tf: str) -> pd.DataFrame:
    """Build `to_tf` bars from `from_tf` bars (to_tf must be a multiple of from_tf)."""
    if from_tf == to_tf:
        return add_avail(df[OHLCV], to_tf)
    if not tfm.divides(from_tf, to_tf):
        raise ValueError(f"{to_tf} is not a multiple of {from_tf}")
    base = df[OHLCV]
    if tfm.is_intraday(to_tf):
        ny = base.index.tz_convert(NY)
        mins = ny.hour * 60 + ny.minute - SESSION_OPEN_MIN
        bucket = mins // tfm.minutes(to_tf)
        keys = [ny.normalize(), bucket]
        out = _agg(base.groupby(keys))
        # bucket open time = session open + bucket*tf  (computed in NY, stored in UTC)
        day = out.index.get_level_values(0)
        b = out.index.get_level_values(1)
        opens = (day + pd.to_timedelta(SESSION_OPEN_MIN + b * tfm.minutes(to_tf), unit="m"))
        out.index = pd.DatetimeIndex(opens).tz_convert("UTC")
        # a candle is only complete at its NOMINAL end (capped at the 16:00 close), never earlier: when the last
        # constituent bar has merely not arrived yet (live), the candle must not be treated as finished.
        close_utc = (day + pd.to_timedelta(SESSION_CLOSE_MIN, unit="m")).tz_convert("UTC")
        out["avail"] = np.minimum(out.index + tfm.delta(to_tf), pd.DatetimeIndex(close_utc))
        return out.sort_index()
    # daily / weekly: group on trading date (daily source) or NY date (intraday source)
    if tfm.is_intraday(from_tf):
        dates = base.index.tz_convert(NY).tz_localize(None).normalize()
        daily = _agg(base.groupby(dates))
        daily.index = daily.index.tz_localize("UTC")
        base, from_tf = daily, "1d"
    if to_tf == "1d":
        return add_avail(base, "1d")
    n_days = tfm.minutes(to_tf) // 1440
    if to_tf.endswith("w"):
        # weeks anchored to Monday; label = Monday 00:00 UTC
        wk = base.index.tz_localize(None).to_period("W-SUN").start_time
        out = _agg(base.groupby(wk))
        out.index = out.index.tz_localize("UTC")
        out["avail"] = out.index + tfm.delta(to_tf)
        return out
    grp = np.arange(len(base)) // n_days
    out = _agg(base.groupby(grp))
    out.index = base.index[::n_days][: len(out)]
    out["avail"] = out.index + tfm.delta(to_tf)
    return out
