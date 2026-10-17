"""Turn the Trade state's intents into broker actions. PURE function: no network, no clock.
Used by the live scheduler (whole shares) and by the backtest engine (fractional shares), so both size and cap
orders the same way.

    entry         flat and nothing working: ONE order (market, or limit) carrying its own stop-loss / take-profit
                  (an Alpaca bracket order). Size: the intent's dollars, or `pct` of the account's equity; never more
                  than the account's CASH pays for (no margin), keeping a small buffer for the next open.
    close         cancel the exit legs first, then close the position with a market order
    cancel_entry  cancel a limit entry that did not fill
    stop          move the stop-loss leg (trailing stop)
`plan` returns (actions, notes, keep): `keep` = intents still outstanding (a close waits for its legs to cancel and
for the position to be flat); everything else is done or dropped after this call.
"""
from __future__ import annotations

from dataclasses import dataclass

from .node import Intent


def slug(label: str) -> str:
    """strategy/TICKER -> a client-order-id-safe token."""
    return "".join(ch if ch.isalnum() else "-" for ch in label)


MAX_DECISION_AGE = 72 * 3600          # never open a position on a decision older than this (restart after long outage)
CASH_BUFFER = 0.01                    # whole shares: keep 1% of cash spare, a market order fills at the NEXT open


@dataclass
class Action:
    kind: str                         # entry | close | cancel | replace_stop
    side: str | None = None
    qty: float = 0
    stop: float | None = None
    take: float | None = None
    limit: float | None = None
    client_id: str = ""
    order_id: str | None = None
    reason: str = ""
    token: str = ""

    def __str__(self):
        bits = [self.kind, self.side or "", f"{self.qty:g}" if self.qty else "",
                f"limit {self.limit:.2f}" if self.limit else "", f"stop {self.stop:.2f}" if self.stop else "",
                f"take {self.take:.2f}" if self.take else "", f"({self.reason})" if self.reason else ""]
        return " ".join(b for b in bits if b)


def _size(it: Intent, equity, cash, whole: bool) -> tuple[float, str | None]:
    ref = it.limit or it.price
    if not ref or ref <= 0:
        return 0, "entry skipped: no price"
    if it.pct:
        if equity is None:
            return 0, "entry skipped: % sizing needs the account's equity"
        usd = it.pct * equity
    else:
        usd = it.usd or 0.0
    qty = usd / ref
    if cash is not None:
        afford = max(0.0, cash) * ((1 - CASH_BUFFER) if whole else 1.0) / ref
        if (afford < 1 if whole else afford * ref < 0.01):
            return 0, f"entry skipped: not enough cash (${cash:,.2f}) for one share at ${ref:.2f}"
        qty = min(qty, afford)
    if whole:
        qty = int(qty)
        if qty < 1:
            return 0, f"entry skipped: ${usd:,.0f} buys less than one share at ${ref:.2f}"
    elif qty * ref < 0.01:
        return 0, "entry skipped: order too small"
    return qty, None


def plan(intents: list[Intent], pos_qty: float, orders: list, label: str, now: float, paused: bool = False,
         equity: float | None = None, cash: float | None = None, whole_shares: bool = True
         ) -> tuple[list[Action], list[str], list[Intent]]:
    pre = f"dt.{slug(label)}"
    q = pos_qty if not whole_shares else int(round(pos_qty))
    active = [o for o in orders if o.active]           # we own this symbol: every working order is ours
    acts, notes, keep = [], [], []
    if paused:
        return [], ["paused: no orders sent"], list(intents)
    for it in intents:
        if it.kind == "close":
            if abs(q) < 1e-9:
                continue                                 # already flat: done
            if any(o.kind == "cls" for o in active):
                keep.append(it)                          # our close is on its way
                continue
            legs = [o for o in active if o.kind != "cls"]
            if legs:                                     # free the shares held by the exit legs first
                acts += [Action("cancel", order_id=o.id, reason=it.reason or "exit", token=it.token) for o in legs]
            else:
                acts.append(Action("close", "sell" if q > 0 else "buy", abs(q), client_id=f"{pre}.cls.{it.token}",
                                   reason=it.reason or "exit", token=it.token))
            keep.append(it)
        elif it.kind == "entry":
            if abs(q) > 1e-9:
                notes.append("entry skipped: already in a position")
                continue
            if active:
                notes.append("entry skipped: an order is already working")
                continue
            if it.token.isdigit() and now - int(it.token) > MAX_DECISION_AGE:
                notes.append("entry skipped: the decision is too old")
                continue
            qty, why = _size(it, equity, cash, whole_shares)
            if why:
                notes.append(why)
                continue
            acts.append(Action("entry", it.side, qty, stop=it.stop, take=it.take, limit=it.limit,
                               client_id=f"{pre}.ent.{it.token}", reason="new signal", token=it.token))
        elif it.kind == "cancel_entry":
            if abs(q) < 1e-9:
                acts += [Action("cancel", order_id=o.id, reason=it.reason or "entry expired", token=it.token)
                         for o in active]
        elif it.kind == "stop":
            stops = [o for o in active if o.type == "stop"]
            acts += [Action("replace_stop", stop=it.stop, order_id=o.id, reason=it.reason or "trail", token=it.token)
                     for o in stops]
    return acts, notes, keep
