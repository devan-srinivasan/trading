"""The building blocks of a strategy: states of a finite-state machine (FSA).

    Node   one state. While it is the active state, `run(candles, ctx)` is called every time a candle of its interval
           closes. `candles` is the window described by its `history` Range. It answers:
               None            not yet: stay here, ask again next candle
               True or a dict  success: move on along its "ok" edge (the dict is passed to every later state)
               False           failure: follow its "fail" edge (default: back to the start)
               Go("name", ...) a named outcome, for branching (edges={node: {"name": other_node}})
           A node that keeps answering None for longer than `max_wait` fails with the outcome "max_wait".
    Trade  the state that trades: places the order described by the state before it (with take-profit and
           stop-loss), holds the position until it is closed, then follows its "ok" edge (default: the start).

        class Uptrend(Node):
            history = Range(candles=200)                  # the last 200 candles of whatever interval the FSA runs on
            def run(self, candles, ctx):
                return candles.close.iloc[-1] > candles.close.mean() or None

        class Dip(Node):
            history = Range(start="-10d", interval="h")   # always hourly candles
            def run(self, candles, ctx):
                if candles.close.iloc[-1] < 0.97 * candles.close.max():
                    return {"buy": True}

        strategy = FSA.chain(Uptrend(), Dip(), Trade(take_profit=0.10, stop_loss=0.05))

ctx gives a node: ctx["uptrend"] (outputs of earlier states), ctx.inputs (starting cash and the [inputs] table of the
TOML), ctx.account (cash, equity, position: read-only), ctx.now, ctx.entered_at, ctx.waited.
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..range import Range

OHLCV = ["open", "high", "low", "close", "volume"]
DEFAULT_MAX_WAIT = 20            # candles of the node's own interval (the start node waits forever)


class _Default:
    def __repr__(self):
        return "default"


DEFAULT = _Default()


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _module_fingerprint(cls: type) -> str:
    mod = sys.modules.get(cls.__module__)
    path = getattr(mod, "__file__", None)
    try:
        return hashlib.sha1(open(path, "rb").read()).hexdigest()[:12] if path else cls.__qualname__
    except OSError:
        return cls.__qualname__


class Go:
    """A named outcome: `return Go("sell", gap_top=101.2)` follows the edge called "sell"."""

    def __init__(self, outcome: str, **data):
        self.outcome, self.data = outcome, data

    def __repr__(self):
        return f"Go({self.outcome!r}, {self.data})"


@dataclass
class Account:
    """Read-only view of the account a node may look at (sizing, risk rules)."""
    cash: float = 0.0
    equity: float = 0.0
    position: float = 0.0                     # signed shares of this strategy's symbol
    last_price: float | None = None
    order_status: Any = None                  # callable(token) -> 'filled' | 'accepted' | 'expired' | ... | None

    def status(self, token: str) -> str | None:
        return self.order_status(token) if self.order_status else None


@dataclass
class Intent:
    """What the Trade state wants done. The planner turns it into broker orders (sized from the account)."""
    kind: str                                 # entry | close | cancel_entry | stop
    token: str = ""                           # unique per order: client ids are dt.<dag>.<kind>.<token>
    side: str | None = None                   # buy | sell
    usd: float | None = None
    pct: float | None = None
    price: float | None = None                # reference price for sizing (the signal candle's close)
    stop: float | None = None
    take: float | None = None
    limit: float | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Ctx(Mapping):
    """What a node sees besides its candles. Indexing it reads earlier states' outputs: ctx["opening_range"]."""

    def __init__(self, now, state, memory, inputs, account, entered_at, waited, last_entry=None,
                 minutes_to_close=None):
        self.now, self.state, self.memory, self.inputs, self.account = now, state, memory, inputs, account
        self.entered_at, self.waited, self.last_entry = entered_at, waited, last_entry
        self.minutes_to_close = minutes_to_close

    def __getitem__(self, k):
        return self.memory[k]

    def __iter__(self):
        return iter(self.memory)

    def __len__(self):
        return len(self.memory)


