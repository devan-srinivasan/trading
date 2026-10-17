"""Larry Connors RSI(2): in a long-term uptrend, buy short-term panic; sell the bounce."""
from dagtrade import FSA, Range, Trade
from dagtrade.nodes import Indicator
from dagtrade.indicators import rsi, sma

DESCRIPTION = ("Only when the close is above the 200-candle SMA: buy when RSI(2) drops below 10; exit when the close "
               "rises above the 5-candle SMA.")
DEFAULTS = dict(trend=200, rsi_n=2, buy_below=10, exit_sma=5, take_profit=1.0, stop_loss=0.10)


class ConnorsRsi2(Indicator):
    edge = True                   # a level, not a crossing: enter when it first becomes true
    history = Range(candles=276)

    def __init__(self, trend, rsi_n, buy_below, exit_sma, **kw):
        kw.setdefault("history", Range(candles=trend + 76))     # enough candles for the trend average
        super().__init__(trend=trend, rsi_n=rsi_n, buy_below=buy_below, exit_sma=exit_sma, **kw)

    def check(self, candles):
        if len(candles) < self.trend:
            return {}
        c = candles.close
        up = c.iloc[-1] > sma(c, self.trend).iloc[-1]
        return {"buy": bool(up and rsi(c, self.rsi_n).iloc[-1] < self.buy_below),
                "exit": bool(c.iloc[-1] > sma(c, self.exit_sma).iloc[-1])}


def build(trend, rsi_n, buy_below, exit_sma, take_profit, stop_loss):
    sig = ConnorsRsi2(trend, rsi_n, buy_below, exit_sma)
    return FSA.chain(sig, Trade(take_profit=take_profit, stop_loss=stop_loss, exit=sig.as_exit()))
