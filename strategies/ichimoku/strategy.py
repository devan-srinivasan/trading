"""Ichimoku cloud: buy a Tenkan/Kijun bullish cross while price is above the cloud; sell when price falls below it."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator

DESCRIPTION = ("Buy when the Tenkan-sen (9) crosses above the Kijun-sen (26) while the close is above the cloud; "
               "exit when the close falls below the cloud.")
DEFAULTS = dict(tenkan=9, kijun=26, senkou=52, take_profit=1.0, stop_loss=0.10)


def _mid(candles, n):
    return (candles.high.rolling(n).max() + candles.low.rolling(n).min()) / 2


class Ichimoku(Indicator):
    history = Range(candles=138)

    def __init__(self, tenkan, kijun, senkou, **kw):
        super().__init__(tenkan=tenkan, kijun=kijun, senkou=senkou, **kw)

    def check(self, candles):
        if len(candles) < self.senkou + self.kijun + 2:
            return {}
        t, k = _mid(candles, self.tenkan), _mid(candles, self.kijun)
        # the cloud plotted at today was computed kijun candles ago (it is displaced forward): causal by construction
        span_a = ((t + k) / 2).shift(self.kijun)
        span_b = _mid(candles, self.senkou).shift(self.kijun)
        top, bottom = max(span_a.iloc[-1], span_b.iloc[-1]), min(span_a.iloc[-1], span_b.iloc[-1])
        c = candles.close.iloc[-1]
        cross = t.iloc[-1] > k.iloc[-1] and t.iloc[-2] <= k.iloc[-2]
        return {"buy": bool(cross and c > top), "exit": bool(c < bottom)}


def build(tenkan, kijun, senkou, take_profit, stop_loss):
    sig = Ichimoku(tenkan, kijun, senkou)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
