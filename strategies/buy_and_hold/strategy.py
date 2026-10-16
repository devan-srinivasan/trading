"""Sanity check: always buy. Its result must match simply buying the stock (minus tiny costs)."""
from dagtrade import Graph, Node, Range, Trade

DESCRIPTION = "Buy once and hold. If this does not match buy & hold, something is broken."
DEFAULTS = {}


class AlwaysBuy(Node):
    history = Range(start="-5d", interval="d")

    def run(self, candles, upstream):
        return {"buy": True}


def build():
    return Graph(Trade(AlwaysBuy(), take_profit=1000.0, stop_loss=0.99))   # exits so far away they never fire
