"""Local paper engine, the live registry, and the registry-driven runner (local + Alpaca modes)."""
import json

import pandas as pd
import pytest

from dagtrade.data import BarStore, DataSource, SyntheticProvider
from dagtrade.live import LiveRunner, LocalBroker, Registry
from dagtrade.live.models import BClock
from dagtrade.live.pnl import position_pnl
from test_live import STRATEGY, FakeBroker, ts

CLOCK = lambda: BClock(pd.Timestamp("2024-06-10 15:00", tz="UTC"), True,      # noqa: E731
                       pd.Timestamp("2024-06-11 13:30", tz="UTC"), pd.Timestamp("2024-06-10 20:00", tz="UTC"))


def candles(rows, start="2024-01-02"):
    """rows: list of (open, high, low, close); daily candles available at the next midnight."""
    idx = pd.date_range(start, periods=len(rows), freq="D", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["avail"] = idx + pd.Timedelta(days=1)
    return df


def broker(tmp_path, now, **kw):
    return LocalBroker("s/X", "X", tmp_path, clock=CLOCK, now_fn=lambda: now[0], commission_bps=0, slippage_bps=0, **kw)


# ---------------- local engine ----------------
def test_market_entry_fills_at_next_candle_open_then_take_profit(tmp_path):
    now = [ts("2024-01-02 12:00")]
    b = broker(tmp_path, now)
    bars = candles([(100, 101, 99, 100), (102, 104, 101, 103), (103, 111, 102, 110), (110, 110, 108, 109)])
    b.on_candles(bars.iloc[:1])                                   # account starts at the latest candle
    now[0] = ts("2024-01-03 00:00:05")                             # order sent just after candle 0 closed
    b.submit_entry("X", "buy", 10, "dt.s-X.ent.1", stop=95.0, take=110.0)
    assert b.positions() == {} and len(b.open_orders()) == 1       # working, not filled yet
    b.on_candles(bars)                                             # candle 1 (opens 102) and candle 2 (high 111)
    o = b.s["orders"]
    assert o[0]["status"] == "filled" and o[0]["fill_price"] == 102 and b.s["fills"][0]["role"] == "entry"
    take = next(x for x in o if x["role"] == "take")
    assert take["status"] == "filled" and take["fill_price"] == 110          # take-profit hit on candle 2
    assert next(x for x in o if x["role"] == "stop")["status"] == "canceled"  # one-cancels-other
    assert b.positions() == {} and b.s["cash"] == pytest.approx(10_000 + 10 * (110 - 102))
    s = b.summary()
    assert s["realized"] == pytest.approx(80.0) and s["closed_trades"] == 1 and s["wins"] == 1


def test_stop_wins_when_a_candle_touches_both_and_gaps_fill_at_the_open(tmp_path):
    now = [ts("2024-01-02 12:00")]
    b = broker(tmp_path, now)
    bars = candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 112, 90, 100)])
    b.on_candles(bars.iloc[:1])
    now[0] = ts("2024-01-03 00:00:05")
    b.submit_entry("X", "buy", 1, "dt.s-X.ent.1", stop=95.0, take=110.0)
    b.on_candles(bars)
    assert [f["reason"] for f in b.s["fills"]] == ["market", "stop"] and b.s["fills"][1]["price"] == 95.0
    b2 = LocalBroker("s/Y", "Y", tmp_path, clock=CLOCK, now_fn=lambda: now[0], commission_bps=0, slippage_bps=0)
    gap = candles([(100, 100, 100, 100), (100, 100, 100, 100), (80, 85, 79, 84)])
    b2.on_candles(gap.iloc[:1])
    b2.submit_entry("Y", "buy", 1, "dt.s-Y.ent.1", stop=95.0, take=110.0)
    b2.on_candles(gap)
    assert b2.s["fills"][-1]["price"] == 80.0                       # gapped through the stop: filled at the open


def test_limit_entry_fills_or_expires_after_one_candle(tmp_path):
    now = [ts("2024-01-02 12:00")]
    b = broker(tmp_path, now)
    bars = candles([(100, 100, 100, 100), (100, 101, 99.5, 100), (100, 100, 97, 98)])
    b.on_candles(bars.iloc[:1])
    now[0] = ts("2024-01-03 00:00:05")
    b.submit_entry("X", "buy", 1, "dt.s-X.ent.1", limit=98.0, stop=90.0, take=120.0)
    b.on_candles(bars)
    assert b.s["orders"][0]["status"] == "expired" and b.positions() == {}     # candle 1 never reached 98


