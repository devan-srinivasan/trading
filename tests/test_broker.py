import numpy as np
import pandas as pd
import pytest

from dagtrade.sim import SimConfig, simulate
from conftest import make_bars

ZERO = SimConfig(capital=1000.0, commission_bps=0, slippage_bps=0)
USD = 1000.0


def intents(bars, target, stop=None, take=None, usd=USD):
    n = len(bars)
    return pd.DataFrame({"target": np.asarray(target, float), "usd": np.full(n, float(usd)),
                         "stop": np.full(n, np.nan) if stop is None else np.asarray(stop, float),
                         "take": np.full(n, np.nan) if take is None else np.asarray(take, float)}, index=bars.index)


def test_fills_at_next_open_not_signal_close():
    bars = make_bars([10, 11, 12, 13], [10, 11, 12, 13], [10, 11, 12, 13], [10.5, 11.5, 12.5, 13.5])
    r = simulate(bars, intents(bars, [1, 1, 1, 1]), ZERO)
    f = r.fills.iloc[0]
    assert f.bar == 1 and f.price == 11          # decided at close of bar 0, filled at open of bar 1
    # final bar liquidation at its close
    assert r.fills.iloc[-1].reason == "end" and r.fills.iloc[-1].price == 13.5
    assert r.fills.iloc[0].qty == pytest.approx(1000 / 11)                 # $1000 / open price
    assert r.equity.iloc[-1] == pytest.approx(1000 + 1000 / 11 * (13.5 - 11))


def test_stop_fills_at_stop_and_gap_fills_at_open():
    # bar1 entry @10. bar2 low touches stop 9.5 -> fill 9.5.
    bars = make_bars([10, 10, 10, 8], [10, 10.5, 10, 8], [10, 9.8, 9.0, 7.5], [10, 10, 9.4, 8])
    r = simulate(bars, intents(bars, [1, 1, 1, 1], stop=[9.5] * 4), ZERO)
    stops = r.fills[r.fills.reason == "stop"]
    assert len(stops) == 1 and stops.iloc[0].price == 9.5 and stops.iloc[0].bar == 2
    # gap: open below the stop -> fill at the open
    bars = make_bars([10, 10, 8.0, 8], [10, 10, 8.2, 8], [10, 10, 7.5, 8], [10, 10, 8, 8])
    r = simulate(bars, intents(bars, [1, 1, 1, 1], stop=[9.5] * 4), ZERO)
    assert r.fills[r.fills.reason == "stop"].iloc[0].price == 8.0


def test_stop_wins_when_bar_touches_both():
    bars = make_bars([10, 10, 10, 10], [10, 10, 12, 10], [10, 10, 9, 10], [10, 10, 10, 10])
    r = simulate(bars, intents(bars, [1, 1, 1, 1], stop=[9.5] * 4, take=[11.5] * 4), ZERO)
    assert (r.fills.reason == "stop").sum() == 1 and (r.fills.reason == "take").sum() == 0


def test_no_reentry_after_stop_until_target_changes():
    flat = [10] * 8
    bars = make_bars(flat, flat, [10, 10, 9, 10, 10, 10, 10, 10], flat)
    tgt = [1, 1, 1, 1, 0, 1, 1, 1]
    r = simulate(bars, intents(bars, tgt, stop=[9.5] * 8), ZERO)
    entries = r.fills[r.fills.qty > 0]
    assert list(entries.bar) == [1, 6]           # initial entry, then only after target went 0 -> 1
    assert list(r.fills[r.fills.reason == "stop"].bar) == [2]


def test_short_pnl_and_costs():
    bars = make_bars([10, 10, 9, 8], [10, 10, 9, 8], [10, 10, 9, 8], [10, 10, 9, 8])
    r = simulate(bars, intents(bars, [-1, -1, -1, -1]), SimConfig(capital=1000.0, commission_bps=10, slippage_bps=0))
    t = r.trades.iloc[0]
    assert t.side == "short" and t.entry_price == 10 and t.exit_price == 8
    gross = 100 * 2.0
    comm = 100 * 10 * 0.001 + 100 * 8 * 0.001                                 # 100 shares = $1000 / $10
    assert t.pnl == pytest.approx(gross - comm)
    assert r.equity.iloc[-1] == pytest.approx(1000 + gross - comm)


def test_slippage_hurts_both_ways():
    bars = make_bars([10, 10, 10, 10], [10] * 4, [10] * 4, [10] * 4)
    r = simulate(bars, intents(bars, [1, 1, 1, 1]), SimConfig(capital=1000.0, commission_bps=0, slippage_bps=100))
    assert r.equity.iloc[-1] < 1000 * 0.985


def test_sign_flip_is_two_trades():
    bars = make_bars(list(range(10, 16)), list(range(10, 16)), list(range(10, 16)), list(range(10, 16)))
    r = simulate(bars, intents(bars, [1, 1, -1, -1, -1, -1]), ZERO)
    assert list(r.trades.side) == ["long", "short"]


def test_trade_from_blocks_early_signals():
    bars = make_bars([10] * 6, [10] * 6, [10] * 6, [10] * 6)
    r = simulate(bars, intents(bars, [1] * 6), ZERO, trade_from=bars.index[3])
    assert r.fills.iloc[0].bar == 3              # earliest: open of the first allowed bar


def test_position_size_is_the_dollar_amount_not_a_share_of_equity():
    bars = make_bars([10, 10, 10, 10], [10] * 4, [10] * 4, [10] * 4)
    for usd in (250.0, 4000.0):
        r = simulate(bars, intents(bars, [1, 1, 1, 1], usd=usd), ZERO)
        assert r.fills.iloc[0].qty == pytest.approx(usd / 10)
