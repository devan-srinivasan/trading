"""The simplest possible strategy: a built-in signal + a Trade."""
from dagtrade import Graph, Trade
from dagtrade.nodes import SmaCross

DESCRIPTION = "Buy when the 20-day average crosses above the 50-day; leave when it crosses back below, or on TP/SL."
DEFAULTS = dict(fast=20, slow=50, take_profit=0.20, stop_loss=0.07)


def build(fast, slow, take_profit, stop_loss):
    return Graph(Trade(SmaCross(fast=fast, slow=slow), take_profit=take_profit, stop_loss=stop_loss))