class Node:
    """One state. Subclass it and implement `run(candles, ctx)`."""

    kind = "node"
    plot = "auto"                                   # "auto" | "hidden" (dashboard)
    history: Range = Range(candles=60)
    max_wait: Any = DEFAULT                         # candles (int), a duration ("2h", "3d"), or None = forever
    needs: tuple = ()                               # earlier states whose output run() reads (checked on a mid-start)

    def __init__(self, *args, history: Range | None = None, name: str | None = None, max_wait: Any = DEFAULT,
                 **params: Any):
        if args:
            raise TypeError(f"{type(self).__name__}: states are connected by the FSA's edges, not by passing nodes "
                            f"into each other (see FSA.chain / FSA(start=..., edges=...))")
        self.name = name or _snake(type(self).__name__)
        if history is not None:
            self.history = history
        if max_wait is not DEFAULT:
            self.max_wait = max_wait
        _check_wait(self.max_wait, self.name)
        self.params = params
        for k, v in params.items():                 # Dip(drop=0.03) -> self.drop
            if hasattr(self, k):
                raise ValueError(f"{type(self).__name__}: parameter {k!r} clashes with an existing attribute")
            setattr(self, k, v)

    @property
    def tf(self) -> str | None:
        return self.history.tf

    def run(self, candles: pd.DataFrame, ctx: Ctx) -> Any:
        raise NotImplementedError(f"{type(self).__name__} must implement run(candles, ctx)")

    def spec(self) -> str:
        return "|".join([type(self).__qualname__, _module_fingerprint(type(self)), repr(sorted(self.params.items())),
                         repr(self.history), repr(self.max_wait)])

    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"


def _check_wait(w, name):
    if w is DEFAULT or w is None:
        return
    if isinstance(w, bool) or not isinstance(w, (int, str)):
        raise ValueError(f"{name}: max_wait is a number of candles, a duration like '2h' / '3d', or None")
    if isinstance(w, int) and w < 1:
        raise ValueError(f"{name}: max_wait must be >= 1 candle")
    if isinstance(w, str):
        wait_delta(w)


def wait_delta(w: str) -> pd.Timedelta:
    m = re.match(r"^(\d+)\s*([mhdw])$", w.strip().lower())
    if not m:
        raise ValueError(f"max_wait {w!r}: use a number of candles or a duration like '30m', '2h', '3d', '1w'")
    unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
    return pd.Timedelta(**{unit: int(m.group(1))})


def interpret(res) -> tuple[str | None, dict]:
    """A node's answer -> (outcome, data). outcome None = keep waiting."""
    if res is None:
        return None, {}
    if isinstance(res, Go):
        return res.outcome, dict(res.data)
    if isinstance(res, (bool, np.bool_)):
        return ("ok" if res else "fail"), {}
    if isinstance(res, dict):
        return "ok", dict(res)
    raise TypeError(f"a node must return None, True/False, a dict or Go(...), got {type(res).__name__}")


def _check_pct(p: float | None) -> float | None:
    if p is not None and not 0 < p <= 1:
        raise ValueError(f"size / pct is a fraction of the account between 0 and 1 (no borrowing), got {p}")
    return p


