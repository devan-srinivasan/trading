"""Supertrend (ATR 10, multiplier 3): buy when it flips to an uptrend; sell when it flips to a downtrend."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import supertrend

DESCRIPTION = "Buy when Supertrend(10, 3) flips from down to up; exit when it flips back down."
DEFAULTS = dict(n=10, mult=3.0, take_profit=1.0, stop_loss=0.10)


class Supertrend(Indicator):
    history = Range(candles=138)

    def __init__(self, n, mult, **kw):
        super().__init__(n=n, mult=mult, **kw)

    def check(self, candles):
        d = supertrend(candles, self.n, self.mult)
        if d.iloc[-2:].isna().any():
            return {}
        return {"buy": bool(d.iloc[-1] > 0 and d.iloc[-2] < 0), "exit": bool(d.iloc[-1] < 0)}


def build(n, mult, take_profit, stop_loss):
    sig = Supertrend(n, mult)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
