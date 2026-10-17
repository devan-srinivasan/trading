"""Bollinger mean reversion: buy a close below the lower band; sell when price gets back to the middle band."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import bollinger

DESCRIPTION = "Buy when the close falls below the lower Bollinger Band (20, 2 sd); exit when it closes above the middle band."
DEFAULTS = dict(n=20, k=2.0, take_profit=1.0, stop_loss=0.10)


class BollingerReversion(Indicator):
    edge = True                   # a level, not a crossing: enter when it first becomes true
    history = Range(candles=42)

    def __init__(self, n, k, **kw):
        super().__init__(n=n, k=k, **kw)

    def check(self, candles):
        if len(candles) < self.n:
            return {}
        lower, mid, _ = bollinger(candles.close, self.n, self.k)
        c = candles.close.iloc[-1]
        return {"buy": bool(c < lower.iloc[-1]), "exit": bool(c > mid.iloc[-1])}


def build(n, k, take_profit, stop_loss):
    sig = BollingerReversion(n, k)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
