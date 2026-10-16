"""Regime -> signal -> trade, using the optional RegimeNode / SignalNode helpers."""
from dagtrade import Graph, Range, Trade
from dagtrade.nodes import SignalNode, TrendRegime

DESCRIPTION = ("Detect the market regime from the last ~300 days of daily candles. Only while it is an uptrend, buy a "
               "dip that bounces. Leave when the regime turns to a downtrend, or on a stop-loss / take-profit.")
DEFAULTS = dict(fast=20, slow=100, drop=0.04, stop_loss=0.06, take_profit=0.12)


class BuyDipInUptrend(SignalNode):
    history = Range(start="-15d", interval="d")

    def __init__(self, regime, drop, **kw):
        super().__init__(regime, drop=drop, **kw)

    def signal(self, candles, upstream):
        regime = upstream["trend_regime"]["regime"]
        last = candles.iloc[-1]
        dip = last.close <= candles.high.max() * (1 - self.drop) and last.close > last.open
        return {"buy": bool(regime == "up" and dip), "exit": regime == "down"}


def build(fast, slow, drop, stop_loss, take_profit):
    signal = BuyDipInUptrend(TrendRegime(fast=fast, slow=slow), drop)
    return Graph(Trade(signal, take_profit=take_profit, stop_loss=stop_loss))
