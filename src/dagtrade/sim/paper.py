"""PaperBroker: the ONE set of fill rules, used by backtests and by the local live paper engine.

    * a market order fills at the OPEN of the first candle that closes after it was sent (+ slippage, fees)
    * a limit entry fills only if that candle trades through the price (at the open if it gapped through),
      otherwise it expires
    * an entry carries its take-profit / stop-loss as exit legs; each candle's high/low is checked against them
      (the stop wins if one candle touches both; a gap through a stop fills at the open)
    * no borrowing: an entry is cut to what the cash pays for (incl. fee), or rejected
Backtests use fractional shares; live (local or Alpaca) uses whole shares.
Everything lives in `self.s` (a JSON-able dict) so LocalBroker can persist it.
"""
from __future__ import annotations

import math

ACTIVE = {"accepted", "new"}


class PaperBroker:
    def __init__(self, cash: float = 10_000.0, commission_bps: float = 1.0, slippage_bps: float = 2.0,
                 whole_shares: bool = False, record_equity: bool = False, state: dict | None = None):
        self.comm, self.slip, self.whole = commission_bps / 1e4, slippage_bps / 1e4, whole_shares
        self.record_equity = record_equity
        self.s = {"start_cash": cash, "cash": cash, "qty": 0.0, "last_px": None, "last_avail": None,
                  "orders": [], "fills": [], "equity": [], "n": 0}
        if state:
            self.s.update(state)
        self._open = [o for o in self.s["orders"] if o["status"] in ACTIVE]

    # -- reads ---------------------------------------------------------------------------------------
    @property
    def cash(self) -> float:
        return self.s["cash"]

    @property
    def qty(self) -> float:
        return self.s["qty"]

    def equity(self, px: float | None = None) -> float:
        px = px if px is not None else (self.s["last_px"] or 0.0)
        return self.s["cash"] + self.s["qty"] * px

    def working(self) -> list[dict]:
        return list(self._open)

    def status(self, client_id: str) -> str | None:
        """Status of the order with this client id ('filled', 'accepted', 'expired', ...), newest first."""
        for o in reversed(self.s["orders"]):
            if o["client_id"] == client_id:
                return o["status"]
        return None

    # -- writes --------------------------------------------------------------------------------------
    def _new_id(self) -> str:
        self.s["n"] += 1
        return f"L{self.s['n']}"

    def _add(self, at: float, **o) -> dict:
        if any(x["client_id"] == o["client_id"] for x in self.s["orders"][-50:]):
            raise ValueError("client_order_id must be unique")
        o = {"id": self._new_id(), "status": "accepted", "submitted_at": at, **o}
        self.s["orders"].append(o)
        self._open.append(o)
        return o

    def submit_entry(self, side: str, qty: float, client_id: str, at: float, stop=None, take=None, limit=None,
                     why: str = "entry") -> dict:
        return self._add(at, client_id=client_id, side=side, qty=float(qty), role="entry",
                         type="limit" if limit is not None else "market", limit_price=limit, leg_stop=stop,
                         leg_take=take, why=why)

    def submit_market(self, side: str, qty: float, client_id: str, at: float, why: str = "close") -> dict:
        return self._add(at, client_id=client_id, side=side, qty=float(qty), role="close", type="market", why=why)

    def cancel(self, order_id: str) -> None:
        for o in self._open:
            if o["id"] == order_id:
                o["status"] = "canceled"
        self._prune()

    def replace_stop(self, order_id: str | None, stop: float) -> None:
        """Move the open position's stop leg (trailing stops). order_id None = every working stop."""
        for o in self._open:
            if o["role"] == "stop" and (order_id is None or o["id"] == order_id):
                o["stop_price"] = float(stop)

    def _prune(self):
        self._open = [o for o in self._open if o["status"] in ACTIVE]

    # -- the engine ----------------------------------------------------------------------------------
    def _fill(self, o: dict, price: float, at: float, reason: str):
        q = o["qty"] if o["side"] == "buy" else -o["qty"]
        fee = abs(q) * price * self.comm
        self.s["cash"] -= q * price + fee
        self.s["qty"] += q
        if abs(self.s["qty"]) < 1e-9:
            self.s["qty"] = 0.0
        o.update(status="filled", fill_price=price, filled_at=at, reason=reason)
        self.s["fills"].append({"time": at, "side": o["side"], "qty": o["qty"], "price": price, "fee": fee,
                                "role": o["role"], "reason": reason, "client_id": o["client_id"]})

    def _affordable(self, od: dict, px: float) -> bool:
        cash = max(0.0, self.s["cash"])
        afford = cash / (px * (1 + self.comm)) if px > 0 else 0.0
        if self.whole:
            afford = math.floor(afford)
        if afford * px < 0.01 or (self.whole and afford < 1):
            od.update(status="rejected", reason=f"not enough cash (${self.s['cash']:,.2f}) at ${px:,.2f}")
            return False
        if od["qty"] > afford:
            od.update(qty=float(afford), note=f"cut from {od['qty']:g} to {afford:g} shares: not enough cash")
        return True

    def _legs_for(self, entry: dict, at: float):
        side = "sell" if entry["side"] == "buy" else "buy"
        base = entry["client_id"]
        for role, typ, key, px in (("stop", "stop", "stop_price", entry.get("leg_stop")),
                                   ("take", "limit", "limit_price", entry.get("leg_take"))):
            if px is not None:
                self._add(at, client_id=f"{base}.{role}", side=side, qty=entry["qty"], role=role, type=typ,
                          **{key: px}, parent=entry["id"])

    def process(self, t_open: float, avail: float, o: float, h: float, l: float, c: float) -> None:
        """One closed candle: fill what was sent before it closed, then check exit legs against its range."""
        if self.s["last_avail"] is not None and avail <= self.s["last_avail"]:
            return
        live = lambda roles: [x for x in self._open if x["role"] in roles and x["submitted_at"] < avail]  # noqa: E731
        for od in live(("close", "entry")):
            if od["type"] == "market":
                px, how = (o * (1 + self.slip) if od["side"] == "buy" else o * (1 - self.slip)), "market"
            else:
                L = od["limit_price"]
                if (od["side"] == "buy" and l <= L) or (od["side"] == "sell" and h >= L):
                    px, how = (min(o, L) if od["side"] == "buy" else max(o, L)), "limit"
                else:
                    od.update(status="expired")
                    continue
            if od["role"] == "entry" and not self._affordable(od, px):
                continue
            if od["role"] == "close":                     # never close more than we hold (or flip the position)
                held = abs(self.s["qty"])
                if held < 1e-9:
                    od.update(status="canceled", reason="nothing to close")
                    continue
                od["qty"] = min(od["qty"], held)
            self._fill(od, px, t_open, how if od["role"] == "entry" else (od.get("why") or how))
            if od["role"] == "entry":
                self._legs_for(od, avail - 1e-6)
        self._prune()
        legs = live(("stop", "take"))
        if legs and abs(self.s["qty"]) > 1e-9:
            long_ = self.s["qty"] > 0
            stop = next((x for x in legs if x["role"] == "stop"), None)
            take = next((x for x in legs if x["role"] == "take"), None)
            hit = None
            if stop and ((long_ and l <= stop["stop_price"]) or (not long_ and h >= stop["stop_price"])):
                sp = stop["stop_price"]
                hit = (stop, min(o, sp) * (1 - self.slip) if long_ else max(o, sp) * (1 + self.slip), "stop")
            elif take and ((long_ and h >= take["limit_price"]) or (not long_ and l <= take["limit_price"])):
                tp = take["limit_price"]
                hit = (take, max(o, tp) if long_ else min(o, tp), "take")
            if hit:
                hit[0]["qty"] = abs(self.s["qty"])
                self._fill(hit[0], hit[1], t_open, hit[2])
        if abs(self.s["qty"]) < 1e-9:                     # flat: leftover exit legs are void (one-cancels-other)
            for x in self._open:
                if x["role"] in ("stop", "take") and x["status"] in ACTIVE:
                    x["status"] = "canceled"
        self._prune()
        self.s["last_px"], self.s["last_avail"] = c, avail
        if self.record_equity:
            self.s["equity"].append([avail, round(self.equity(), 2)])

    def flatten(self, at: float, reason: str = "end", px: float | None = None) -> None:
        """Cancel everything and close the position at `px` (default: the last price), e.g. end of a backtest."""
        for x in self._open:
            x["status"] = "canceled"
        self._prune()
        q, px = self.s["qty"], px if px is not None else self.s["last_px"]
        if abs(q) > 1e-9 and px:
            o = {"id": self._new_id(), "client_id": f"flat.{self.s['n']}", "side": "sell" if q > 0 else "buy",
                 "qty": abs(q), "role": "close", "type": "market", "status": "accepted", "submitted_at": at}
            self.s["orders"].append(o)
            self._fill(o, px * (1 - self.slip) if q > 0 else px * (1 + self.slip), at, reason)
