"""The two building blocks of a strategy.

    Node   candles in -> structured data out.  `run(candles, upstream)` is called every time a candle of the node's
           interval closes; `candles` is the window described by the node's `history` Range, `upstream` holds the
           latest output of each upstream node (keyed by that node's name).
    Trade  the last node: turns a signal into an order (fixed dollar size, optional take-profit / stop-loss).

        class Uptrend(Node):
            history = Range(start="-200d", interval="d")
            def run(self, candles, upstream):
                return {"up": candles.close.iloc[-1] > candles.close.mean()}

        class Dip(Node):
            history = Range(start="-10d", interval="h")
            def run(self, candles, upstream):
                dip = candles.close.iloc[-1] < 0.97 * candles.close.max()
                return {"buy": upstream["uptrend"]["up"] and dip}

        strategy = Graph(Trade(Dip(Uptrend()), notional=1000, take_profit=0.10, stop_loss=0.05))

Everything else (regimes, signals, indicators) is optional sugar built on these; nothing here depends on it.
"""
from __future__ import annotations

import hashlib
import re
import sys
from typing import Any

import numpy as np
import pandas as pd

from ..range import Range

OHLCV = ["open", "high", "low", "close", "volume"]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _module_fingerprint(cls: type) -> str:
    """Hash of the source file that defines the node, so editing ANY helper in it invalidates cached results."""
    mod = sys.modules.get(cls.__module__)
    path = getattr(mod, "__file__", None)
    try:
        return hashlib.sha1(open(path, "rb").read()).hexdigest() if path else cls.__qualname__
    except OSError:
        return cls.__qualname__


