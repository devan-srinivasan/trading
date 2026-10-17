"""Golden cross: buy when the 50-candle average crosses above the 200-candle; sell on the death cross."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import sma

DESCRIPTION = "Buy when the 50-candle SMA crosses above the 200-candle SMA (golden cross); exit on the reverse (death cross)."
DEFAULTS = dict(fast=50, slow=200, take_profit=1.0, stop_loss=0.10)


class GoldenCross(Indicator):
    history = Range(candles=276)

    def __init__(self, fast, slow, **kw):
        kw.setdefault("history", Range(candles=slow + 76))      # enough candles for the slow average
        super().__init__(fast=fast, slow=slow, **kw)

    def check(self, candles):
        f, s = sma(candles.close, self.fast), sma(candles.close, self.slow)
        if len(candles) <= self.slow:
            return {}
        return {"buy": bool(f.iloc[-1] > s.iloc[-1] and f.iloc[-2] <= s.iloc[-2]), "exit": bool(f.iloc[-1] < s.iloc[-1])}


def build(fast, slow, take_profit, stop_loss):
    sig = GoldenCross(fast, slow)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
