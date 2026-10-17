"""Opening-range breakout with a fair-value-gap retest (day trading, 1-minute candles).

    opening_range  (5m)  today's first 5-minute candle (9:30-9:35 ET) -> its high and low
         | ok
    gap_break      (1m)  a candle closes outside the range and leaves a fair-value gap: three candles where the first
         | ok             and the third do not overlap (short: the third's high is below the first's low)
    retest_engulf  (1m)  price comes back into the gap, then an engulfing candle away from it
         | ok             -> the order: stop just beyond the retest's extreme, target at `rr` x the risk
    trade                 places it, holds until the target / stop / 15:58, then back to the start
Back edges: retest_engulf fails (a close beyond the gap's far side) or waits longer than `retest_wait` candles ->
back to gap_break (find the next gap). gap_break fails at `last_entry` or on a new day -> back to the start.
One trade a day: opening_range waits for tomorrow once today has traded.
"""
from datetime import time

import pandas as pd

from dagtrade import FSA, Node, Range, Trade
from dagtrade.indicators import atr

DESCRIPTION = ("Day trading on 1-minute candles: the first 5-minute candle sets the opening range; wait for a breakout "
               "that leaves a fair-value gap, then a retest of the gap and an engulfing candle away from it. Enter with "
               "the stop beyond the retest and the target at 3x the risk. Short below the range, long above.")
DEFAULTS = dict(rr=3.0, min_gap_atr=0.25, retest_wait=60, last_entry="15:00", take_profit=0.05, stop_loss=0.02)
NY = "America/New_York"


def _ny(ts) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_convert(NY)


class OpeningRange(Node):
    """Start state: succeeds with today's first 5-minute candle's high/low once it has closed."""
    history = Range(start="-1d", interval="5m")

    def __init__(self, last_entry, **kw):
        super().__init__(last_entry=last_entry, **kw)

    def run(self, candles, ctx):
        ny = candles.index.tz_convert(NY)
        today = ny[-1].date()
        if ctx.last_entry is not None and _ny(ctx.last_entry).date() == today:
            return None                                       # already traded today: wait for tomorrow
        if (ny[-1] + pd.Timedelta(minutes=5)).time() > time.fromisoformat(self.last_entry):
            return None                                       # too late for today's setup
        first = candles[(ny.date == today) & (ny.hour == 9) & (ny.minute == 30)]
        if first.empty:
            return None
        return {"high": float(first.high.iloc[0]), "low": float(first.low.iloc[0]), "day": str(today)}


class GapBreak(Node):
    """Wait for the breakout candle that leaves a fair-value gap outside the opening range."""
    history = Range(candles=40, interval="1m")
    max_wait = None                                           # waits all morning; fails at last_entry instead
    needs = ("opening_range",)

    def __init__(self, min_gap_atr, last_entry, **kw):
        super().__init__(min_gap_atr=min_gap_atr, last_entry=last_entry, **kw)

    def run(self, candles, ctx):
        orb = ctx["opening_range"]
        now = _ny(candles.index[-1])
        if str(now.date()) != orb["day"] or now.time() >= time.fromisoformat(self.last_entry):
            return False                                      # the day is over for this setup
        if len(candles) < 20 or _ny(candles.index[-3]).time() < time(9, 35):
            return None                                       # only candles after the opening range
        c1, c2, c3 = candles.iloc[-3], candles.iloc[-2], candles.iloc[-1]
        need = self.min_gap_atr * float(atr(candles, 14).iloc[-1])
        since = candles.index[-1].isoformat()
        if c3.high < c1.low and c1.low - c3.high >= need and c2.close < orb["low"]:
            return {"sell": True, "gap_top": float(c1.low), "gap_bottom": float(c3.high), "since": since}
        if c3.low > c1.high and c3.low - c1.high >= need and c2.close > orb["high"]:
            return {"buy": True, "gap_top": float(c3.low), "gap_bottom": float(c1.high), "since": since}
        return None


class RetestEngulf(Node):
    """Wait for a retest of the gap and then an engulfing candle away from it; succeed with a 3:1 order."""
    history = Range(candles=120, interval="1m")
    needs = ("gap_break",)

    def __init__(self, rr, retest_wait, last_entry, **kw):
        super().__init__(rr=rr, last_entry=last_entry, max_wait=retest_wait, **kw)

    def run(self, candles, ctx):
        g = ctx["gap_break"]
        short = bool(g.get("sell"))
        if _ny(candles.index[-1]).time() >= time.fromisoformat(self.last_entry):
            return False
        after = candles[candles.index > pd.Timestamp(g["since"])]
        if len(after) < 2:
            return None
        top, bot = g["gap_top"], g["gap_bottom"]
        if (short and (after.close > top).any()) or (not short and (after.close < bot).any()):
            return False                                      # closed through the gap: the setup is void
        touched = (after.high >= bot) if short else (after.low <= top)
        if not touched.any():
            return None
        k = int(touched.to_numpy().argmax())                  # the first retest candle
        if len(after) - 1 <= k:
            return None                                       # the engulfing candle must come after the retest
        prev, cur = after.iloc[-2], after.iloc[-1]
        lo_body, hi_body = min(prev.open, prev.close), max(prev.open, prev.close)
        if short:
            engulf = cur.close < cur.open and cur.open >= hi_body and cur.close < lo_body
        else:
            engulf = cur.close > cur.open and cur.open <= lo_body and cur.close > hi_body
        if not engulf:
            return None
        seg, entry = after.iloc[k:], float(cur.close)
        if short:
            stop = float(seg.high.max()) + 0.01
            risk = stop - entry
            out = {"sell": True, "stop_loss_price": stop, "take_profit_price": entry - self.rr * risk}
        else:
            stop = float(seg.low.min()) - 0.01
            risk = entry - stop
            out = {"buy": True, "stop_loss_price": stop, "take_profit_price": entry + self.rr * risk}
        return out if risk > 0 else None


def build(rr, min_gap_atr, retest_wait, last_entry, take_profit, stop_loss):
    orb = OpeningRange(last_entry=last_entry)
    gap = GapBreak(min_gap_atr=min_gap_atr, last_entry=last_entry)
    eng = RetestEngulf(rr=rr, retest_wait=retest_wait, last_entry=last_entry)
    trade = Trade(take_profit=take_profit, stop_loss=stop_loss)       # defaults; the signal gives exact prices
    return FSA(start=orb, edges={
        orb: dict(ok=gap),
        gap: dict(ok=eng),                                    # fail (day over) -> the start
        eng: dict(ok=trade, fail=gap, max_wait=gap),          # missed or void: look for the next gap
    })                                                        # trade -> the start
