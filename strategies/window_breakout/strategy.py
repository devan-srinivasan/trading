"""N-candle window: a tight consolidation followed by a breakout on rising volume."""
from dagtrade import Graph, Node, Range, Trade

DESCRIPTION = ("Look at the last N daily candles. If that window was a tight range (consolidation) and price then "
               "breaks above it on above-average volume, buy; leave on a close below the recent low or a trailing stop.")
DEFAULTS = dict(window=10, tight=0.08, vol_x=1.2, exit_window=5, stop_loss=0.07)


class TightBreakout(Node):
    history = Range(start="-60d", interval="d")

    def __init__(self, window, tight, vol_x, exit_window, **kw):
        super().__init__(window=window, tight=tight, vol_x=vol_x, exit_window=exit_window, **kw)

    def run(self, candles, upstream):
        if len(candles) < max(self.window, self.exit_window) + 21:
            return {"buy": False}
        last, prior = candles.iloc[-1], candles.iloc[-(self.window + 1):-1]
        top = prior.high.max()
        tight = (top - prior.low.min()) / last.close < self.tight
        loud = last.volume > self.vol_x * candles.volume.iloc[-21:-1].mean()
        leave = last.close < candles.low.iloc[-(self.exit_window + 1):-1].min()
        return {"buy": bool(tight and last.close > top and loud), "exit": bool(leave)}


def build(window, tight, vol_x, exit_window, stop_loss):
    return Graph(Trade(TightBreakout(window, tight, vol_x, exit_window), take_profit=1.0,   # +100%: the trailing stop does the work
                       stop_loss=stop_loss, trail=True))
