"""Range: which candles a node looks at.

    Range(start="2024-01-01", end="now", interval="d")     # everything since 1 Jan, daily candles
    Range(start="-30d", interval="h")                      # the last 30 days, hourly candles (slides with time)
    Range(start="2024-03-01-09-30", end="2024-03-01-16-00", interval="5m")
    Range(candles=250)                                      # the last 250 candles of whatever interval the DAG runs on

interval: leave it out and the node runs on the DAG's own interval (picked per backtest / live run), so the same
strategy works on daily, hourly or 5-minute candles. Name one only when the node needs that timeframe specifically
(e.g. a daily trend filter inside a 5-minute strategy).
candles: a window counted in candles instead of time (instead of `start`). An indicator like a 200-candle average
needs 200 candles whatever their size, so this is how a node stays timeframe-agnostic.

start / end accept
  * "yyyy-mm-dd"  or  "yyyy-mm-dd-hh-mm"  (also "yyyy-mm-ddThh:mm"); read as UTC
  * "-90d" / "-12h" / "-30m" / "-4w"  = that long before the moment the node is deciding
  * "now"  = the moment the node is deciding (default for `end`);  start=None = as far back as data is loaded
interval: "m" "5m" "15m" "30m" "h" "4h" "d" "w" (also "1m", "1h", "1d", ...), or None (default) = the DAG's interval

A node never sees candles that had not closed yet at the moment it decides, whatever `end` says.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import tf as tfm

_REL = re.compile(r"^-(\d+)\s*([mhdw])$")
_ABS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[-t ](\d{2})(?:[-:](\d{2}))?(?:[-:](\d{2}))?)?$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_interval(s: str) -> str:
    s = s.strip().lower()
    s = {"m": "1m", "min": "1m", "h": "1h", "d": "1d", "w": "1w"}.get(s, s)
    tfm.minutes(s)                       # validates
    return s


def parse_time(s: str, now: pd.Timestamp) -> pd.Timestamp:
    s = s.strip().lower()
    if s == "now":
        return now
    m = _REL.match(s)
    if m:
        return now - pd.Timedelta(**{_UNIT[m.group(2)]: int(m.group(1))})
    m = _ABS.match(s)
    if m:
        y, mo, d, hh, mm, ss = (int(x) if x else 0 for x in m.groups())
        return pd.Timestamp(year=y, month=mo, day=d, hour=hh, minute=mm, second=ss, tz="UTC")
    raise ValueError(f"cannot read time {s!r}: use yyyy-mm-dd, yyyy-mm-dd-hh-mm, -30d, or now")


def _vec(spec: str, avail_ns: np.ndarray) -> np.ndarray:
    """parse_time for every decision moment at once (int64 ns)."""
    s = spec.strip().lower()
    if s == "now":
        return avail_ns
    m = _REL.match(s)
    if m:
        return avail_ns - int(pd.Timedelta(**{_UNIT[m.group(2)]: int(m.group(1))}).value)
    return np.full(len(avail_ns), parse_time(s, pd.Timestamp(0, tz="UTC")).value, dtype=np.int64)


# trading minutes in a regular US session, used to turn a candle count into calendar time for data loading
SESSION_MIN = 390


@dataclass(frozen=True)
class Range:
    start: str | None = None
    end: str = "now"
    interval: str | None = None
    candles: int | None = None

    def __post_init__(self):
        if self.interval is not None:
            object.__setattr__(self, "interval", parse_interval(self.interval))
        if self.candles is not None:
            if self.start is not None:
                raise ValueError("Range: give start OR candles, not both")
            if int(self.candles) < 1:
                raise ValueError("Range: candles must be >= 1")
            object.__setattr__(self, "candles", int(self.candles))
        probe = pd.Timestamp("2000-01-01", tz="UTC")
        for v in (self.start, self.end):
            if v is not None:
                parse_time(v, probe)

    @property
    def tf(self) -> str | None:
        return self.interval

    def __str__(self) -> str:
        win = f"last {self.candles} candles" if self.candles else f"{self.start or 'all'} → {self.end}"
        return f"{win} of {self.interval or 'the DAG interval'}"

    def resolve(self, now: pd.Timestamp) -> tuple[pd.Timestamp | None, pd.Timestamp]:
        """(start, end) as timestamps for a node deciding at `now`; end is never later than now."""
        start = None if self.start is None else parse_time(self.start, now)
        return start, min(parse_time(self.end, now), now)

    def bounds(self, opens: pd.DatetimeIndex, avail: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
        """For every candle i (deciding at avail[i]): the slice [lo[i], hi[i]) of candles it may look at."""
        n = len(opens)
        a = avail.asi8
        ends = np.minimum(_vec(self.end, a), a)
        hi = a.searchsorted(ends, "right")
        if self.candles:
            lo = np.maximum(hi - self.candles, 0)
        else:
            lo = np.zeros(n, dtype=np.int64) if self.start is None else opens.asi8.searchsorted(_vec(self.start, a), "left")
        return lo, hi

    def span(self, at: pd.Timestamp, tf: str | None = None) -> pd.Timedelta | None:
        """How much calendar time this range reaches back from `at` (None = unbounded). Used only to decide how
        much data to load, so a candle count is converted generously (weekends, holidays, short sessions).
        `tf` = the interval the node actually runs on (needed when the Range has none)."""
        if self.candles:
            m = tfm.minutes(self.interval or tf or "1d")
            if m >= 1440:
                days = self.candles * m / 1440 * 7 / 5                  # trading days -> calendar days
            else:
                days = self.candles / max(1, SESSION_MIN // m) * 7 / 5   # candles per session -> calendar days
            return pd.Timedelta(days=int(np.ceil(days * 1.15)) + 4)
        if self.start is None:
            return None
        return max(pd.Timedelta(0), at - parse_time(self.start, at))