class Node:
    """Candles in, structured data out. Subclass and implement `run`."""

    kind = "node"
    cacheable = True
    plot = "auto"                                   # "auto" | "hidden" (dashboard)
    history: Range = Range(start="-60d", interval="d")

    def __init__(self, *upstream: "Node", history: Range | None = None, name: str | None = None, **params: Any):
        for u in upstream:
            if not isinstance(u, Node):
                raise TypeError(f"{type(self).__name__}: upstream must be Nodes, got {type(u).__name__}")
        self.inputs: list[Node] = list(upstream)
        self.name = name or _snake(type(self).__name__)
        names = [u.name for u in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: upstream nodes need distinct names (got {names}); pass name=...")
        if history is not None:
            self.history = history
        self.tf = self.history.tf
        self.params = params
        for k, v in params.items():                 # Dip(drop=0.03) -> self.drop
            if hasattr(self, k):
                raise ValueError(f"{type(self).__name__}: parameter {k!r} clashes with an existing attribute")
            setattr(self, k, v)

    # -- what you write ---------------------------------------------------------------------------
    def run(self, candles: pd.DataFrame, upstream: dict[str, Any]) -> Any:
        """candles: DataFrame [open high low close volume], index = candle open time (UTC), oldest first, the LAST
        row is the candle that just closed. upstream: {node name: that node's latest output}.
        Return anything picklable, usually a dict. Return None for 'nothing to say'."""
        raise NotImplementedError(f"{type(self).__name__} must implement run(candles, upstream)")

    def ready(self, upstream: dict[str, Any]) -> bool:
        """Checked BEFORE any candles are looked at: should this node run for this candle at all?
        Default: yes once every upstream node has produced an output. Override to wait for a success criterion,
        e.g. `return upstream["regime"]["ok"]`. When it returns False the node outputs None (nothing to say), so
        everything downstream of it waits too."""
        return True

    # -- engine -----------------------------------------------------------------------------------
    def spec(self) -> str:
        return "|".join([type(self).__qualname__, _module_fingerprint(type(self)), repr(sorted(self.params.items())),
                         repr(self.history)])

    def compute(self, ctx, *ups):
        raw = ctx.raw_bars(self.tf)
        n = len(raw)
        lo, hi = self.history.bounds(pd.DatetimeIndex(raw.index), pd.DatetimeIndex(raw["avail"]))
        frame = raw[OHLCV]
        names = [u.name for u in self.inputs]
        cols = [u.to_numpy(dtype=object) for u in ups]
        out: list = [None] * n
        for i in range(n):
            vals = [c[i] for c in cols]
            if any(v is None for v in vals):
                continue                             # an upstream node has not produced anything yet
            up = dict(zip(names, vals))
            if not self.ready(up) or hi[i] <= lo[i]:
                continue                             # upstream criterion not met: do not even look at the candles
            out[i] = self.run(frame.iloc[lo[i]: hi[i]], up)
        return pd.Series(out, index=ctx.index, dtype=object)

    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"


class Trade(Node):
    """The node that places orders. Take-profit and stop-loss are REQUIRED; everything else has a default.

        Trade(signal, take_profit=0.10, stop_loss=0.05)                       # $1000, market entry
        Trade(signal, take_profit=0.10, stop_loss=0.05, notional=500, trail=True, max_hold=20)

    The signal node returns a dict. Keys it may contain (all optional except a direction):
        "buy": True | "sell": True      open a long / short           "exit": True   close the position now
        "price": 101.5                  limit entry: only fills if the next candle trades through it
        "usd": 750  or  "qty": 5        size of this order (default: the Trade's `notional`)
        "take_profit": 0.08 / "stop_loss": 0.03          fractions of the entry price, this order only
        "take_profit_price": 112 / "stop_loss_price": 96  absolute prices, this order only
        "trail": True, "max_hold": 10                     this order only
    Anything in the signal overrides the Trade's own defaults; unknown keys are ignored. So the simplest strategy is
    {"buy": True, "price": 100, "take_profit_price": 110, "stop_loss_price": 95}.

    Fractions are measured from the entry reference price: the signal candle's close (market entry) or your limit
    price. A fresh signal (False -> True) opens a position only when flat.
    A Trade never talks to a broker. It states the order; the backtest simulator (or the live Alpaca runner) carries
    it out. In a backtest the position is followed candle by candle: a high/low touching your take-profit or
    stop-loss ends the trade, and the simulator fills it at that price (at the open if the candle gapped through it;
    stop wins if one candle touches both).
    Output per candle: target (+1/0/-1), usd, stop, take, limit, entry (True on the candle where an order is placed).
    """
    kind = "trade"
    plot = "hidden"
    cacheable = False

    def __init__(self, signal: Node, take_profit: float, stop_loss: float, notional: float = 1000.0, *,
                 trail: bool = False, max_hold: int | None = None, history: Range | None = None, name: str = "trade"):
        if take_profit is None or stop_loss is None:
            raise ValueError("Trade needs both take_profit and stop_loss (fractions of the entry price, e.g. 0.10 / 0.05)")
        if not take_profit > 0 or not 0 < stop_loss < 1:
            raise ValueError("take_profit must be > 0 and stop_loss between 0 and 1 (fractions, not prices)")
        super().__init__(signal, history=history or Range(start="-5d", interval=signal.tf), name=name,
                         take_profit=float(take_profit), stop_loss=float(stop_loss), notional=float(notional),
                         trail=trail, max_hold=max_hold)

    def _order(self, s: dict, d: int, close: float) -> dict:
        """Turn one signal dict into a concrete order (limit, usd, stop/take prices, trail, max_hold)."""
        limit = float(s["price"]) if s.get("price") is not None else None
        ref = limit if limit is not None else close
        usd = float(s["usd"]) if s.get("usd") is not None else \
            float(s["qty"]) * ref if s.get("qty") is not None else self.notional
        tp = float(s["take_profit"]) if s.get("take_profit") is not None else self.take_profit
        sl = float(s["stop_loss"]) if s.get("stop_loss") is not None else self.stop_loss
        tak = float(s["take_profit_price"]) if s.get("take_profit_price") is not None else ref * (1 + d * tp)
        stp = float(s["stop_loss_price"]) if s.get("stop_loss_price") is not None else ref * (1 - d * sl)
        ok = stp < ref < tak if d > 0 else tak < ref < stp
        if not ok:
            raise ValueError(f"{self.name}: a {'long' if d > 0 else 'short'} at {ref:.2f} needs "
                             f"{'stop < price < take' if d > 0 else 'take < price < stop'}; got stop {stp:.2f}, take {tak:.2f}")
        return dict(d=d, limit=limit, usd=usd, stp=stp, tak=tak, ref=ref, trail=bool(s.get("trail", self.trail)),
                    max_hold=s.get("max_hold", self.max_hold), sl_frac=abs(ref - stp) / ref)

    def compute(self, ctx, sig):
        raw = ctx.raw_bars(self.tf)
        hi_, lo_, cl = (raw[c].to_numpy(float) for c in ("high", "low", "close"))
        sigs = sig.to_numpy(dtype=object)
        n = len(raw)
        target, usd_o = np.zeros(n), np.zeros(n)
        stop_o, take_o, limit_o = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        entry_o = np.zeros(n, bool)
        pos, held, cur, pend = 0, 0, None, None          # cur = the order behind the open position; pend = unfilled limit
        stp = tak = np.nan
        prev_buy = prev_sell = False
        for i in range(n):
            s = sigs[i] if isinstance(sigs[i], dict) else {}
            buy, sell, leave = bool(s.get("buy")), bool(s.get("sell")), bool(s.get("exit"))
            c = cl[i]
            if pend is not None:                          # a limit order placed on the previous candle: filled?
                d, L = pend["d"], pend["limit"]
                if (d > 0 and lo_[i] <= L) or (d < 0 and hi_[i] >= L):
                    pos, held, cur, stp, tak = d, 0, pend, pend["stp"], pend["tak"]
                pend = None
            closed_now = False
            if pos != 0:
                held += 1
                hit = (pos > 0 and (lo_[i] <= stp or hi_[i] >= tak)) or (pos < 0 and (hi_[i] >= stp or lo_[i] <= tak))
                if hit or leave or (cur["max_hold"] and held >= cur["max_hold"]):
                    pos, cur, closed_now = 0, None, True
                elif cur["trail"]:
                    cand = c * (1 - pos * cur["sl_frac"])
                    stp = max(stp, cand) if pos > 0 else min(stp, cand)
            if pos == 0 and pend is None and not closed_now and not leave:
                d = 1 if (buy and not prev_buy) else -1 if (sell and not prev_sell and not buy) else 0
                if d:
                    order = self._order(s, d, c)
                    entry_o[i] = True
                    if order["limit"] is None:
                        pos, held, cur, stp, tak = d, 0, order, order["stp"], order["tak"]
                    else:
                        pend = order
            prev_buy, prev_sell = buy, sell
            if pend is not None:                          # the order is working: tell the simulator about it
                target[i], usd_o[i], stop_o[i], take_o[i], limit_o[i] = pend["d"], pend["usd"], pend["stp"], pend["tak"], pend["limit"]
            elif pos != 0:
                target[i], usd_o[i], stop_o[i], take_o[i] = pos, cur["usd"], stp, tak
        return pd.DataFrame({"target": target, "usd": usd_o, "stop": stop_o, "take": take_o, "limit": limit_o,
                             "entry": entry_o}, index=ctx.index)
