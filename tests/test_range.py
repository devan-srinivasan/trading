import pandas as pd
import pytest

from dagtrade import Range
from dagtrade.range import parse_interval, parse_time

NOW = pd.Timestamp("2024-06-10 15:30", tz="UTC")


def test_time_formats():
    assert parse_time("now", NOW) == NOW
    assert parse_time("-30d", NOW) == NOW - pd.Timedelta(days=30)
    assert parse_time("-12h", NOW) == NOW - pd.Timedelta(hours=12)
    assert parse_time("2024-03-01", NOW) == pd.Timestamp("2024-03-01", tz="UTC")
    assert parse_time("2024-03-01-09-30", NOW) == pd.Timestamp("2024-03-01 09:30", tz="UTC")
    assert parse_time("2024-03-01T09:30", NOW) == pd.Timestamp("2024-03-01 09:30", tz="UTC")


def test_interval_aliases_and_validation():
    assert [parse_interval(x) for x in ("d", "h", "m", "w", "5m", "4H")] == ["1d", "1h", "1m", "1w", "5m", "4h"]
    for bad in ("x", "0", "d5"):
        with pytest.raises(ValueError):
            Range(interval=bad)
    with pytest.raises(ValueError, match="cannot read time"):
        Range(start="yesterday")


def test_resolve_never_looks_past_now():
    start, end = Range(start="2024-06-01", end="2030-01-01").resolve(NOW)
    assert start == pd.Timestamp("2024-06-01", tz="UTC") and end == NOW


def test_bounds_slide_with_relative_start_and_anchor_with_absolute():
    avail = pd.date_range("2024-01-02", periods=30, freq="D", tz="UTC")            # candle i closes at avail[i]
    opens = avail - pd.Timedelta(days=1)
    lo, hi = Range(start="-5d", interval="d").bounds(opens, avail)
    assert hi.tolist() == list(range(1, 31))                                       # never includes the future
    assert (hi - lo)[10] <= 6 and (hi - lo)[0] == 1                                # sliding window
    lo, hi = Range(start="2024-01-10", interval="d").bounds(opens, avail)
    assert opens[lo[20]] >= pd.Timestamp("2024-01-10", tz="UTC") and lo[5] >= 5     # fixed anchor, grows
    lo, hi = Range(start="2024-01-05", end="2024-01-10", interval="d").bounds(opens, avail)
    assert hi[25] == hi[29] and opens[hi[29] - 1] < pd.Timestamp("2024-01-10", tz="UTC")   # end cuts the window


def test_span_for_history_loading():
    assert Range(start="-90d").span(NOW) == pd.Timedelta(days=90)
    assert Range(start="2024-06-01").span(NOW) == NOW - pd.Timestamp("2024-06-01", tz="UTC")
    assert Range().span(NOW) is None


def test_candle_count_windows_and_the_dag_interval():
    avail = pd.date_range("2024-01-02", periods=30, freq="D", tz="UTC")
    opens = avail - pd.Timedelta(days=1)
    lo, hi = Range(candles=5).bounds(opens, avail)
    assert (hi - lo).tolist() == [1, 2, 3, 4] + [5] * 26                # the last 5 candles, fewer at the start
    assert Range(candles=5).tf is None and Range(interval="5m").tf == "5m"
    with pytest.raises(ValueError):
        Range(start="-5d", candles=5)
    # loading enough calendar time: 200 daily candles ~ 280 days; 200 five-minute candles ~ 3 sessions
    assert Range(candles=200).span(NOW, "1d") >= pd.Timedelta(days=280)
    assert pd.Timedelta(days=3) <= Range(candles=200).span(NOW, "5m") <= pd.Timedelta(days=12)
