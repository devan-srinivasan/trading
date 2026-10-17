import json

import pandas as pd
import pytest

from conftest import T
from dagtrade import FSA, Go, Node, Range, verify_causal
from dagtrade.fsa import Account, CandleView, FsaRunner, backtest
from dagtrade.sim import SimConfig

START, END = "2022-01-03", "2022-12-30"
CFG = SimConfig(capital=10_000, commission_bps=0, slippage_bps=0)


def day(ts) -> int:
    return (pd.Timestamp(ts) - pd.Timestamp("2022-01-03", tz="UTC")).days


class OnDays(Node):
    """Succeeds on the listed day numbers (days since 3 Jan 2022), waits otherwise."""
    history = Range(candles=3)

    def __init__(self, days=(), fail_on=(), outcome=None, **kw):
        super().__init__(days=tuple(days), fail_on=tuple(fail_on), outcome=outcome, **kw)

    def run(self, candles, ctx):
        d = day(candles.index[-1])
        if d in self.fail_on:
            return False
        if d in self.days:
            return Go(self.outcome, at=d) if self.outcome else {"at": d}
        return None


def run(fsa, data, **kw):
    return backtest(fsa, data, "TEST", START, END, cfg=CFG, **kw)


def path(res):
    return [(e["from"], e["to"], e["outcome"]) for e in res.events]


def test_states_move_on_success_and_a_chain_can_pass_on_one_candle(data):
    a, b = OnDays([3, 10], name="a"), OnDays([10], name="b")
    res = run(FSA.chain(a, b, T(take_profit=5.0, stop_loss=0.9)), data)
    first = path(res)[:2]
    assert first == [("a", "b", "ok"), ("b", "trade", "ok")]
    t = [day(pd.Timestamp(e["t"], unit="s", tz="UTC")) for e in res.events[:2]]
    assert t[0] == 4 and t[1] == 11              # day 3's candle closes on day 4; b passes day 10's candle (closes day 11)


def test_max_wait_fails_back_to_the_start_and_memory_is_cleared(data):
    seen = []

    class B(Node):
        history = Range(candles=3)
        max_wait = 4

        def run(self, candles, ctx):
            seen.append(dict(ctx.memory))
            return None

    res = run(FSA.chain(OnDays([3], name="a"), B(), T()), data)
    ev = path(res)
    assert ev[:2] == [("a", "b", "ok"), ("b", "a", "max_wait")]
    assert len(seen) == 4 and seen[0] == {"a": {"at": 3}}                 # b saw a's output, 4 candles, then gave up
    assert res.events[1]["reason"].startswith("no answer within 4")


def test_fail_edges_named_outcomes_and_back_edges_keep_earlier_memory(data):
    mem = []

    class C(Node):
        history = Range(candles=3)
        max_wait = None

        def run(self, candles, ctx):
            mem.append(sorted(ctx.memory))
            return False if len(mem) == 1 else {"buy": True}

    a, b, c = OnDays([3], name="a"), OnDays([4, 8], outcome="up", name="b"), C()          # weekdays only
    fsa = FSA(start=a, edges={a: dict(ok=b), b: dict(up=c), c: dict(ok=T(take_profit=5.0, stop_loss=0.9), fail=b)})
    res = run(fsa, data)
    assert path(res)[:4] == [("a", "b", "ok"), ("b", "c", "up"), ("c", "b", "fail"), ("b", "c", "up")]
    assert mem[0] == ["a", "b"] and mem[1] == ["a", "b"]           # back at b, a's output survives; b's is fresh


def test_unknown_outcome_and_bad_wiring_are_errors(data):
    with pytest.raises(ValueError, match="no edge"):
        run(FSA.chain(OnDays([3], outcome="weird", name="a"), T()), data)
    with pytest.raises(ValueError, match="Trade"):
        FSA.chain(OnDays([3], name="a"))
    with pytest.raises(ValueError, match="unique"):
        FSA.chain(OnDays([3], name="a"), OnDays([4], name="a"), T())
    with pytest.raises(TypeError, match="edges"):
        Node(OnDays([2]))                                          # states are wired by edges, not nesting
    with pytest.raises(ValueError, match="max_wait"):
        OnDays([1], max_wait="soon")


def test_a_state_only_sees_closed_candles_of_its_own_interval(data):
    seen = []

    class Daily(Node):
        history = Range(candles=5, interval="1d")

        def run(self, candles, ctx):
            seen.append((ctx.now, candles.index[-1]))
            return None

    backtest(FSA.chain(Daily(), T()), data, "TEST", "2022-03-01", "2022-04-29", base_tf="1h", cfg=CFG)
    days = [s[1] for s in seen]
    assert len(days) == len(set(days)) > 30                        # once per new daily candle, not every hour
    assert all(last + pd.Timedelta(days=1) <= now for now, last in seen)   # that daily candle had closed


