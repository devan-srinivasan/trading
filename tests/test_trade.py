"""The Trade state: turns the previous state's signal into an order, holds it, closes it. Daily candles you control:
candle i opens on 2 Jan 2024 + i days, closes (is available) the next midnight, so a decision at candle i's close
fills at candle i+1's open."""
import pandas as pd
import pytest

from conftest import T
from dagtrade import FSA, Node, Range, Trade
from dagtrade.fsa import backtest
from dagtrade.sim import SimConfig

FLAT = [100.0] * 20
ZERO = SimConfig(capital=10_000, commission_bps=0, slippage_bps=0)
D0 = pd.Timestamp("2024-01-02", tz="UTC")


def n(ts) -> int:
    return (pd.Timestamp(ts) - D0).days


class Orders(Node):
    """Emits the dict you give it on the given candle numbers; waits otherwise."""
    history = Range(candles=3)
    max_wait = None

    def __init__(self, plan, **kw):
        super().__init__(plan=tuple(sorted(plan.items(), key=lambda kv: kv[0])), **kw)

    def run(self, candles, ctx):
        r = dict(self.plan).get(n(candles.index[-1]))
        return dict(r) if r else None


class ExitOn(Node):
    history = Range(candles=3)

    def __init__(self, days, **kw):
        super().__init__(days=tuple(days), **kw)

    def run(self, candles, ctx):
        return n(candles.index[-1]) in self.days


def bt(fixed_data, plan, o=None, h=None, l=None, c=None, cfg=ZERO, **trade_kw):
    o = o or FLAT
    data = fixed_data(o, h or o, l or o, c or o)
    return backtest(FSA.chain(Orders(plan), T(**trade_kw)), data, "FIX", "2024-01-02", "2024-02-28", cfg=cfg)


def fills(res):
    return [(n(f.time), f.qty, f.price, f.reason) for f in res.fills.itertuples()]


def legs(res, i):
    row = res.trace.iloc[i]
    return row["stop"], row["take"]


def test_buy_fills_at_the_next_open_with_levels_from_the_signal_candle(fixed_data):
    res = bt(fixed_data, {3: dict(buy=True)}, take_profit=0.10, stop_loss=0.05)
    assert fills(res)[0] == (4, 10.0, 100.0, "market")                         # $1000 / $100 at candle 4's open
    assert legs(res, 4) == (pytest.approx(95.0), pytest.approx(110.0))
    assert [(e["from"], e["to"]) for e in res.events] == [("orders", "trade")]  # still holding at the end


def test_take_profit_and_stop_loss_hit_by_high_and_low_then_back_to_the_start(fixed_data):
    hi = list(FLAT); hi[6] = 112
    res = bt(fixed_data, {3: dict(buy=True)}, h=hi, take_profit=0.10, stop_loss=0.05)
    assert fills(res)[1] == (6, -10.0, pytest.approx(110.0), "take")
    assert [(e["from"], e["to"], e["outcome"]) for e in res.events][-1] == ("trade", "orders", "ok")
    lo = list(FLAT); lo[5] = 94
    res = bt(fixed_data, {3: dict(buy=True)}, l=lo, take_profit=0.10, stop_loss=0.05)
    assert fills(res)[1] == (5, -10.0, 95.0, "stop")


def test_stop_wins_when_a_candle_touches_both_and_gaps_fill_at_the_open(fixed_data):
    hi, lo = list(FLAT), list(FLAT)
    hi[6], lo[6] = 115, 90
    assert fills(bt(fixed_data, {3: dict(buy=True)}, h=hi, l=lo))[1][3] == "stop"
    o = list(FLAT); o[6] = 90
    assert fills(bt(fixed_data, {3: dict(buy=True)}, o=o, h=o, l=o, c=o))[1][2:] == (90.0, "stop")


def test_only_one_position_at_a_time_and_signals_while_holding_are_ignored(fixed_data):
    res = bt(fixed_data, {3: dict(buy=True), 5: dict(buy=True), 9: dict(buy=True)}, take_profit=5.0, stop_loss=0.9)
    assert [f for f in fills(res) if f[1] > 0] == [(4, 10.0, 100.0, "market")]


def test_exit_check_closes_at_the_next_open(fixed_data):
    data = fixed_data(FLAT, FLAT, FLAT, FLAT)
    fsa = FSA.chain(Orders({3: dict(buy=True)}), T(take_profit=5.0, stop_loss=0.9, exit=ExitOn([6])))
    res = backtest(fsa, data, "FIX", "2024-01-02", "2024-02-28", cfg=ZERO)
    assert fills(res)[1] == (7, -10.0, 100.0, "exit signal")


def test_max_hold_closes_after_n_candles(fixed_data):
    res = bt(fixed_data, {3: dict(buy=True)}, max_hold=3)
    assert fills(res)[1][:1] == (7,) and fills(res)[1][3] == "max_hold (3 candles)"


