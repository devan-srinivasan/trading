import json
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from dagtrade.config import Deployment
from dagtrade.data import BarStore, DataSource, SyntheticProvider
from dagtrade.fsa import Intent
from dagtrade.live import AlpacaBroker, LiveRunner, plan
from dagtrade.live.models import BAccount, BClock, BOrder, slug

LABEL = "strat/ABC"
PRE = f"dt.{slug(LABEL)}"
NOW = 1_800_000_000.0
TOKEN = str(int(NOW - 60))


def entry(side="buy", price=100.0, stop=95.0, take=110.0, usd=1000.0, pct=None, limit=None, token=TOKEN):
    return Intent("entry", token, side=side, usd=usd, pct=pct, price=price, stop=stop, take=take, limit=limit)


def leg(token="1", status="new", type_="stop"):
    return BOrder(id=f"leg{token}", client_id=f"x{token}", symbol="ABC", side="sell", qty=10, status=status, type=type_)


def run(intents, pos=0, orders=(), paused=False, **kw):
    return plan(list(intents), pos, list(orders), LABEL, NOW, paused, **kw)


# ---------------- planner: intents -> broker actions (pure) ----------------
def test_entry_when_flat_sends_one_order_that_carries_its_own_exits():
    (a,), _, keep = run([entry()])
    assert (a.kind, a.side, a.qty, a.stop, a.take) == ("entry", "buy", 10, 95.0, 110.0)      # $1000 / $100
    assert a.client_id == f"{PRE}.ent.{TOKEN}" and keep == []


def test_limit_entry_sizes_from_the_limit_price_and_shorts_sell():
    (a,), _, _ = run([entry(stop=47.5, take=55.0, limit=50.0)])
    assert (a.qty, a.limit) == (20, 50.0)                                   # $1000 / $50 limit, not / $100 last close
    (a,), _, _ = run([entry(side="sell", stop=105.0, take=90.0)])
    assert (a.side, a.stop, a.take) == ("sell", 105.0, 90.0)


@pytest.mark.parametrize("kw,note", [
    (dict(pos=10), "already in a position"),
    (dict(orders=[leg()]), "already working"),
    (dict(paused=True), "paused"),
])
def test_no_entry_when_not_appropriate(kw, note):
    acts, notes, _ = run([entry()], **kw)
    assert acts == [] and any(note in n for n in notes)


def test_no_entry_with_less_than_one_share_or_a_stale_decision():
    acts, notes, _ = run([entry(price=5000.0)])
    assert acts == [] and any("less than one share" in n for n in notes)
    acts, notes, _ = run([entry(token=str(int(NOW - 10 * 86400)))])
    assert acts == [] and any("too old" in n for n in notes)


def test_close_cancels_the_exit_legs_first_then_closes_and_waits_until_flat():
    c = Intent("close", TOKEN, reason="exit signal")
    acts, _, keep = run([c], pos=10, orders=[leg("1"), leg("2")])
    assert [(a.kind, a.order_id) for a in acts] == [("cancel", "leg1"), ("cancel", "leg2")] and keep == [c]
    acts, _, keep = run([c], pos=10)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("close", "sell", 10)] and acts[0].client_id == f"{PRE}.cls.{TOKEN}"
    closing = BOrder(id="c", client_id=f"{PRE}.cls.{TOKEN}", symbol="ABC", side="sell", qty=10, status="new")
    assert run([c], pos=10, orders=[closing])[0] == []                      # our close is on its way: nothing new
    assert run([c], pos=0) == ([], [], [])                                  # flat: done
    acts, _, _ = run([c], pos=-4)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("close", "buy", 4)]


def test_cancel_entry_and_trailing_stop():
    acts, _, _ = run([Intent("cancel_entry", TOKEN)], orders=[leg("e", type_="limit")])
    assert [(a.kind, a.order_id) for a in acts] == [("cancel", "lege")]
    acts, _, _ = run([Intent("stop", TOKEN, stop=101.5)], pos=10, orders=[leg("s"), leg("t", type_="limit")])
    assert [(a.kind, a.order_id, a.stop) for a in acts] == [("replace_stop", "legs", 101.5)]


def test_percent_orders_use_the_account_equity_and_never_exceed_its_cash():
    (a,), _, _ = run([entry(pct=0.5)], equity=20_000.0, cash=20_000.0)
    assert a.qty == 100                                                       # 50% of $20k at $100
    (a,), _, _ = run([entry(pct=0.5)], equity=20_000.0, cash=3_000.0)
    assert a.qty == 29                                                        # cash pays for 29 (1% buffer kept)
    acts, notes, _ = run([entry()], equity=50.0, cash=50.0)
    assert acts == [] and "not enough cash" in notes[0]
    acts, notes, _ = run([entry(pct=0.5)])                                    # % order but no account read
    assert acts == [] and "equity" in notes[0]


