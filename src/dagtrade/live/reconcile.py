"""Turn the strategy's latest decision into broker actions. PURE function: no network, no clock.

Only two things ever happen:
  * ENTRY - the Trade node placed a fresh order on the candle that just closed and we are flat:
            send ONE order (market, or limit if the signal gave a price) that carries its own stop-loss / take-profit
            (Alpaca bracket order).
  * EXIT  - the strategy wants out (exit signal / max_hold) while we hold a position:
            cancel the exit legs, then close with a market order.
Take-profit and stop-loss fills are handled by Alpaca itself; when they fill we are simply flat again.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import BOrder, slug

MAX_DECISION_AGE = 72 * 3600          # never open a position on a decision older than this (restart after long outage)


@dataclass
class Decision:
    avail: float                      # epoch seconds: when the deciding candle closed
    price: float                      # that candle's close
    target: float                     # +1 long / -1 short / 0 flat: what the Trade node wants to be holding
    usd: float = 1000.0
    stop: float | None = None
    take: float | None = None
    limit: float | None = None       # limit entry price (None = market order)
    entry: bool = False               # True on the candle where a NEW order should be placed
    nodes: dict = field(default_factory=dict)


@dataclass
class DepState:
    last_avail: float | None = None   # avail time of the last candle we processed
    decision: dict | None = None
    pending: float | None = None      # avail of an entry that has not been sent successfully yet
    last_action: dict | None = None
    last_error: str | None = None
    retry_not_before: float = 0.0


@dataclass
class Action:
    kind: str                         # entry | close | cancel
    side: str | None = None
    qty: int = 0
    stop: float | None = None
    take: float | None = None
    limit: float | None = None
    client_id: str = ""
    order_id: str | None = None
    reason: str = ""

    def __str__(self):
        bits = [self.kind, self.side or "", str(self.qty or ""), f"limit {self.limit:.2f}" if self.limit else "", f"stop {self.stop:.2f}" if self.stop else "",
                f"take {self.take:.2f}" if self.take else "", f"({self.reason})" if self.reason else ""]
        return " ".join(b for b in bits if b)


def plan(dec: Decision, pos_qty: float, orders: list[BOrder], label: str, now: float, act_on_entry: bool,
         paused: bool = False) -> tuple[list[Action], list[str]]:
    pre = f"dt.{slug(label)}"
    q = int(round(pos_qty))
    active = [o for o in orders if o.active]          # we own this symbol: every working order is ours
    if paused:
        return [], ["paused: no orders sent"]

    if q != 0 and dec.target == 0:                    # the strategy wants out
        if active:                                    # free the shares held by the exit legs first
            return [Action("cancel", order_id=o.id, reason="exit") for o in active], []
        return [Action("close", "sell" if q > 0 else "buy", abs(q), client_id=f"{pre}.cls.{int(dec.avail)}",
                       reason="exit signal")], []

    if dec.entry and act_on_entry and q == 0:
        if active:
            return [], ["entry skipped: an order is already working"]
        if now - dec.avail > MAX_DECISION_AGE:
            return [], ["entry skipped: the decision is too old"]
        ref = dec.limit or dec.price
        qty = int(dec.usd // ref) if ref > 0 else 0
        if qty < 1:
            return [], [f"entry skipped: ${dec.usd:.0f} buys less than one share at ${ref:.2f}"]
        return [Action("entry", "buy" if dec.target > 0 else "sell", qty, stop=dec.stop, take=dec.take, limit=dec.limit,
                       client_id=f"{pre}.ent.{int(dec.avail)}", reason="new signal")], []
    return [], []
