import numpy as np
import pandas as pd
import pytest

from conftest import T
from dagtrade import Graph, Node, Range, Trade
from dagtrade.sim import SimConfig, simulate

FLAT = [100.0] * 20


class Script(Node):
    """Emits a scripted signal: buy/sell/exit on the given candle numbers."""
    history = Range(start="-3d", interval="d")

    def __init__(self, buy=(), sell=(), leave=(), **kw):
        super().__init__(buy=tuple(buy), sell=tuple(sell), leave=tuple(leave), **kw)

    def run(self, candles, upstream):
        i = (candles.index[-1] - pd.Timestamp("2024-01-02", tz="UTC")).days
        return {"buy": i in self.buy, "sell": i in self.sell, "exit": i in self.leave}


def intents(fixed_data, trade_kw=None, signal_kw=None, o=None, h=None, l=None, c=None):
    o = o or FLAT; h = h or o; l = l or o; c = c or o
    data = fixed_data(o, h, l, c)
    res = Graph(T(Script(**(signal_kw or {})), **(trade_kw or {}))).run(
        data, "FIX", "2024-01-01", "2024-02-28", cache=False)
    return res.intents


def test_buy_signal_makes_a_1000_dollar_order_with_levels_from_the_signal_candle(fixed_data):
    out = intents(fixed_data, dict(take_profit=0.10, stop_loss=0.05), dict(buy=[3]))
    row = out.iloc[3]
    assert (row["target"], row["usd"], row["entry"]) == (1.0, 1000.0, True)
    assert row["stop"] == pytest.approx(95.0) and row["take"] == pytest.approx(110.0)
    assert out.iloc[2].target == 0 and out.iloc[2].usd == 0 and not out.iloc[2].entry
    assert out.iloc[4].target == 1.0 and not out.iloc[4].entry          # held, no new order


def test_signal_must_rise_to_trigger_and_cannot_enter_while_in_a_trade(fixed_data):
    out = intents(fixed_data, {}, dict(buy=[3, 4, 5, 9]))      # 3-4-5 is one sustained signal
    assert out.entry.tolist().count(True) == 1 and out.entry.iloc[3]               # 9 ignored: still holding


def test_take_profit_and_stop_loss_detect_hits_by_high_and_low(fixed_data):
    base = [100.0] * 20
    hi = list(base); hi[6] = 112                                                   # high touches the +10% target
    out = intents(fixed_data, dict(take_profit=0.10, stop_loss=0.05), dict(buy=[3]), o=base, h=hi, l=base, c=base)
    assert out.target.iloc[5] == 1.0 and out.target.iloc[6] == 0.0                # closed on the candle it was hit
    lo = list(base); lo[5] = 94                                                    # low touches the -5% stop
    out = intents(fixed_data, dict(take_profit=0.10, stop_loss=0.05), dict(buy=[3]), o=base, h=base, l=lo, c=base)
    assert out.target.iloc[4] == 1.0 and out.target.iloc[5] == 0.0


def test_no_reentry_on_the_candle_an_exit_happens_and_exit_signal_closes(fixed_data):
    out = intents(fixed_data, {}, dict(buy=[3], leave=[6]))
    assert out.target.iloc[5] == 1.0 and out.target.iloc[6] == 0.0


def test_max_hold_closes_after_n_candles(fixed_data):
    out = intents(fixed_data, dict(max_hold=3), dict(buy=[3]))
    assert out.target.tolist()[3:8] == [1.0, 1.0, 1.0, 0.0, 0.0]


def test_trailing_stop_ratchets_up_never_down(fixed_data):
    c = [100.0] * 4 + [104, 108, 112, 110, 105, 100] + [100.0] * 10
    out = intents(fixed_data, dict(stop_loss=0.05, take_profit=5.0, trail=True), dict(buy=[3]), o=c, h=c, l=c, c=c)
    stops = out["stop"].iloc[3:9].dropna()
    assert stops.is_monotonic_increasing and stops.iloc[0] == pytest.approx(95.0) and stops.max() == pytest.approx(112 * 0.95)


def test_sell_signal_opens_a_short_with_levels_on_the_other_side(fixed_data):
    out = intents(fixed_data, dict(take_profit=0.10, stop_loss=0.05), dict(sell=[3]))
    row = out.iloc[3]
    assert row["target"] == -1.0 and row["stop"] == pytest.approx(105.0) and row["take"] == pytest.approx(90.0)


def test_take_profit_and_stop_loss_are_required_everything_else_defaults(fixed_data):
    sig = Script(buy=[3])
    for bad in (dict(), dict(take_profit=0.1), dict(stop_loss=0.05)):
        with pytest.raises(TypeError):
            Trade(sig, **bad)
    with pytest.raises(ValueError, match="both"):
        Trade(sig, take_profit=None, stop_loss=0.05)
    with pytest.raises(ValueError, match="fractions"):
        Trade(sig, take_profit=110, stop_loss=95)             # prices, not fractions
    out = intents(fixed_data, {}, dict(buy=[3]))              # defaults: $1000, market entry, no trail, no max_hold
    assert out.target.iloc[10] == 1.0 and out["usd"].iloc[10] == 1000.0 and out["limit"].isna().all()


