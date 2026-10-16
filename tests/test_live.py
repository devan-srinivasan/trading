import json
from pathlib import Path
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from dagtrade.config import Deployment
from dagtrade.data import BarStore, DataSource, SyntheticProvider
from dagtrade.live import AlpacaBroker, LiveRunner, build_live, compute_decision, plan
from dagtrade.live.models import BAccount, BClock, BOrder, slug
from dagtrade.live.reconcile import Decision

LABEL = "strat/ABC"
PRE = f"dt.{slug(LABEL)}"
NOW = 1_800_000_000.0


def dec(entry=True, target=1.0, price=100.0, stop=95.0, take=110.0, avail=NOW - 60, usd=1000.0):
    return Decision(avail=avail, price=price, target=target, usd=usd, stop=stop, take=take, entry=entry)


def leg(token="1", status="new"):
    return BOrder(id=f"leg{token}", client_id=f"x{token}", symbol="ABC", side="sell", qty=10, status=status, type="stop")


def run(d, pos=0, orders=(), act=True, paused=False):
    return plan(d, pos, list(orders), LABEL, NOW, act, paused)


# ---------------- planner ----------------
def test_fresh_signal_when_flat_sends_one_entry_that_carries_its_own_exits():
    acts, _ = run(dec())
    (a,) = acts
    assert (a.kind, a.side, a.qty, a.stop, a.take) == ("entry", "buy", 10, 95.0, 110.0)      # $1000 / $100
    assert a.client_id == f"{PRE}.ent.{int(NOW - 60)}"


def test_limit_entry_sizes_shares_from_the_limit_price_and_passes_it_on():
    acts, _ = run(dec(price=100.0, take=110.0, stop=95.0, usd=1000.0).__class__(
        avail=NOW - 60, price=100.0, target=1.0, usd=1000.0, stop=47.5, take=55.0, limit=50.0, entry=True))
    assert (acts[0].qty, acts[0].limit) == (20, 50.0)                      # $1000 / $50 limit, not / $100 last close


def test_short_signal_sells():
    acts, _ = run(dec(target=-1.0, stop=105.0, take=90.0))
    assert (acts[0].side, acts[0].stop, acts[0].take) == ("sell", 105.0, 90.0)


@pytest.mark.parametrize("kw,note", [
    (dict(pos=10), None),                                          # already in a position
    (dict(orders=[leg()]), "already working"),                     # an order is in flight
    (dict(act=False), None),                                       # not the candle we are reacting to
    (dict(paused=True), "paused"),
])
def test_no_entry_when_not_appropriate(kw, note):
    acts, notes = run(dec(), **kw)
    assert acts == [] and (note is None or any(note in n for n in notes))


def test_no_entry_without_the_entry_flag_or_with_less_than_one_share_or_stale():
    assert run(dec(entry=False))[0] == []
    acts, notes = run(dec(price=5000.0))
    assert acts == [] and any("less than one share" in n for n in notes)
    acts, notes = run(dec(avail=NOW - 10 * 86400))
    assert acts == [] and any("too old" in n for n in notes)


def test_holding_while_the_strategy_still_wants_the_position_does_nothing():
    assert run(dec(entry=False, target=1.0), pos=10, orders=[leg()])[0] == []


def test_exit_cancels_the_exit_legs_first_then_closes():
    d = dec(entry=False, target=0.0)
    acts, _ = run(d, pos=10, orders=[leg("1"), leg("2")])
    assert [(a.kind, a.order_id) for a in acts] == [("cancel", "leg1"), ("cancel", "leg2")]
    acts, _ = run(d, pos=10)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("close", "sell", 10)]
    acts, _ = run(d, pos=-4)
    assert [(a.kind, a.side, a.qty) for a in acts] == [("close", "buy", 4)]


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
    def recent_orders(self, symbol, since): return []

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

    def fill(self, o):
        o.status = "filled"
        self.pos[o.symbol] = self.pos.get(o.symbol, 0) + (o.qty if o.side == "buy" else -o.qty)


