"""Stochastic oscillator: buy when %K crosses above %D in oversold territory (<20); sell when %K goes above 80."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import stochastic

DESCRIPTION = "Buy when stochastic %K(14,3) crosses above %D while both are below 20; exit when %K rises above 80."
DEFAULTS = dict(n=14, smooth=3, low=20, high=80, take_profit=1.0, stop_loss=0.10)


class StochasticCross(Indicator):
    history = Range(candles=42)

    def __init__(self, n, smooth, low, high, **kw):
        super().__init__(n=n, smooth=smooth, low=low, high=high, **kw)

    def check(self, candles):
        k, d = stochastic(candles, self.n, self.smooth)
        if k.iloc[-2:].isna().any() or d.iloc[-2:].isna().any():
            return {}
        cross = k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]
        return {"buy": bool(cross and d.iloc[-1] < self.low), "exit": bool(k.iloc[-1] > self.high)}


def build(n, smooth, low, high, take_profit, stop_loss):
    sig = StochasticCross(n, smooth, low, high)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
