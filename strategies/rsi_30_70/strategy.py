"""Classic RSI: buy when RSI(14) climbs back above 30 (leaving oversold); sell when it crosses above 70."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import rsi

DESCRIPTION = "Buy when RSI(14) crosses back above 30 (oversold recovery); exit when RSI crosses above 70 (overbought)."
DEFAULTS = dict(n=14, low=30, high=70, take_profit=1.0, stop_loss=0.10)


class Rsi3070(Indicator):
    history = Range(candles=62)

    def __init__(self, n, low, high, **kw):
        super().__init__(n=n, low=low, high=high, **kw)

    def check(self, candles):
        r = rsi(candles.close, self.n)
        if r.iloc[-2:].isna().any():
            return {}
        return {"buy": bool(r.iloc[-2] < self.low <= r.iloc[-1]), "exit": bool(r.iloc[-1] > self.high)}


def build(n, low, high, take_profit, stop_loss):
    sig = Rsi3070(n, low, high)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
