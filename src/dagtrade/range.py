"""Range: which candles a node looks at.

    Range(start="2024-01-01", end="now", interval="d")     # everything since 1 Jan, daily candles
    Range(start="-30d", interval="h")                      # the last 30 days, hourly candles (slides with time)
    Range(start="2024-03-01-09-30", end="2024-03-01-16-00", interval="5m")

start / end accept
  * "yyyy-mm-dd"  or  "yyyy-mm-dd-hh-mm"  (also "yyyy-mm-ddThh:mm"); read as UTC
  * "-90d" / "-12h" / "-30m" / "-4w"  = that long before the moment the node is deciding
  * "now"  = the moment the node is deciding (default for `end`);  start=None = as far back as data is loaded
interval: "m" "5m" "15m" "30m" "h" "4h" "d" "w" (also "1m", "1h", "1d", ...)

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


@dataclass(frozen=True)
class Range:
    start: str | None = None
    end: str = "now"
    interval: str = "d"

    def __post_init__(self):
        object.__setattr__(self, "interval", parse_interval(self.interval))
        probe = pd.Timestamp("2000-01-01", tz="UTC")
        for v in (self.start, self.end):
            if v is not None:
                parse_time(v, probe)

    @property
    def tf(self) -> str:
        return self.interval

    def resolve(self, now: pd.Timestamp) -> tuple[pd.Timestamp | None, pd.Timestamp]:
        """(start, end) as timestamps for a node deciding at `now`; end is never later than now."""
        start = None if self.start is None else parse_time(self.start, now)
        return start, min(parse_time(self.end, now), now)

    def bounds(self, opens: pd.DatetimeIndex, avail: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
        """For every candle i (deciding at avail[i]): the slice [lo[i], hi[i]) of candles it may look at."""
        n = len(opens)
        a = avail.asi8
        lo = np.zeros(n, dtype=np.int64) if self.start is None else opens.asi8.searchsorted(_vec(self.start, a), "left")
        ends = np.minimum(_vec(self.end, a), a)
        hi = a.searchsorted(ends, "right")
        return lo, hi

    def span(self, at: pd.Timestamp) -> pd.Timedelta | None:
        """How much history this range reaches back from `at` (None = unbounded)."""
        if self.start is None:
            return None
        return max(pd.Timedelta(0), at - parse_time(self.start, at))
