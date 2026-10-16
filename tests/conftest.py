import numpy as np
import pandas as pd
import pytest

from dagtrade.data import BarStore, DataSource, SyntheticProvider


@pytest.fixture
def data(tmp_path):
    return DataSource([SyntheticProvider()], BarStore(tmp_path / "bars"))


def make_bars(o, h, l, c, start="2024-01-02"):
    idx = pd.date_range(start, periods=len(o), freq="D", tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0}, index=idx)


class FixedProvider:
    """Serves exactly the daily candles you give it (for tests that need controlled prices)."""
    name = "fixed"

    def __init__(self, ohlc: pd.DataFrame):
        self.ohlc = ohlc

    def fetch(self, ticker, tf, start, end):
        return self.ohlc


@pytest.fixture
def fixed_data(tmp_path):
    n = [0]

    def make(o, h, l, c, start="2024-01-02"):
        n[0] += 1                                   # a fresh cache per call so prices never leak between calls
        return DataSource([FixedProvider(make_bars(o, h, l, c, start))], BarStore(tmp_path / f"fixed{n[0]}"))
    return make


def T(signal, **kw):
    """Trade with the required take_profit / stop_loss filled in (override via kw)."""
    from dagtrade import Trade
    kw.setdefault("take_profit", 0.10)
    kw.setdefault("stop_loss", 0.05)
    return Trade(signal, **kw)
