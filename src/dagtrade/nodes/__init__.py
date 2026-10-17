"""Optional, ready-made nodes. Each is a few lines on top of Node: copy one and change it.

    Indicator    implement check(candles) -> {"buy": bool, "exit": bool, ...}. As a state it waits until "buy" (or
                 "sell") is true and passes the dict on to the Trade; `.as_exit()` gives the same rule as a Trade's
                 exit check (closes when "exit" is true). One class, both halves of a classic indicator strategy:
                     sig = GoldenCross(fast=50, slow=200)
                     FSA.chain(sig, Trade(take_profit=1.0, stop_loss=0.1, exit=sig.as_exit()))
    RegimeNode   implement classify(candles) -> label; succeeds once the label is `want` (default "up")
    SignalNode   implement signal(candles, ctx) -> bool or dict; succeeds when it is truthy
Nothing in the core depends on this module.
"""
from __future__ import annotations

import copy

from ..fsa.node import Node
from ..indicators import ema, rsi, sma
from ..range import Range


class Indicator(Node):
    """check(candles) -> {"buy"/"sell": bool, "exit": bool, plus any order keys (stop_loss_price, ...)}.

    edge = True: only enter when the signal has just switched on (it was off on the previous candle), so a condition
    that stays true for days does not re-enter right after a stop-out. Crossings are events already; leave it False.
    A candle that says both buy and exit does not enter."""
    role = "entry"
    edge = False

    def check(self, candles) -> dict:
        raise NotImplementedError

    def run(self, candles, ctx):
        r = self.check(candles) or {}
        if self.role == "exit":
            return bool(r.get("exit"))
        if not (r.get("buy") or r.get("sell")) or r.get("exit"):
            return None
        if self.edge and len(candles) > 1:
            p = self.check(candles.iloc[:-1]) or {}
            if (r.get("buy") and p.get("buy")) or (r.get("sell") and p.get("sell")):
                return None
        return {k: v for k, v in r.items() if k != "exit"}

    def as_exit(self, name: str | None = None) -> "Indicator":
        """The same indicator as a Trade's exit check: True when "exit" is."""
        x = copy.copy(self)
        x.role, x.name = "exit", name or f"{self.name}_exit"
        return x


class RegimeNode(Node):
    """Labels the market state; succeeds (passing {"regime": label}) once the label is `want`."""
    want = "up"

    def classify(self, candles):
        raise NotImplementedError

    def run(self, candles, ctx):
        label = self.classify(candles)
        return {"regime": label} if label == self.want else None


class SignalNode(Node):
    """Succeeds when signal() is truthy; a dict is passed on (e.g. {"buy": True, "stop_loss_price": 95})."""

    def signal(self, candles, ctx):
        raise NotImplementedError

    def run(self, candles, ctx):
        r = self.signal(candles, ctx)
        if not r:
            return None
        return r if isinstance(r, dict) else {"buy": True}


class TrendRegime(RegimeNode):
    """"up" / "down" / "range" from a fast and a slow EMA. Needs ~3x `slow` candles of history."""
    history = Range(candles=300)

    def __init__(self, fast: int = 20, slow: int = 100, band: float = 0.01, want: str = "up", **kw):
        super().__init__(fast=fast, slow=slow, band=band, **kw)
        self.want = want

    def classify(self, candles):
        if len(candles) < self.slow:
            return "unknown"
        gap = ema(candles.close, self.fast).iloc[-1] / ema(candles.close, self.slow).iloc[-1] - 1
        return "up" if gap > self.band else "down" if gap < -self.band else "range"


class DipBuy(SignalNode):
    """Buy when price is `drop` below its recent high and the latest candle closes up."""
    history = Range(candles=15)

    def __init__(self, drop: float = 0.03, **kw):
        super().__init__(drop=drop, **kw)

    def signal(self, candles, ctx):
        last = candles.iloc[-1]
        return bool(last.close <= candles.high.max() * (1 - self.drop) and last.close > last.open)


class BreakoutBuy(SignalNode):
    """Buy when the close exceeds the highest high of the candles before it."""
    history = Range(candles=25)

    def signal(self, candles, ctx):
        return bool(len(candles) > 1 and candles.close.iloc[-1] > candles.high.iloc[:-1].max())


class SmaCross(Indicator):
    """Buy when the fast moving average crosses above the slow one; exit when it crosses back below."""
    history = Range(candles=250)

    def __init__(self, fast: int = 20, slow: int = 50, **kw):
        super().__init__(fast=fast, slow=slow, **kw)

    def check(self, candles):
        if len(candles) <= self.slow:
            return {}
        f, s_ = sma(candles.close, self.fast), sma(candles.close, self.slow)
        return {"buy": bool(f.iloc[-1] > s_.iloc[-1] and f.iloc[-2] <= s_.iloc[-2]), "exit": bool(f.iloc[-1] < s_.iloc[-1])}


class RsiOversold(Indicator):
    """Buy when RSI drops below `buy_below` (oversold); exit when it recovers above `exit_above`."""
    history = Range(candles=60)
    edge = True

    def __init__(self, n: int = 14, buy_below: float = 30, exit_above: float = 55, **kw):
        super().__init__(n=n, buy_below=buy_below, exit_above=exit_above, **kw)

    def check(self, candles):
        r = rsi(candles.close, self.n).iloc[-1]
        return {"buy": bool(r < self.buy_below), "exit": bool(r > self.exit_above)}
