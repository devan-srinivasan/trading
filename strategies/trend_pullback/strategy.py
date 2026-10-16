"""Daily trend gate -> hourly pullback entries.

    daily candles -> DailyTrend {"up": bool} -> HourlyPullback {"buy", "exit"} -> Trade
    (the hourly node receives the daily node's latest output, but only once that daily candle has closed)
"""
from dagtrade import Graph, Node, Range, Trade
from dagtrade.indicators import ema, rsi

DESCRIPTION = ("Only trade when the daily trend is up (EMA fast > slow). Inside that trend, buy hourly pullbacks "
               "(RSI oversold), exit when RSI recovers or the trend breaks, with a stop-loss.")
DEFAULTS = dict(fast=20, slow=50, rsi_n=5, rsi_buy=25, rsi_exit=65, stop_loss=0.03, take_profit=0.15)


class DailyTrend(Node):
    history = Range(start="-150d", interval="d")

    def __init__(self, fast, slow, **kw):
        super().__init__(fast=fast, slow=slow, **kw)

    def run(self, candles, upstream):
        return {"up": bool(ema(candles.close, self.fast).iloc[-1] > ema(candles.close, self.slow).iloc[-1])}


class HourlyPullback(Node):
    history = Range(start="-10d", interval="h")

    def __init__(self, trend, rsi_n, rsi_buy, rsi_exit, **kw):
        super().__init__(trend, rsi_n=rsi_n, rsi_buy=rsi_buy, rsi_exit=rsi_exit, **kw)

    def run(self, candles, upstream):
        up = upstream["daily_trend"]["up"]
        r = rsi(candles.close, self.rsi_n).iloc[-1]
        return {"buy": bool(up and r < self.rsi_buy), "exit": bool(r > self.rsi_exit or not up)}


def build(fast, slow, rsi_n, rsi_buy, rsi_exit, stop_loss, take_profit):
    signal = HourlyPullback(DailyTrend(fast, slow), rsi_n, rsi_buy, rsi_exit)
    return Graph(Trade(signal, take_profit=take_profit, stop_loss=stop_loss))
