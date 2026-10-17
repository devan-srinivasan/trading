"""PaperBroker: the one set of fill rules shared by backtests and the local live engine.
Candle i: open time = i days, closes (avail) at i+1 days. Orders sent at a close fill on the next candle."""
import pytest

from dagtrade.sim import PaperBroker

DAY = 86400.0


def candle(b, i, o, h=None, l=None, c=None):
    h = o if h is None else h
    l = o if l is None else l
    c = o if c is None else c
    b.process(i * DAY, (i + 1) * DAY, o, h, l, c)


def broker(cash=10_000.0, comm=0.0, slip=0.0, whole=False):           # comm / slip in basis points
    return PaperBroker(cash, comm, slip, whole_shares=whole)


def test_market_entry_fills_at_the_next_open_not_the_signal_close():
    b = broker()
    candle(b, 0, 10, c=10.5)
    b.submit_entry("buy", 100, "e1", at=1 * DAY)             # decided at candle 0's close
    candle(b, 1, 11, c=12)
    f = b.s["fills"][0]
    assert (f["time"], f["price"], f["qty"]) == (1 * DAY, 11, 100)
    assert b.cash == pytest.approx(10_000 - 1100) and b.equity(12) == pytest.approx(10_000 + 100)


def test_stop_fills_at_the_stop_and_a_gap_fills_at_the_open():
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, stop=95, take=120)
    candle(b, 1, 100, h=101, l=94)                          # entry candle already checks the legs
    assert [f["reason"] for f in b.s["fills"]] == ["market", "stop"] and b.s["fills"][1]["price"] == 95
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, stop=95, take=120)
    candle(b, 1, 100)
    candle(b, 2, 90)                                        # gapped through the stop: filled at the open
    assert b.s["fills"][1]["price"] == 90 and b.qty == 0


def test_stop_wins_when_a_candle_touches_both_and_the_other_leg_is_cancelled():
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, stop=95, take=105)
    candle(b, 1, 100, h=106, l=94)
    assert b.s["fills"][1]["reason"] == "stop" and b.working() == []


def test_take_profit_fills_at_the_target_or_better_on_a_gap():
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, stop=90, take=110)
    candle(b, 1, 100)
    candle(b, 2, 115)
    assert b.s["fills"][1]["reason"] == "take" and b.s["fills"][1]["price"] == 115


def test_short_pnl_and_costs():
    b = broker(cash=2000, comm=10)
    candle(b, 0, 10)
    b.submit_entry("sell", 100, "e", at=DAY)
    candle(b, 1, 10)
    b.submit_market("buy", 100, "c", at=2 * DAY)
    candle(b, 2, 8)
    comm = 100 * 10 * 0.001 + 100 * 8 * 0.001
    assert b.equity(8) == pytest.approx(2000 + 200 - comm) and b.qty == 0


def test_slippage_hurts_both_ways():
    b = broker(slip=100)
    candle(b, 0, 10)
    b.submit_entry("buy", 100, "e", at=DAY)
    candle(b, 1, 10)
    b.submit_market("sell", 100, "c", at=2 * DAY)
    candle(b, 2, 10)
    assert b.equity(10) == pytest.approx(10_000 - 2 * 1000 * 0.01)        # 1% on the way in and out


def test_limit_entry_fills_only_if_the_candle_trades_through_it_else_expires():
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, limit=99)
    candle(b, 1, 100, l=98)
    assert b.s["fills"][0]["price"] == 99 and b.s["fills"][0]["reason"] == "limit"
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, limit=99)
    candle(b, 1, 100)
    assert b.s["fills"] == [] and b.status("e") == "expired"


def test_no_borrowing_entries_are_cut_to_the_cash_or_rejected():
    b = broker(cash=1000)
    candle(b, 0, 10)
    b.submit_entry("buy", 400, "e", at=DAY)                 # $4000 order, $1000 account
    candle(b, 1, 10)
    assert b.s["fills"][0]["qty"] == pytest.approx(100)
    b = broker(cash=1000, comm=10)
    candle(b, 0, 10)
    b.submit_entry("buy", 100, "e", at=DAY)
    candle(b, 1, 10)
    f = b.s["fills"][0]
    assert f["qty"] * f["price"] + f["fee"] == pytest.approx(1000) and f["qty"] < 100      # the fee must fit too
    b = broker(cash=150, whole=True)
    candle(b, 0, 100)
    b.submit_entry("buy", 3, "e", at=DAY)
    candle(b, 1, 100)
    assert b.s["fills"][0]["qty"] == 1                                                     # whole shares only
    b = broker(cash=50, whole=True)
    candle(b, 0, 100)
    b.submit_entry("buy", 1, "e", at=DAY)
    candle(b, 1, 100)
    assert b.s["fills"] == [] and b.status("e") == "rejected"


def test_a_close_never_flips_the_position_and_duplicate_client_ids_are_refused():
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY)
    candle(b, 1, 100)
    b.submit_market("sell", 25, "c", at=2 * DAY)
    candle(b, 2, 100)
    assert b.qty == 0
    with pytest.raises(ValueError, match="unique"):
        b.submit_market("sell", 1, "c", at=3 * DAY)


def test_trailing_stop_moves_and_flatten_closes_at_the_last_price():
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY, stop=95)
    candle(b, 1, 100)
    b.replace_stop(None, 99)
    candle(b, 2, 100, l=98.5)
    assert b.s["fills"][1]["price"] == 99
    b = broker()
    candle(b, 0, 100)
    b.submit_entry("buy", 10, "e", at=DAY)
    candle(b, 1, 100, c=104)
    b.flatten(at=2 * DAY, reason="end")
    assert b.qty == 0 and b.s["fills"][-1]["price"] == 104 and b.s["fills"][-1]["reason"] == "end"