# ---------------- fake broker + scripted strategy on a controlled clock ----------------
class FakeBroker:
    name = "fake"

    def __init__(self):
        self.pos, self.orders, self.n, self.attempts, self.fail_seq = {}, [], 0, 0, []

    def account(self): return BAccount(100_000.0, 100_000.0, 200_000.0)
    def clock(self):
        t = pd.Timestamp("2024-06-10 15:00", tz="UTC")
        return BClock(t, True, t, t + pd.Timedelta(hours=1))
    def positions(self): return dict(self.pos)
    def open_orders(self, symbol=None): return [o for o in self.orders if o.active]
    def recent_orders(self, symbol, since): return list(self.orders)

    def _add(self, **kw):
        self.attempts += 1
        if self.fail_seq:
            raise self.fail_seq.pop(0)
        self.n += 1
        o = BOrder(id=f"o{self.n}", status="accepted", **kw)
        self.orders.append(o)
        return o

    def submit_entry(self, symbol, side, qty, client_id, stop=None, take=None, limit=None):
        return self._add(client_id=client_id, symbol=symbol, side=side, qty=qty, stop_price=stop, limit_price=take)

    def submit_market(self, symbol, side, qty, client_id):
        return self._add(client_id=client_id, symbol=symbol, side=side, qty=qty)

    def cancel(self, order_id):
        next(o for o in self.orders if o.id == order_id).status = "canceled"

    def replace_stop(self, order_id, stop):
        next(o for o in self.orders if o.id == order_id).stop_price = stop

    def fill(self, o):
        o.status = "filled"
        self.pos[o.symbol] = self.pos.get(o.symbol, 0) + (o.qty if o.side == "buy" else -o.qty)


STRATEGY = '''
from dagtrade import FSA, Node, Range, Trade
DEFAULTS = dict(buy_on="2024-06-07", exit_on="2099-01-01", take_profit=0.10, stop_loss=0.05)

class Dates(Node):
    """Buys on the listed dates, waits otherwise."""
    history = Range(candles=3, interval="d")
    def __init__(self, buy_on, **kw):
        super().__init__(buy_on=buy_on, **kw)
    def run(self, candles, ctx):
        return {"buy": True} if str(candles.index[-1].date()) in self.buy_on.split(",") else None

class ExitOn(Node):
    history = Range(candles=3, interval="d")
    def __init__(self, exit_on, **kw):
        super().__init__(exit_on=exit_on, **kw)
    def run(self, candles, ctx):
        return str(candles.index[-1].date()) in self.exit_on.split(",")

def build(buy_on, exit_on, take_profit, stop_loss):
    return FSA.chain(Dates(buy_on), Trade(take_profit=take_profit, stop_loss=stop_loss, exit=ExitOn(exit_on)))
'''


def ts(s):
    return pd.Timestamp(s, tz="UTC").timestamp()


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "strategies" / "livetest"
    d.mkdir(parents=True)
    (d / "strategy.py").write_text(STRATEGY)

    def make(params=None, broker=None):
        dep = Deployment(strategy="livetest", ticker="TEST", start="2024-01-01", live={"enabled": True},
                         params=params or {}, path=d / "TEST.toml")
        clock = [ts("2024-06-08 12:00")]            # Saturday: the last closed daily candle is Friday 7 June
        sleeps = []
        b = broker or FakeBroker()
        factory = lambda dp, s: DataSource([SyntheticProvider()], BarStore(tmp_path / "live_bars"))
        r = LiveRunner([dep], b, data_factory=factory, root=tmp_path / "live", charts=False,
                       clock_fn=lambda: clock[0], sleep_fn=sleeps.append, retry_delays=(0.25, 1.0, 2.5))
        return NS(dep=dep, b=b, r=r, clock=clock, sleeps=sleeps, root=tmp_path / "live", factory=factory)
    return make