STRATEGY = '''
from dagtrade import Graph, Node, Range, Trade
DEFAULTS = dict(buy_on="2024-06-07", exit_on="2099-01-01", take_profit=0.10, stop_loss=0.05)

class Dates(Node):
    history = Range(start="-3d", interval="d")
    def __init__(self, buy_on, exit_on, **kw):
        super().__init__(buy_on=buy_on, exit_on=exit_on, **kw)
    def run(self, candles, upstream):
        d = str(candles.index[-1].date())
        return {"buy": d == self.buy_on, "exit": d == self.exit_on}

def build(buy_on, exit_on, take_profit, stop_loss):
    return Graph(Trade(Dates(buy_on, exit_on), notional=1000, take_profit=take_profit, stop_loss=stop_loss))
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


def test_decision_comes_from_the_dag_with_levels_from_the_signal_candle(env):
    e = env()
    li = build_live(e.dep, e.factory)
    d = compute_decision(li, pd.Timestamp("2024-06-08 12:00", tz="UTC"))
    assert d.entry and d.target == 1.0 and d.usd == 1000.0
    assert d.stop == pytest.approx(d.price * 0.95) and d.take == pytest.approx(d.price * 1.10)
    d2 = compute_decision(li, pd.Timestamp("2024-06-06 12:00", tz="UTC"))                 # day before the signal
    assert not d2.entry and d2.target == 0.0


def test_runner_sends_one_bracket_entry_for_the_signal_and_never_duplicates(env):
    e = env()
    e.r.cycle()
    (o,) = e.b.orders
    d = e.r.status["deployments"][0]["decision"]
    assert (o.side, o.symbol, o.qty) == ("buy", "TEST", int(1000 // d["price"]))
    assert o.stop_price == pytest.approx(d["price"] * 0.95) and o.limit_price == pytest.approx(d["price"] * 1.10)
    for _ in range(3):
        e.r.cycle()
    assert len(e.b.orders) == 1
    text = (e.root / "journal.jsonl").read_text()
    assert '"type": "decision"' in text and '"type": "action"' in text
    assert json.loads((e.root / "status.json").read_text())["mode"] == "fake"


def test_runner_does_nothing_on_a_day_without_a_signal(env):
    e = env()
    e.clock[0] = ts("2024-06-07 12:00")                  # last closed candle is Thursday: no signal
    e.r.cycle()
    assert e.b.orders == []


def test_exit_signal_cancels_legs_then_closes_the_position(env):
    e = env(dict(exit_on="2024-06-12"))
    e.r.cycle()
    entry = e.b.orders[0]
    e.b.fill(entry)
    e.b.orders.append(BOrder(id="leg", client_id="legs", symbol="TEST", side="sell", qty=entry.qty, status="new",
                             type="stop"))                                   # the bracket's exit leg at Alpaca
    e.clock[0] = ts("2024-06-13 12:00")                  # candle of 12 June (exit) has closed
    e.r.cycle()
    assert [o.status for o in e.b.orders if o.id == "leg"] == ["canceled"]
    e.r.cycle()
    close = e.b.orders[-1]
    assert (close.side, close.qty) == ("sell", entry.qty) and ".cls." in close.client_id


def test_paused_runner_sends_nothing_and_one_strategy_per_symbol(env):
    e = env()
    e.r.set_paused(True)
    e.r.cycle()
    assert e.b.orders == []
    dep2 = Deployment(strategy="livetest", ticker="TEST", start="2024-01-01", live={"enabled": True}, path=e.dep.path)
    with pytest.raises(ValueError, match="one strategy per"):
        LiveRunner([e.dep, dep2], e.b, data_factory=e.factory, root=e.root / "x", charts=False)


# ---------------- failures: retry now -> re-run the whole DAG -> pause briefly ----------------
class HttpError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg or f"http {code}")
        self.status_code = code


def count_decisions(monkeypatch):
    import dagtrade.live.runner as lr
    calls, real = [], lr.compute_decision
    monkeypatch.setattr(lr, "compute_decision", lambda li, now: calls.append(1) or real(li, now))
    return calls


def test_classify_error():
    from dagtrade.live.runner import classify_error
    assert classify_error(HttpError(503)) == classify_error(HttpError(429)) == "transient"
    assert classify_error(HttpError(403)) == classify_error(HttpError(422)) == "permanent"
    assert classify_error(ConnectionError("x")) == classify_error(RuntimeError("Read timed out")) == "transient"
    assert classify_error(ValueError("bad qty")) == "permanent"


def test_transient_error_is_retried_immediately_in_the_same_cycle(env, monkeypatch):
    e = env()
    e.b.fail_seq = [HttpError(503), ConnectionError("reset")]
    calls = count_decisions(monkeypatch)
    e.r.cycle()
    assert len(e.b.orders) == 1 and e.b.attempts == 3
    assert e.sleeps == [0.25, 1.0] and len(calls) == 1          # quick retries; the DAG did not need re-running


def test_persistent_transient_error_reruns_the_whole_dag_then_succeeds(env, monkeypatch):
    e = env()
    e.b.fail_seq = [HttpError(503)] * 4                          # all attempts of the first pass fail
    calls = count_decisions(monkeypatch)
    e.r.cycle()
    assert len(calls) == 2 and len(e.b.orders) == 1             # DAG re-ran on fresh data inside the same cycle
    assert "re-running the whole DAG" in (e.root / "journal.jsonl").read_text()


def test_still_failing_keeps_the_entry_pending_and_retries_from_scratch_shortly(env, monkeypatch):
    e = env()
    e.b.fail_seq = [HttpError(503)] * 8
    calls = count_decisions(monkeypatch)
    e.r.cycle()
    st = e.r.states.load("livetest/TEST")
    assert len(calls) == 2 and e.b.orders == [] and e.b.attempts == 8       # exactly one re-run, no loops
    assert st.retry_not_before == pytest.approx(e.clock[0] + 10) and st.pending is not None
    e.clock[0] += 11                                                        # 10 s pause passes, broker recovers
    e.r.cycle()
    assert len(calls) == 3 and len(e.b.orders) == 1                         # DAG re-run again, then the order went out
    assert e.r.states.load("livetest/TEST").pending is None


def test_an_unsent_entry_is_dropped_when_a_newer_candle_makes_it_obsolete(env):
    e = env()
    e.b.fail_seq = [HttpError(503)] * 8
    e.r.cycle()
    assert e.r.states.load("livetest/TEST").pending is not None
    e.clock[0] = ts("2024-06-12 12:00")                      # newer candles closed; none of them has a signal
    e.r.cycle()
    assert e.b.orders == [] and e.r.states.load("livetest/TEST").pending is None
    assert "obsolete" in (e.root / "journal.jsonl").read_text()


def test_permanent_error_is_not_retried_and_does_not_rerun_the_dag(env, monkeypatch):
    e = env()
    e.b.fail_seq = [HttpError(403, "insufficient buying power")]
    calls = count_decisions(monkeypatch)
    e.r.cycle()
    st = e.r.states.load("livetest/TEST")
    assert e.b.attempts == 1 and e.sleeps == [] and len(calls) == 1 and st.pending is None
    assert st.retry_not_before == pytest.approx(e.clock[0] + 60)


def test_duplicate_client_order_id_counts_as_already_sent(env):
    e = env()
    e.b.fail_seq = [HttpError(422, "client_order_id must be unique")]
    e.r.cycle()
    st = e.r.states.load("livetest/TEST")
    assert st.pending is None and st.retry_not_before == 0.0


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
    assert b.positions() == {"AAA": 10.0, "BBB": -5.0, "CCC": -5.0}      # shorts come back negative
    assert b.account().equity == 100_000 and b.clock().is_open


def test_alpaca_broker_is_paper_only(monkeypatch):
    import alpaca.trading.client as tc
    seen = {}
    monkeypatch.setattr(tc, "TradingClient", lambda k, s, paper: seen.setdefault("paper", paper) or NS())
    AlpacaBroker(key="k", secret="s")
    assert seen["paper"] is True
