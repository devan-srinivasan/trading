"""EMA crossover: buy when the 9-candle EMA crosses above the 21-candle EMA; sell when it crosses below."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import ema

DESCRIPTION = "Buy when the 9-candle EMA crosses above the 21-candle EMA; exit when it crosses back below."
DEFAULTS = dict(fast=9, slow=21, take_profit=1.0, stop_loss=0.10)


class EmaCross(Indicator):
    history = Range(candles=83)

    def __init__(self, fast, slow, **kw):
        super().__init__(fast=fast, slow=slow, **kw)

    def check(self, candles):
        f, s = ema(candles.close, self.fast), ema(candles.close, self.slow)
        if s.iloc[-2:].isna().any():
            return {}
        return {"buy": bool(f.iloc[-1] > s.iloc[-1] and f.iloc[-2] <= s.iloc[-2]), "exit": bool(f.iloc[-1] < s.iloc[-1])}


def build(fast, slow, take_profit, stop_loss):
    sig = EmaCross(fast, slow)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