def test_runner_steps_the_fsa_and_sends_one_bracket_entry_never_twice(env):
    e = env()
    e.r.cycle()
    (o,) = e.b.orders
    row = e.r.status["deployments"][0]
    assert (o.side, o.symbol) == ("buy", "TEST") and row["current_step"] == "trade"
    price = e.r.items[0].last_bars["close"].iloc[-1]
    assert o.qty == int(1000 // price) and o.stop_price == pytest.approx(price * 0.95)
    assert o.limit_price == pytest.approx(price * 1.10) and o.client_id.startswith(f"dt.{slug('livetest/TEST')}.ent.")
    for _ in range(3):
        e.r.cycle()
    assert len(e.b.orders) == 1
    text = (e.root / "journal.jsonl").read_text()
    assert '"type": "transition"' in text and '"type": "action"' in text and '"type": "intent"' in text
    assert json.loads((e.root / "status.json").read_text())["mode"] == "fake"


def test_runner_does_nothing_on_a_day_without_a_signal(env):
    e = env()
    e.clock[0] = ts("2024-06-07 12:00")                  # last closed candle is Thursday: no signal
    e.r.cycle()
    assert e.b.orders == [] and e.r.status["deployments"][0]["current_step"] == "dates"


def test_exit_check_cancels_legs_then_closes_and_the_fsa_loops_back(env):
    e = env(dict(exit_on="2024-06-12"))
    e.r.cycle()
    ent = e.b.orders[0]
    e.b.fill(ent)
    e.b.orders.append(BOrder(id="leg", client_id="legs", symbol="TEST", side="sell", qty=ent.qty, status="new",
                             type="stop"))                                   # the bracket's exit leg at Alpaca
    e.clock[0] = ts("2024-06-13 12:00")                  # candles of 10, 11, 12 June (exit) have closed
    e.r.cycle()
    assert [o.status for o in e.b.orders if o.id == "leg"] == ["canceled"]
    e.r.cycle()
    close = e.b.orders[-1]
    assert (close.side, close.qty) == ("sell", ent.qty) and ".cls." in close.client_id
    e.b.fill(close)
    e.clock[0] = ts("2024-06-14 12:00")
    e.r.cycle()
    assert e.r.status["deployments"][0]["current_step"] == "dates"            # trade done -> back to the start


def test_fsa_state_survives_a_restart_of_the_scanner(env):
    e = env()
    e.r.cycle()
    r2 = LiveRunner([e.dep], e.b, data_factory=e.factory, root=e.root, charts=False, clock_fn=lambda: e.clock[0])
    assert r2.items[0].runner.current == "trade"                             # resumed from live/state, not the start
    r2.cycle()
    assert len(e.b.orders) == 1


def test_paused_runner_sends_nothing_and_one_strategy_per_symbol(env):
    e = env()
    e.r.set_paused(True)
    e.r.cycle()
    assert e.b.orders == []
    dep2 = Deployment(strategy="livetest", ticker="TEST", start="2024-01-01", live={"enabled": True}, path=e.dep.path)
    with pytest.raises(ValueError, match="one strategy per"):
        LiveRunner([e.dep, dep2], e.b, data_factory=e.factory, root=e.root / "x", charts=False)


# ---------------- failures: retry now -> re-read the account and re-plan -> pause briefly ----------------
class HttpError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg or f"http {code}")
        self.status_code = code


def state(e):
    return e.r.states.load("livetest/TEST")


def test_classify_error():
    from dagtrade.live.runner import classify_error
    assert classify_error(HttpError(503)) == classify_error(HttpError(429)) == "transient"
    assert classify_error(HttpError(403)) == classify_error(HttpError(422)) == "permanent"
    assert classify_error(ConnectionError("x")) == classify_error(RuntimeError("Read timed out")) == "transient"
    assert classify_error(ValueError("bad qty")) == "permanent"


def test_transient_error_is_retried_immediately_in_the_same_cycle(env):
    e = env()
    e.b.fail_seq = [HttpError(503), ConnectionError("reset")]
    e.r.cycle()
    assert len(e.b.orders) == 1 and e.b.attempts == 3 and e.sleeps == [0.25, 1.0]


def test_persistent_transient_error_replans_on_a_fresh_account_read_then_succeeds(env):
    e = env()
    e.b.fail_seq = [HttpError(503)] * 4                          # all attempts of the first pass fail
    e.r.cycle()
    assert len(e.b.orders) == 1 and e.b.attempts == 5
    assert "re-reading the account and re-planning" in (e.root / "journal.jsonl").read_text()


def test_still_failing_keeps_the_entry_and_retries_from_scratch_shortly(env):
    e = env()
    e.b.fail_seq = [HttpError(503)] * 8
    e.r.cycle()
    st = state(e)
    assert e.b.orders == [] and e.b.attempts == 8                           # exactly one re-plan, no loops
    assert st.retry_not_before == pytest.approx(e.clock[0] + 10) and st.intents
    e.clock[0] += 11                                                        # 10 s pause passes, broker recovers
    e.r.cycle()
    assert len(e.b.orders) == 1 and state(e).intents == []


