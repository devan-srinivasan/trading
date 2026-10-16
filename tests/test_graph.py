import numpy as np
import pandas as pd
import pytest

from conftest import T
from dagtrade import Graph, Node, Range, verify_causal
from dagtrade.graph import asof_align

START, END = "2022-01-01", "2023-06-30"


class Daily(Node):
    history = Range(start="-30d", interval="d")

    def run(self, candles, upstream):
        return {"close": float(candles.close.iloc[-1]), "n": len(candles)}


class Hourly(Node):
    history = Range(start="-3d", interval="h")

    def run(self, candles, upstream):
        return {"daily_close": upstream["daily"]["close"], "buy": False}


def test_node_sees_candles_oldest_first_ending_at_the_one_that_just_closed(data):
    seen = []

    class Probe(Node):
        history = Range(start="-5d", interval="d")

        def run(self, candles, upstream):
            seen.append((candles.index[0], candles.index[-1], len(candles)))
            return {"buy": False}

    res = Graph(T(Probe())).run(data, "TEST", START, END, cache=False)
    raw = res.ctx.raw_bars("1d")
    assert len(seen) == len(raw)
    assert all(last == raw.index[i] for i, (_, last, _) in enumerate(seen))           # window ends at current candle
    assert max(n for *_, n in seen) <= 6 and seen[0][2] == 1


def test_slower_node_output_reaches_faster_node_only_after_its_candle_closes(data):
    g = Graph(T(Hourly(Daily())))
    res = g.run(data, "TEST", START, END, cache=False)
    hourly = res.by_name("hourly")
    daily_raw = res.ctx.raw_bars("1d")
    grid = res.ctx.grid("1h")
    checked = 0
    for t, out in list(zip(grid, hourly))[::97][:40]:
        if out is None:
            continue
        done = daily_raw[daily_raw["avail"] <= t]
        assert out["daily_close"] == pytest.approx(done["close"].iloc[-1])           # last COMPLETED daily candle
        today = daily_raw[(daily_raw.index <= t) & (daily_raw["avail"] > t)]
        if len(today):
            assert out["daily_close"] != pytest.approx(today["close"].iloc[-1])      # never the still-forming one
        checked += 1
    assert checked > 10


def test_upstream_not_ready_means_no_output_and_duplicate_names_are_rejected(data):
    res = Graph(T(Hourly(Daily()))).run(data, "TEST", START, END, cache=False)
    assert res.by_name("hourly").iloc[0] is None                                      # no daily output yet at the start
    with pytest.raises(ValueError, match="distinct names"):
        Hourly(Daily(), Daily())
    Hourly(Daily(name="a"), Daily(name="b"))


def test_leaky_node_is_caught(data):
    class Peek(Node):
        history = Range(start="-5d", interval="d")

        def compute(self, ctx, *ups):                      # takes over and reads tomorrow's close
            c = ctx.bars("1d")["close"]
            return pd.Series([{"buy": bool(x)} for x in (c.shift(-1) > c)], index=ctx.index, dtype=object)

    rep = verify_causal(Graph(T(Peek(name="peek"))), data, "TEST", START, END)
    assert not rep.ok and any(n == "peek" for n, _ in rep.leaks)


def test_honest_multi_timeframe_graph_passes_the_causality_check(data):
    rep = verify_causal(Graph(T(Hourly(Daily()))), data, "TEST", "2023-01-01", "2023-04-01")
    assert rep.ok


def test_results_are_cached_and_params_change_the_key(data, tmp_path):
    class Param(Node):
        history = Range(start="-10d", interval="d")

        def run(self, candles, upstream):
            return {"buy": bool(candles.close.iloc[-1] > candles.close.mean() * (1 + self.k))}

    g = Graph(T(Param(k=0.01)))
    r1 = g.run(data, "TEST", START, END, cache_dir=tmp_path / "c")
    r2 = g.run(data, "TEST", START, END, cache_dir=tmp_path / "c")
    assert r1.cache_hits == 0 and r2.cache_hits >= 1
    r3 = Graph(T(Param(k=0.05))).run(data, "TEST", START, END, cache_dir=tmp_path / "c")
    assert r3.cache_hits == 0


def test_graph_must_end_in_a_trade_node():
    with pytest.raises(TypeError, match="Trade"):
        Graph(Daily())


def test_required_history_comes_from_the_ranges():
    g = Graph(T(Hourly(Daily())))
    assert g.required_history(pd.Timestamp("2024-06-01", tz="UTC")) >= pd.Timedelta(days=30)


def test_a_node_checks_upstream_before_touching_candles(data):
    calls = []

    class Waiter(Node):
        history = Range(start="-5d", interval="d")

        def run(self, candles, upstream):
            return {"ok": bool(candles.close.iloc[-1] > candles.close.mean())}

    class Gated(Node):
        history = Range(start="-5d", interval="d")

        def ready(self, upstream):
            return upstream["waiter"]["ok"]

        def run(self, candles, upstream):
            calls.append(candles.index[-1])
            return {"buy": True}

    g = Graph(T(Gated(Waiter())))
    res = g.run(data, "TEST", START, END, cache=False)
    w, out = res.by_name("waiter"), res.by_name("gated")
    ok = [bool(x and x["ok"]) for x in w]
    assert len(calls) == sum(ok) and 0 < len(calls) < len(w)          # run() only called where the waiter succeeded
    assert [o is not None for o in out] == ok                         # elsewhere: output None, so downstream waits too