def test_duplicates_rejected_state_persists_and_flatten(tmp_path):
    now = [ts("2024-01-02 12:00")]
    b = broker(tmp_path, now)
    bars = candles([(100, 100, 100, 100), (100, 100, 100, 101)])
    b.on_candles(bars.iloc[:1])
    now[0] = ts("2024-01-03 00:00:05")
    b.submit_entry("X", "buy", 5, "dt.s-X.ent.1", stop=90.0, take=120.0)
    with pytest.raises(ValueError, match="unique"):
        b.submit_entry("X", "buy", 5, "dt.s-X.ent.1")
    b.on_candles(bars)
    again = broker(tmp_path, now)                                   # a restart reloads the account
    assert again.positions() == {"X": 5.0} and len(again.open_orders()) == 2
    again.flatten_now("stopped")
    assert again.positions() == {} and again.open_orders() == [] and again.s["fills"][-1]["price"] == 101.0


def test_pnl_from_fills():
    p = position_pnl([{"side": "buy", "qty": 10, "price": 100}, {"side": "buy", "qty": 10, "price": 110},
                      {"side": "sell", "qty": 20, "price": 120}], 120)
    assert p["realized"] == pytest.approx(300.0) and p["position"] == 0 and p["closed_trades"] == 1
    p = position_pnl([{"side": "sell", "qty": 5, "price": 50}], 45)
    assert p["position"] == -5 and p["unrealized"] == pytest.approx(25.0)


# ---------------- registry ----------------
@pytest.fixture
def project(tmp_path):
    d = tmp_path / "strategies" / "livetest"
    d.mkdir(parents=True)
    (d / "strategy.py").write_text(STRATEGY)
    (d / "TEST.toml").write_text('start = "2024-01-01"\n[params]\nstop_loss = 0.5\ntake_profit = 5.0\n')  # exits far away
    return tmp_path


def test_registry_add_update_remove_and_rules(project, tmp_path):
    r = Registry(tmp_path / "live", project=project)
    e = r.add("livetest", "test")
    assert e["id"] == "livetest/TEST" and e["mode"] == "local" and e["cash"] == 10_000 and e["status"] == "running"
    with pytest.raises(ValueError, match="already live"):
        r.add("livetest", "TEST")
    r.add("livetest", "TEST", interval="1d", mode="alpaca")
    with pytest.raises(ValueError, match="one strategy per symbol"):
        r.update("livetest/TEST", mode="alpaca")                    # a second Alpaca DAG on the same ticker
    r.update("livetest/TEST", status="stopped")
    assert r.get("livetest/TEST")["status"] == "stopped"
    r.remove("livetest/TEST")
    assert [x["id"] for x in r.load()] == ["livetest/TEST@1d"]
    with pytest.raises(ValueError, match="unknown strategy"):
        r.add("nope", "SPY")
    with pytest.raises(ValueError, match="intraday"):
        r.add("livetest", "TEST", interval="1d", flat_by_close=True)  # day trading needs intraday candles
    assert r.add("livetest", "TEST", interval="5m", flat_by_close=True)["flat_by_close"] is True
    with pytest.raises(ValueError, match="no state called"):
        r.add("livetest", "TEST", interval="1h", start_at="nope")
    e = r.add("livetest", "TEST", interval="1h", start_at="trade", inputs={"risk": 0.01})
    assert e["start_at"] == "trade" and e["inputs"] == {"risk": 0.01}
    req = r.request_restart(e["id"], "dates")["restart"]
    assert req["at"] == "dates" and req["nonce"]
    with pytest.raises(ValueError, match="no state called"):
        r.request_restart(e["id"], "nope")


# ---------------- runner driven by the registry ----------------
def make(project, tmp_path, clock, alpaca=None):
    reg = Registry(tmp_path / "live", project=project)
    factory = lambda dp, s: DataSource([SyntheticProvider()], BarStore(tmp_path / "bars"))     # noqa: E731
    r = LiveRunner(registry=reg, data_factory=factory, root=tmp_path / "live", charts=False,
                   clock_fn=lambda: clock[0], market_clock=CLOCK, alpaca_factory=(lambda: alpaca) if alpaca else None)
    return reg, r


def test_local_dag_trades_on_live_candles_and_records_the_pipeline(project, tmp_path):
    clock = [ts("2024-06-08 12:00")]                                # the scripted strategy buys on Fri 7 June
    reg, r = make(project, tmp_path, clock)
    reg.add("livetest", "TEST")
    r.cycle()
    li = r.items[0]
    assert li.mode == "local" and isinstance(li.broker, LocalBroker)
    assert [o["role"] for o in li.broker.s["orders"]] == ["entry"]      # entry sent to the LOCAL engine
    det = json.loads((tmp_path / "live" / "dags" / "livetest-TEST.json").read_text())
    stages = det["stages"]
    assert [(s["name"], s["state"]) for s in stages] == [("dates", "done"), ("trade", "active")]
    assert det["fsa"]["current"] == "trade" and det["fsa"]["memory"] == {"dates": {"buy": True}}
    assert det["dag"]["start"] == "dates" and det["account"]["equity"] == pytest.approx(10_000)

    clock[0] = ts("2024-06-11 12:00")                               # Monday's candle has closed
    r.cycle()
    assert li.broker.positions() == {"TEST": pytest.approx(li.broker.s["orders"][0]["qty"])}
    assert li.broker.s["orders"][0]["fill_price"] is not None
    det = json.loads((tmp_path / "live" / "dags" / "livetest-TEST.json").read_text())
    assert det["position"] > 0 and len(det["orders"]) == 3            # entry + its stop + take legs
    chart = json.loads((tmp_path / "live" / "charts" / "livetest-TEST.json").read_text())
    assert chart["t"] and "fsa.state" in chart["nodes"]                 # live candles + what the FSA was doing
    from dagtrade.viz.payload import live_payload
    lp = live_payload(chart, det["fills"])
    assert len(lp["trades"]) == 1 and lp["trades"][0]["open"] is True  # the REAL position, shown as an open trade
    assert any(j["type"] == "fill" for j in r.journal.tail(50))

    reg.update("livetest/TEST", status="stopped")                    # stop: the local position is closed
    r.cycle()
    assert r.items == [] and li.broker.positions() == {}
    assert json.loads((tmp_path / "live" / "dags" / "livetest-TEST.json").read_text())["running"] is False