def test_an_unsent_entry_is_dropped_when_a_newer_candle_makes_it_obsolete(env):
    e = env()
    e.b.fail_seq = [HttpError(503)] * 8
    e.r.cycle()
    assert state(e).intents
    e.clock[0] = ts("2024-06-12 12:00")                      # newer candles closed; none of them has a signal
    e.r.cycle()
    assert e.b.orders == [] and state(e).intents == []
    assert "obsolete" in (e.root / "journal.jsonl").read_text()
    assert e.r.status["deployments"][0]["current_step"] == "dates"          # the Trade saw no order and failed back


def test_permanent_error_is_not_retried_and_the_entry_is_dropped(env):
    e = env()
    e.b.fail_seq = [HttpError(403, "insufficient buying power")]
    e.r.cycle()
    st = state(e)
    assert e.b.attempts == 1 and e.sleeps == [] and st.intents == []
    assert st.retry_not_before == pytest.approx(e.clock[0] + 60)


def test_duplicate_client_order_id_counts_as_already_sent(env):
    e = env()
    e.b.fail_seq = [HttpError(422, "client_order_id must be unique")]
    e.r.cycle()
    st = state(e)
    assert st.intents == [] and st.retry_not_before == 0.0


# ---------------- Alpaca adapter: request translation (validated by the SDK's own models) ----------------
class StubClient:
    def __init__(self):
        self.sent = []

    def submit_order(self, req):
        self.sent.append(req)
        return NS(id="abc", client_order_id=req.client_order_id, symbol=req.symbol, side=req.side, qty=str(req.qty),
                  status="accepted", order_type=req.type, type=req.type, order_class=req.order_class, stop_price=None,
                  limit_price=None, filled_qty="0", filled_avg_price=None, submitted_at=None, filled_at=None, legs=None)

    def cancel_order_by_id(self, oid): self.sent.append(("cancel", oid))
    def replace_order_by_id(self, oid, req): self.sent.append(("replace", oid, req))

    def get_all_positions(self):
        return [NS(symbol="AAA", qty="10", side=NS(value="long")), NS(symbol="BBB", qty="-5", side=NS(value="short")),
                NS(symbol="CCC", qty="5", side="short")]

    def get_account(self):
        return NS(equity="100000", cash="50000", buying_power="200000", status=NS(value="ACTIVE"), trading_blocked=False)

    def get_clock(self):
        t = pd.Timestamp("2026-10-15 14:00", tz="UTC").to_pydatetime()
        return NS(timestamp=t, is_open=True, next_open=t, next_close=t)


def test_alpaca_entry_orders_are_valid_sdk_requests():
    stub = StubClient()
    b = AlpacaBroker(client=stub)
    b.submit_entry("AAPL", "buy", 10, "dt.x.ent.1", stop=95.123, take=110.0)
    b.submit_entry("AAPL", "buy", 10, "dt.x.ent.2", stop=95.0)
    b.submit_entry("AAPL", "sell", 10, "dt.x.ent.3", take=90.0)
    b.submit_entry("AAPL", "buy", 10, "dt.x.ent.4")
    b.submit_market("AAPL", "sell", 10, "dt.x.cls.5")
    b.submit_entry("AAPL", "buy", 10, "dt.x.ent.6", stop=95.0, take=110.0, limit=99.5)
    both, stop_only, take_only, plain, close, limited = stub.sent
    assert (limited.type.value, limited.limit_price, limited.order_class.value) == ("limit", 99.5, "bracket")
    assert both.order_class.value == "bracket" and both.stop_loss.stop_price == 95.12 \
        and both.take_profit.limit_price == 110.0 and both.time_in_force.value == "gtc"
    assert stop_only.order_class.value == "oto" and stop_only.stop_loss.stop_price == 95.0 and stop_only.take_profit is None
    assert take_only.order_class.value == "oto" and take_only.take_profit.limit_price == 90.0 and take_only.side.value == "sell"
    assert plain.order_class is None and plain.time_in_force.value == "day" and plain.qty == 10
    assert (close.type.value, close.side.value, close.time_in_force.value) == ("market", "sell", "day")
    b.cancel("abc")
    assert stub.sent[-1] == ("cancel", "abc")
    b.replace_stop("abc", 101.234)
    assert stub.sent[-1][0] == "replace" and stub.sent[-1][2].stop_price == 101.23
    assert b.positions() == {"AAA": 10.0, "BBB": -5.0, "CCC": -5.0}      # shorts come back negative
    assert b.account().equity == 100_000 and b.clock().is_open


def test_alpaca_broker_is_paper_only(monkeypatch):
    import alpaca.trading.client as tc
    seen = {}
    monkeypatch.setattr(tc, "TradingClient", lambda k, s, paper: seen.setdefault("paper", paper) or NS())
    AlpacaBroker(key="k", secret="s")
    assert seen["paper"] is True