def test_trailing_stop_ratchets_up_never_down(fixed_data):
    c = [100.0] * 4 + [104, 108, 112, 110, 105, 100] + [100.0] * 10
    res = bt(fixed_data, {3: dict(buy=True)}, o=c, h=c, l=c, c=c, stop_loss=0.05, take_profit=5.0, trail=True)
    stops = res.trace["stop"].iloc[4:9].dropna()
    assert stops.is_monotonic_increasing and stops.max() == pytest.approx(112 * 0.95)
    assert fills(res)[1][3] == "stop"


def test_sell_opens_a_short_with_levels_on_the_other_side(fixed_data):
    res = bt(fixed_data, {3: dict(sell=True)}, take_profit=0.10, stop_loss=0.05)
    assert fills(res)[0][:2] == (4, -10.0) and legs(res, 4) == (pytest.approx(105.0), pytest.approx(90.0))


def test_take_profit_and_stop_loss_are_required_everything_else_defaults(fixed_data):
    for bad in (dict(), dict(take_profit=0.1), dict(stop_loss=0.05)):
        with pytest.raises(TypeError):
            Trade(**bad)
    with pytest.raises(ValueError, match="both"):
        Trade(take_profit=None, stop_loss=0.05)
    with pytest.raises(ValueError, match="fractions"):
        Trade(take_profit=110, stop_loss=95)                      # prices, not fractions
    with pytest.raises(ValueError, match="between 0 and 1"):
        T(size=1.5)


def test_dollar_orders_and_a_gap_above_the_target_fills_at_the_open(fixed_data):
    o = [100.0] * 4 + [100, 100, 100, 100, 120] + [120.0] * 11
    res = bt(fixed_data, {3: dict(buy=True)}, o=o, notional=500, take_profit=0.10)
    assert fills(res) == [(4, 5.0, 100.0, "market"), (8, -5.0, 120.0, "take")]
    assert res.equity.iloc[-1] == pytest.approx(10_100.0)


def test_signal_can_set_absolute_levels_and_size_per_order(fixed_data):
    res = bt(fixed_data, {3: dict(buy=True, take_profit_price=120.0, stop_loss_price=97.0, usd=250.0)})
    assert fills(res)[0][1] == pytest.approx(2.5) and legs(res, 4) == (97.0, 120.0)
    res = bt(fixed_data, {3: dict(buy=True, qty=4, take_profit=0.2, stop_loss=0.01)})
    assert fills(res)[0][1] == pytest.approx(4.0) and legs(res, 4) == (pytest.approx(99.0), pytest.approx(120.0))


def test_inconsistent_levels_are_rejected(fixed_data):
    with pytest.raises(ValueError, match="stop < price < take"):
        bt(fixed_data, {3: dict(buy=True, stop_loss_price=105.0)})
    with pytest.raises(ValueError, match="take < price < stop"):
        bt(fixed_data, {3: dict(sell=True, take_profit_price=105.0)})


def test_limit_entry_fills_at_the_limit_or_fails_back_to_the_start(fixed_data):
    lo = list(FLAT); lo[4] = 98.0                                   # candle 4 dips to 98: a buy limit at 99 fills
    res = bt(fixed_data, {3: dict(buy=True, price=99.0)}, l=lo)
    assert fills(res)[0] == (4, pytest.approx(1000 / 99), 99.0, "limit")
    assert legs(res, 4) == (pytest.approx(99 * 0.95), pytest.approx(99 * 1.10))
    res = bt(fixed_data, {3: dict(buy=True, price=99.0)})            # never trades down to 99
    assert res.fills.empty
    (ev,) = [e for e in res.events if e["from"] == "trade"]
    assert ev["outcome"] == "fail" and "not filled" in ev["reason"] and ev["to"] == "orders"


def test_buy_at_a_price_with_take_profit_and_stop_loss_prices_end_to_end(fixed_data):
    o = [100.0] * 4 + [100, 100, 104, 108, 109] + [109.0] * 11
    h = list(o); h[8] = 112
    res = bt(fixed_data, {3: dict(buy=True, price=100.0, take_profit_price=110.0, stop_loss_price=95.0)}, o=o, h=h)
    assert fills(res)[:2] == [(4, 10.0, 100.0, "limit"), (8, -10.0, 110.0, "take")]


def test_percent_sizing_reinvests_profits(fixed_data):
    o = [100.0] * 5 + [200.0] * 15                                   # price doubles while the first trade is open
    data = fixed_data(o, o, o, o)
    fsa = FSA.chain(Orders({2: dict(buy=True), 8: dict(buy=True)}),
                    T(size=0.5, take_profit=5.0, stop_loss=0.5, exit=ExitOn([6])))
    res = backtest(fsa, data, "FIX", "2024-01-02", "2024-02-28", cfg=ZERO)
    buys = res.fills[res.fills.qty > 0]
    assert buys.iloc[0].qty * buys.iloc[0].price == pytest.approx(5_000)            # half of $10k
    assert buys.iloc[1].qty * buys.iloc[1].price == pytest.approx(7_500)            # half of $15k after the win