def test_move_to_alpaca_closes_local_position_and_does_not_chase(project, tmp_path):
    clock = [ts("2024-06-08 12:00")]
    fake = FakeBroker()
    reg, r = make(project, tmp_path, clock, alpaca=fake)
    reg.add("livetest", "TEST")
    r.cycle()
    clock[0] = ts("2024-06-11 12:00")
    r.cycle()
    local = r.items[0].broker
    assert local.positions()
    reg.update("livetest/TEST", mode="alpaca")
    r.cycle()
    assert local.positions() == {}                                   # local engine stopped and flattened
    assert r.items[0].mode == "alpaca" and r.items[0].broker is fake
    assert fake.orders == []                                         # adopts the signal, no surprise entry


def test_live_trades_pair_real_fills_into_round_trips():
    from dagtrade.viz.payload import live_trades
    bars = candles([(100, 100, 100, 100)] * 6)[["open", "high", "low", "close"]].assign(volume=1.0)
    t0 = bars.index[1].timestamp()
    fills = [{"time": t0, "side": "buy", "qty": 10, "price": 100.0, "fee": 0.1, "reason": "market"},
             {"time": t0 + 86400 * 2, "side": "sell", "qty": 10, "price": 110.0, "fee": 0.1, "reason": "take"},
             {"time": t0 + 86400 * 3, "side": "sell", "qty": 5, "price": 100.0, "fee": 0.0, "reason": "market"}]
    df = live_trades(fills, bars)
    assert list(df["side"]) == ["long", "short"] and list(df["open"]) == [False, True]
    assert df["pnl"].iloc[0] == pytest.approx(99.8) and df["exit_reason"].iloc[0] == "take" and df["bars_held"].iloc[0] == 2


def test_local_engine_never_spends_more_cash_than_it_has(tmp_path):
    now = [ts("2024-01-02 12:00")]
    b = broker(tmp_path, now, cash=1_000.0)
    bars = candles([(100, 101, 99, 100), (125, 126, 124, 125), (125, 126, 124, 125)])
    b.on_candles(bars.iloc[:1])
    now[0] = ts("2024-01-03 00:00:05")
    b.submit_entry("X", "buy", 9, "dt.s-X.ent.1", stop=90.0, take=150.0)     # 9 x $100 fit, but it opens at $125
    b.on_candles(bars.iloc[:2])
    e = b.s["orders"][0]
    assert e["status"] == "filled" and e["qty"] == 8 and b.s["cash"] >= 0       # cut to 8 shares
    b2 = LocalBroker("s/Z", "Z", tmp_path, clock=CLOCK, now_fn=lambda: now[0], commission_bps=0, slippage_bps=0, cash=50.0)
    now[0] = ts("2024-01-02 12:00")
    b2.on_candles(bars.iloc[:1])
    now[0] = ts("2024-01-03 00:00:05")
    b2.submit_entry("Z", "buy", 1, "dt.s-Z.ent.1", stop=90.0, take=150.0)
    b2.on_candles(bars.iloc[:2])
    assert b2.s["orders"][0]["status"] == "rejected" and b2.positions() == {} and b2.s["cash"] == 50.0


def test_restart_request_moves_a_running_fsa_once(project, tmp_path):
    clock = [ts("2024-06-07 12:00")]                                # Thursday's candle: no signal, waits at dates
    reg, r = make(project, tmp_path, clock)
    reg.add("livetest", "TEST")
    r.cycle()
    assert r.items[0].runner.current == "dates"
    reg.request_restart("livetest/TEST", "trade", {"signal": {"buy": True}})
    r.cycle()
    li = r.items[0]
    assert [o["role"] for o in li.broker.s["orders"]] == ["entry"]   # started at the Trade: it ordered right away
    r.cycle()
    assert len(li.broker.s["orders"]) == 1                            # the request is applied once, not every cycle
    assert any("restarted at state trade" in j["text"] for j in r.journal.tail(20))