def test_trade_node_drives_the_simulator_in_dollars(fixed_data):
    o = [100.0] * 4 + [100, 100, 100, 100, 120] + [120.0] * 11
    h = list(o); l = list(o); c = list(o)
    data = fixed_data(o, h, l, c)
    res = Graph(T(Script(buy=[3]), notional=500, take_profit=0.10)).run(data, "FIX", "2024-01-01", "2024-02-28",
                                                                           cache=False)
    sim = simulate(res.base_bars, res.intents, SimConfig(capital=1000, commission_bps=0, slippage_bps=0))
    t = sim.trades.iloc[0]
    assert t.entry_price == 100 and t.qty == pytest.approx(5.0)                   # $500 / $100
    assert t.exit_reason == "take" and t.exit_price == 120 and t.pnl == pytest.approx(100.0)   # gap above target: at open
    assert sim.equity.iloc[-1] == pytest.approx(1100.0)


# ---------------- richer per-signal instructions ----------------
class Orders(Node):
    """Emits the exact dict you give it on the given candle numbers."""
    history = Range(start="-3d", interval="d")

    def __init__(self, plan, **kw):
        super().__init__(plan=tuple(sorted(plan.items(), key=lambda kv: kv[0])), **kw)

    def run(self, candles, upstream):
        i = (candles.index[-1] - pd.Timestamp("2024-01-02", tz="UTC")).days
        return dict(dict(self.plan).get(i, {}))


def with_orders(fixed_data, plan, o=None, h=None, l=None, c=None, **trade_kw):
    o = o or FLAT
    data = fixed_data(o, h or o, l or o, c or o)
    return Graph(T(Orders(plan), **trade_kw)).run(data, "FIX", "2024-01-01", "2024-02-28", cache=False)


def test_signal_can_set_absolute_tp_sl_size_and_trailing_per_order(fixed_data):
    res = with_orders(fixed_data, {3: dict(buy=True, take_profit_price=120.0, stop_loss_price=97.0, usd=250.0, trail=True)})
    row = res.intents.iloc[3]
    assert (row["stop"], row["take"], row["usd"]) == (97.0, 120.0, 250.0)
    res = with_orders(fixed_data, {3: dict(buy=True, qty=4, take_profit=0.2, stop_loss=0.01)})
    row = res.intents.iloc[3]
    assert row["usd"] == pytest.approx(400.0) and row["take"] == pytest.approx(120.0) and row["stop"] == pytest.approx(99.0)


def test_inconsistent_levels_are_rejected(fixed_data):
    with pytest.raises(ValueError, match="stop < price < take"):
        with_orders(fixed_data, {3: dict(buy=True, stop_loss_price=105.0)})
    with pytest.raises(ValueError, match="take < price < stop"):
        with_orders(fixed_data, {3: dict(sell=True, take_profit_price=105.0)})


def test_limit_entry_fills_only_when_the_next_candle_trades_through_it(fixed_data):
    base = [100.0] * 20
    lo = list(base); lo[4] = 98.0                                       # candle 4 dips to 98: a buy limit at 99 fills
    res = with_orders(fixed_data, {3: dict(buy=True, price=99.0)}, o=base, h=base, l=lo, c=base)
    t = res.intents
    assert t["limit"].iloc[3] == 99.0 and t["entry"].iloc[3] and t["target"].iloc[3] == 1.0
    assert t["stop"].iloc[3] == pytest.approx(99 * 0.95) and t["take"].iloc[3] == pytest.approx(99 * 1.10)
    assert t["target"].iloc[4] == 1.0 and t["target"].iloc[6] == 1.0    # filled -> now a position
    res = with_orders(fixed_data, {3: dict(buy=True, price=99.0)}, o=base, h=base, l=base, c=base)   # never reaches 99
    assert res.intents["target"].iloc[3] == 1.0 and (res.intents["target"].iloc[4:] == 0).all()      # order expired


def test_simulator_honours_limit_fills_and_misses(fixed_data):
    base = [100.0] * 20
    lo = list(base); lo[4] = 98.0
    cfg = SimConfig(capital=1000, commission_bps=0, slippage_bps=0)
    res = with_orders(fixed_data, {3: dict(buy=True, price=99.0, take_profit_price=101.0, stop_loss_price=90.0)},
                      o=base, h=base, l=lo, c=base)
    sim = simulate(res.base_bars, res.intents, cfg)
    t = sim.trades.iloc[0]
    assert t.entry_price == 99.0 and t.qty == pytest.approx(1000 / 99)             # filled AT the limit, not the open
    res = with_orders(fixed_data, {3: dict(buy=True, price=99.0)}, o=base, h=base, l=base, c=base)
    assert len(simulate(res.base_bars, res.intents, cfg).trades) == 0              # never touched: no trade


def test_buy_at_price_with_tp_and_sl_prices_end_to_end(fixed_data):
    """The simplest hand-written trade: buy at 100, sell at 110 (TP) or 95 (SL)."""
    o = [100.0] * 4 + [100, 100, 104, 108, 109] + [109.0] * 11
    h = list(o); l = list(o); h[8] = 112                                             # candle 8 touches 110
    res = with_orders(fixed_data, {3: dict(buy=True, price=100.0, take_profit_price=110.0, stop_loss_price=95.0)},
                      o=o, h=h, l=l, c=o)
    sim = simulate(res.base_bars, res.intents, SimConfig(capital=1000, commission_bps=0, slippage_bps=0))
    t = sim.trades.iloc[0]
    assert (t.entry_price, t.exit_price, t.exit_reason) == (100.0, 110.0, "take") and t.pnl == pytest.approx(100.0)
