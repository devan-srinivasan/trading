"""Wick-by-wick: after a run of down candles, a rejection candle (long lower wick) signals a bounce."""
import numpy as np

from dagtrade import Graph, Node, Range, Trade

DESCRIPTION = ("Wait for N consecutive down closes (capitulation), then buy the first hammer candle (long lower "
               "wick, small body) within a few candles. Hold a few candles with a stop-loss and take-profit.")
DEFAULTS = dict(down_days=3, wick=0.55, window=3, hold=5, stop_loss=0.04, take_profit=0.06)


class HammerAfterSelloff(Node):
    history = Range(start="-30d", interval="d")

    def __init__(self, down_days, wick, window, **kw):
        super().__init__(down_days=down_days, wick=wick, window=window, **kw)

    def run(self, candles, upstream):
        c = candles
        rng = (c.high - c.low).replace(0, np.nan)
        body = (c.close - c.open).abs() / rng
        lower_wick = (np.minimum(c.open, c.close) - c.low) / rng
        hammer = bool(lower_wick.iloc[-1] >= self.wick and body.iloc[-1] <= 0.35)
        selloff = (c.close.diff() < 0).rolling(self.down_days).sum() == self.down_days
        return {"buy": hammer and bool(selloff.iloc[-(self.window + 1):].any())}


def build(down_days, wick, window, hold, stop_loss, take_profit):
    signal = HammerAfterSelloff(down_days, wick, window)
    return Graph(Trade(signal, take_profit=take_profit, stop_loss=stop_loss, max_hold=hold))
