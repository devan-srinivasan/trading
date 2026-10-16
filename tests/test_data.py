import numpy as np
import pandas as pd

from dagtrade.data import resample
from dagtrade.data.providers import SyntheticProvider


def test_5m_to_1h_session_anchored_and_ohlc():
    p = SyntheticProvider()
    df = p.fetch("X", "5m", pd.Timestamp("2024-03-04", tz="UTC"), pd.Timestamp("2024-03-06", tz="UTC"))
    h = resample(df, "5m", "1h")
    ny = h.index.tz_convert("America/New_York")
    assert set(ny.minute) == {30} and ny[0].hour == 9           # 9:30, 10:30 ...
    first = df.iloc[:12]
    assert h.iloc[0].open == first.open.iloc[0] and h.iloc[0].close == first.close.iloc[-1]
    assert h.iloc[0].high == first.high.max() and h.iloc[0].volume == first.volume.sum()
    assert h.iloc[0].avail == first.index[-1] + pd.Timedelta(minutes=5)


def test_intraday_to_daily_available_next_midnight():
    p = SyntheticProvider()
    df = p.fetch("X", "5m", pd.Timestamp("2024-03-04", tz="UTC"), pd.Timestamp("2024-03-08", tz="UTC"))
    d = resample(df, "5m", "1d")
    assert len(d) == 5
    assert (d["avail"] - d.index == pd.Timedelta(days=1)).all()


def test_source_fetches_once_then_serves_from_cache(data):
    a = data.bars("ZZZ", "5m", "2024-03-04", "2024-03-08")
    calls = []
    orig = data.providers[0].fetch
    data.providers[0].fetch = lambda *x, **k: calls.append(1) or orig(*x, **k)
    b = data.bars("ZZZ", "5m", "2024-03-05", "2024-03-07")
    assert not calls and len(b) < len(a)
    c = data.bars("ZZZ", "15m", "2024-03-04", "2024-03-08")      # derived by resampling the cached 15m? no: 15m native
    assert len(c) > 0


def test_a_candle_that_has_not_closed_is_never_returned(data):
    # daily: at 12:00 on Friday 7 June the Friday candle is still forming; the Thursday one is the last complete
    d = data.bars("ZZZ", "1d", "2024-05-01", pd.Timestamp("2024-06-07 12:00", tz="UTC"))
    assert d.index[-1] == pd.Timestamp("2024-06-06", tz="UTC") and (d["avail"] <= pd.Timestamp("2024-06-07 12:00", tz="UTC")).all()
    # hourly (built from 5m): at 10:20 NY the 9:30-10:30 candle is incomplete even though 5m bars up to 10:20 exist
    now = pd.Timestamp("2024-06-07 10:20", tz="America/New_York").tz_convert("UTC")
    h = data.bars("ZZZ", "1h", "2024-06-05", now)
    assert h["avail"].max() <= now
    assert h.index[-1] == pd.Timestamp("2024-06-06 14:30", tz="America/New_York").tz_convert("UTC")
    full = data.bars("ZZZ", "1h", "2024-06-05", pd.Timestamp("2024-06-07 10:31", tz="America/New_York").tz_convert("UTC"))
    assert full.index[-1] == pd.Timestamp("2024-06-07 09:30", tz="America/New_York").tz_convert("UTC")
