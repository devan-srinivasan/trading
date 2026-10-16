"""Timeframe helpers. A timeframe string is `<int><unit>` with unit in m/h/d/w (e.g. 1m, 5m, 1h, 1d)."""
from __future__ import annotations

import re

import pandas as pd

_UNITS = {"m": 1, "h": 60, "d": 1440, "w": 7 * 1440}
_RE = re.compile(r"^(\d+)([mhdw])$")

# timeframes both providers can serve natively; anything else is derived by resampling
NATIVE_TFS = ["1m", "5m", "15m", "30m", "1h", "1d"]


def minutes(tf: str) -> int:
    m = _RE.match(tf)
    if not m:
        raise ValueError(f"bad timeframe {tf!r} (use e.g. 1m, 5m, 1h, 1d, 1w)")
    return int(m.group(1)) * _UNITS[m.group(2)]


def delta(tf: str) -> pd.Timedelta:
    return pd.Timedelta(minutes=minutes(tf))


def is_intraday(tf: str) -> bool:
    return minutes(tf) < 1440


def finest(tfs: list[str]) -> str:
    return min(tfs, key=minutes)


def divides(small: str, big: str) -> bool:
    """True if `big` is a whole multiple of `small` (so big can be built from small)."""
    return minutes(big) % minutes(small) == 0


def native_base(tf: str) -> str:
    """Largest native timeframe that `tf` can be built from."""
    cands = [n for n in NATIVE_TFS if divides(n, tf)]
    if not cands:
        raise ValueError(f"cannot build {tf!r} from native timeframes {NATIVE_TFS}")
    return max(cands, key=minutes)
