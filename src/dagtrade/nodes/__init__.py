"""Optional, ready-made nodes. Each is a few lines on top of Node: copy one and change it.

    RegimeNode   implement classify(candles, upstream) -> label        output {"regime": label}
    SignalNode   implement signal(candles, upstream) -> bool or dict   output {"buy": bool} (or your dict)
Nothing in the core depends on this module.
"""
from __future__ import annotations

from ..graph.node import Node
from ..indicators import ema, rsi, sma
from ..range import Range


class RegimeNode(Node):
    """Labels the market state. Output: {"regime": <label>}."""

    def classify(self, candles, upstream):
        raise NotImplementedError

    def run(self, candles, upstream):
        return {"regime": self.classify(candles, upstream)}


class SignalNode(Node):
    """Says buy / not buy. Output: {"buy": bool}, or whatever dict `signal` returns ("sell", "exit", ...)."""

    def signal(self, candles, upstream):
        raise NotImplementedError

    def run(self, candles, upstream):
        r = self.signal(candles, upstream)
        return r if isinstance(r, dict) else {"buy": bool(r)}


class TrendRegime(RegimeNode):
    """"up" / "down" / "range" from a fast and a slow EMA. Needs ~3x `slow` candles of history."""
    history = Range(start="-300d", interval="d")

    def __init__(self, *upstream, fast: int = 20, slow: int = 100, band: float = 0.01, **kw):
        super().__init__(*upstream, fast=fast, slow=slow, band=band, **kw)

    def classify(self, candles, upstream):
        if len(candles) < self.slow:
            return "unknown"
        gap = ema(candles.close, self.fast).iloc[-1] / ema(candles.close, self.slow).iloc[-1] - 1
        return "up" if gap > self.band else "down" if gap < -self.band else "range"


class DipBuy(SignalNode):
    """Buy when price is `drop` below its recent high and the latest candle closes up. If an upstream node
    outputs {"regime": ...} the signal only fires while that regime is "up"."""
    history = Range(start="-15d", interval="d")

    def __init__(self, *upstream, drop: float = 0.03, **kw):
        super().__init__(*upstream, drop=drop, **kw)

    def signal(self, candles, upstream):
        if any(u.get("regime") not in (None, "up") for u in upstream.values()):
            return False
        last = candles.iloc[-1]
        return bool(last.close <= candles.high.max() * (1 - self.drop) and last.close > last.open)


class BreakoutBuy(SignalNode):
    """Buy when the close exceeds the highest high of the candles before it."""
    history = Range(start="-25d", interval="d")

    def signal(self, candles, upstream):
        return bool(len(candles) > 1 and candles.close.iloc[-1] > candles.high.iloc[:-1].max())


class SmaCross(SignalNode):
    """Buy when the fast moving average crosses above the slow one; exit when it crosses back below."""
    history = Range(start="-250d", interval="d")

    def __init__(self, *upstream, fast: int = 20, slow: int = 50, **kw):
        super().__init__(*upstream, fast=fast, slow=slow, **kw)

    def signal(self, candles, upstream):
        f, s_ = sma(candles.close, self.fast), sma(candles.close, self.slow)
        if len(candles) <= self.slow:
            return {"buy": False}
        return {"buy": bool(f.iloc[-1] > s_.iloc[-1] and f.iloc[-2] <= s_.iloc[-2]), "exit": bool(f.iloc[-1] < s_.iloc[-1])}


class RsiOversold(SignalNode):
    """Buy when RSI drops below `buy_below` (oversold); exit when it recovers above `exit_above`."""
    history = Range(start="-60d", interval="d")

    def __init__(self, *upstream, n: int = 14, buy_below: float = 30, exit_above: float = 55, **kw):
        super().__init__(*upstream, n=n, buy_below=buy_below, exit_above=exit_above, **kw)

    def signal(self, candles, upstream):
        r = rsi(candles.close, self.n).iloc[-1]
        return {"buy": bool(r < self.buy_below), "exit": bool(r > self.exit_above)}
