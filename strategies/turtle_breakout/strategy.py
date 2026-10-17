"""Turtle Trading System 1: buy a 20-candle high breakout, exit on a 10-candle low, stop 2 ATR (the turtles' "2N") below."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import atr

DESCRIPTION = ("Buy when the close breaks above the highest high of the previous 20 candles; exit on a close below the "
               "lowest low of the previous 10 candles, with a stop 2 x ATR(20) below the entry.")
DEFAULTS = dict(entry_n=20, exit_n=10, atr_mult=2.0, take_profit=1.0, stop_loss=0.10)


class TurtleBreakout(Indicator):
    edge = True                   # a level, not a crossing: enter when it first becomes true
    history = Range(candles=42)

    def __init__(self, entry_n, exit_n, atr_mult, **kw):
        super().__init__(entry_n=entry_n, exit_n=exit_n, atr_mult=atr_mult, **kw)

    def check(self, candles):
        if len(candles) < self.entry_n + 21:
            return {}
        c = candles.close.iloc[-1]
        n = atr(candles, 20).iloc[-1]
        out = {"buy": bool(c > candles.high.iloc[-self.entry_n - 1:-1].max()),
               "exit": bool(c < candles.low.iloc[-self.exit_n - 1:-1].min())}
        if out["buy"] and n == n:
            out["stop_loss_price"] = c - self.atr_mult * n        # per-order stop from volatility
        return out


def build(entry_n, exit_n, atr_mult, take_profit, stop_loss):
    sig = TurtleBreakout(entry_n, exit_n, atr_mult)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