def test_a_node_without_an_interval_runs_on_the_fsa_interval(data):
    class Last(Node):
        history = Range(candles=3)

        def run(self, candles, ctx):
            Last.n.append(len(candles))
            return None
    for tf in ("1d", "1h"):
        Last.n = []
        fsa = FSA.chain(Last(), T())
        assert fsa.resolve_tfs(tf)[1] == tf and fsa.resolve_tfs()[1] == "1d"
        backtest(fsa, data, "TEST", "2022-03-01", "2022-03-31", base_tf=tf, cfg=CFG)
        assert Last.n and max(Last.n) == 3


def test_max_wait_as_a_duration_and_start_state_waits_forever(data):
    class Slow(Node):
        history = Range(candles=3)
        max_wait = "3d"

        def run(self, candles, ctx):
            return None
    res = run(FSA.chain(OnDays([10, 40], name="a"), Slow(), T()), data)
    ev = res.events
    assert [e["outcome"] for e in ev[:2]] == ["ok", "max_wait"]
    assert ev[1]["t"] - ev[0]["t"] > 3 * 86400                       # gave up after more than 3 days
    fsa = FSA.chain(OnDays([], name="a"), T())
    assert fsa.max_wait("a") == (None, None) and run(fsa, data).events == []


def test_start_midway_needs_what_that_state_reads(data):
    class NeedsA(Node):
        history = Range(candles=3)
        needs = ("a",)

        def run(self, candles, ctx):
            return {"buy": True} if ctx["a"]["go"] else None

    fsa = FSA.chain(OnDays([], name="a"), NeedsA(), T(take_profit=5.0, stop_loss=0.9))
    with pytest.raises(ValueError, match="needs the output of a"):
        FsaRunner(fsa).reset("needs_a")
    res = run(fsa, data, start_at="needs_a", start_memory={"a": {"go": True}})
    assert path(res)[0] == ("needs_a", "trade", "ok")


def test_runner_state_is_plain_json_and_resumes_exactly(data):
    def make():
        return FSA.chain(OnDays([3, 30, 60], name="a"), OnDays([5, 33, 61], fail_on=[40], name="b"), T())

    view = CandleView(data, "TEST", "2021-12-01", END, ["1d"])
    grid = pd.DatetimeIndex(view.frame("1d")["avail"])
    grid = grid[grid >= pd.Timestamp(START, tz="UTC")]
    flat = Account(cash=10_000, equity=10_000)
    one = FsaRunner(make())
    for t in grid:
        one.step(t, view, flat)
    a = FsaRunner(make())
    for t in grid[:40]:
        a.step(t, view, flat)
    b = FsaRunner(make(), state=json.loads(json.dumps(a.state)))           # e.g. a live restart
    for t in grid[40:]:
        b.step(t, view, flat)
    assert [(e["from"], e["to"]) for e in a.events + b.events] == [(e["from"], e["to"]) for e in one.events]


def test_lookahead_check_passes_honest_fsas_and_catches_a_leaky_engine(data, monkeypatch):
    make = lambda: FSA.chain(OnDays([3, 30, 90, 150], name="a"), T())          # noqa: E731
    assert verify_causal(make, data, "TEST", START, END, cfg=CFG).ok

    class Peek(Node):
        history = Range(candles=3)

        def run(self, candles, ctx):
            return {"buy": True} if candles.close.iloc[-1] > candles.close.iloc[-2] else None
    real = CandleView.window

    def leaky(self, node, tf, idx):                                         # hand nodes one candle of the future
        return real(self, node, tf, min(idx + 1, len(self.frames[tf]) - 1))
    monkeypatch.setattr(CandleView, "window", leaky)
    rep = verify_causal(lambda: FSA.chain(Peek(), T(max_hold=1)), data, "TEST", START, END, cfg=CFG, n_cutoffs=10)
    assert not rep.ok                                                       # caught at the cutoffs


def test_required_history_comes_from_the_ranges_including_exit_checks():
    exit_ = OnDays([], name="x", history=Range(start="-90d", interval="1d"))
    fsa = FSA.chain(OnDays([], name="a"), T(exit=exit_))
    assert fsa.required_history(pd.Timestamp("2024-06-01", tz="UTC")) >= pd.Timedelta(days=90)
    d = fsa.describe()
    assert [n["id"] for n in d["nodes"]] == ["a", "trade"] and d["exits"][0]["id"] == "x"
    assert {(e["from"], e["to"], e["outcome"], e["default"]) for e in d["edges"]} >= {
        ("a", "trade", "ok", False), ("trade", "a", "ok", True), ("trade", "a", "fail", True)}