class Trade(Node):
    """The state that places orders. Take-profit and stop-loss are REQUIRED; everything else has a default.

        Trade(take_profit=0.10, stop_loss=0.05)                       # $1000, market entry
        Trade(take_profit=0.10, stop_loss=0.05, size=0.75)            # 75% of the account: reinvests profits
        Trade(take_profit=1.0, stop_loss=0.10, exit=DeathCross())     # also close when DeathCross says so

    It trades what the state before it returned (a dict; all keys optional):
        "buy": True | "sell": True      long (default) or short
        "price": 101.5                  limit entry: only fills if the next candle trades through it
        "usd": 750 | "qty": 5 | "pct": 0.5     order size (default: the Trade's `size` or `notional`)
        "take_profit": 0.08 / "stop_loss": 0.03          fractions of the entry price, this order only
        "take_profit_price": 112 / "stop_loss_price": 96  absolute prices, this order only
        "trail": True, "max_hold": 10                     this order only
    Fractions are measured from the signal candle's close (market) or your limit price. The order fills at the next
    candle's open. While holding, the position closes on take-profit / stop-loss (at the broker), when `exit` returns
    True, after `max_hold` candles, or before the close on a flat-by-close run. Then the Trade follows "ok". If the
    entry does not fill (limit not reached, not enough cash, already in a position) it follows "fail".
    """
    kind = "trade"
    plot = "hidden"
    max_wait = None
    history = Range(candles=1)

    def __init__(self, take_profit: float, stop_loss: float, notional: float = 1000.0, *, size: float | None = None,
                 trail: bool = False, max_hold: int | None = None, exit: Node | None = None, name: str = "trade"):
        if take_profit is None or stop_loss is None:
            raise ValueError("Trade needs both take_profit and stop_loss (fractions of the entry price, e.g. 0.10 / 0.05)")
        if not take_profit > 0 or not 0 < stop_loss < 1:
            raise ValueError("take_profit must be > 0 and stop_loss between 0 and 1 (fractions, not prices)")
        if exit is not None and not isinstance(exit, Node):
            raise TypeError("Trade(exit=...) takes a Node whose run() returns True to close the position")
        _check_pct(size)
        super().__init__(name=name, take_profit=float(take_profit), stop_loss=float(stop_loss),
                         notional=float(notional), size=None if size is None else float(size), trail=trail,
                         max_hold=max_hold)
        self.exit = exit

    def order(self, s: dict, close: float) -> dict:
        """One signal dict -> a concrete order (direction, limit, usd/pct, stop/take prices, trail, max_hold)."""
        d = -1 if (s.get("sell") and not s.get("buy")) or s.get("side") == "sell" else 1
        limit = float(s["price"]) if s.get("price") is not None else None
        ref = limit if limit is not None else close
        pct = None
        if s.get("usd") is not None:
            usd = float(s["usd"])
        elif s.get("qty") is not None:
            usd = float(s["qty"]) * ref
        else:
            usd = self.notional
            p = s.get("pct", self.size)
            if p is not None:
                pct = _check_pct(float(p))
        tp = float(s["take_profit"]) if s.get("take_profit") is not None else self.take_profit
        sl = float(s["stop_loss"]) if s.get("stop_loss") is not None else self.stop_loss
        tak = float(s["take_profit_price"]) if s.get("take_profit_price") is not None else ref * (1 + d * tp)
        stp = float(s["stop_loss_price"]) if s.get("stop_loss_price") is not None else ref * (1 - d * sl)
        ok = stp < ref < tak if d > 0 else tak < ref < stp
        if not ok:
            raise ValueError(f"{self.name}: a {'long' if d > 0 else 'short'} at {ref:.2f} needs "
                             f"{'stop < price < take' if d > 0 else 'take < price < stop'}; got stop {stp:.2f}, take {tak:.2f}")
        return dict(d=d, side="buy" if d > 0 else "sell", limit=limit, usd=usd, pct=pct, stop=stp, take=tak, ref=ref,
                    trail=bool(s.get("trail", self.trail)), max_hold=s.get("max_hold", self.max_hold),
                    sl_frac=abs(ref - stp) / ref)

    def describe_params(self) -> dict:
        out = {k: v for k, v in self.params.items() if isinstance(v, (int, float, str, bool, type(None)))}
        if self.exit is not None:
            out["exit"] = self.exit.name
        return out


@dataclass
class TradeState:
    """The Trade state's progress, kept in the runner's (persisted) state."""
    phase: str = "enter"                      # enter | pending | holding | closing
    token: str = ""
    side: str = "buy"
    limit: float | None = None
    stop: float | None = None
    take: float | None = None
    sl_frac: float = 0.0
    trail: bool = False
    max_hold: int | None = None
    held: int = 0
    entry_time: float | None = None
    exit_idx: int | None = None
    notes: list = field(default_factory=list)
