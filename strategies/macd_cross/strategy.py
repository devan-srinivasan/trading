"""MACD crossover: buy when the MACD line crosses above its signal line; sell when it crosses below."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import macd

DESCRIPTION = "Buy when MACD(12,26) crosses above its 9-candle signal line; exit when it crosses back below."
DEFAULTS = dict(fast=12, slow=26, signal=9, take_profit=1.0, stop_loss=0.10)


class MacdCross(Indicator):
    history = Range(candles=103)

    def __init__(self, fast, slow, signal, **kw):
        super().__init__(fast=fast, slow=slow, signal=signal, **kw)

    def check(self, candles):
        if len(candles) < self.slow + self.signal:
            return {}
        line, sig = macd(candles.close, self.fast, self.slow, self.signal)
        return {"buy": bool(line.iloc[-1] > sig.iloc[-1] and line.iloc[-2] <= sig.iloc[-2]),
                "exit": bool(line.iloc[-1] < sig.iloc[-1])}


def build(fast, slow, signal, take_profit, stop_loss):
    sig = MacdCross(fast, slow, signal)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
