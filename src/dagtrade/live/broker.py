"""Broker adapters. The runner only ever talks to the `Broker` interface.

AlpacaBroker   - Alpaca PAPER account via alpaca-py. Live (real money) endpoints are deliberately not reachable.
DryRunBroker   - reads real account/positions/orders from Alpaca but only *logs* what it would have sent.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Protocol

from .models import BAccount, BClock, BOrder

log = logging.getLogger("dagtrade.live")


class Broker(Protocol):
    name: str

    def account(self) -> BAccount: ...
    def clock(self) -> BClock: ...
    def positions(self) -> dict[str, float]: ...                      # symbol -> signed qty
    def open_orders(self, symbol: str | None = None) -> list[BOrder]: ...
    def recent_orders(self, symbol: str, since: datetime) -> list[BOrder]: ...   # any status, incl. filled
    def submit_entry(self, symbol: str, side: str, qty: int, client_id: str, stop: float | None = None,
                     take: float | None = None, limit: float | None = None) -> BOrder: ...   # carries its own exits
    def submit_market(self, symbol: str, side: str, qty: int, client_id: str) -> BOrder: ...
    def cancel(self, order_id: str) -> None: ...


def _f(x) -> float | None:
    return None if x is None else float(x)


def _val(x):
    return getattr(x, "value", x)       # enums -> their string value


def to_border(o) -> BOrder:
    return BOrder(
        id=str(o.id), client_id=o.client_order_id or "", symbol=o.symbol, side=str(_val(o.side)).lower(),
        qty=_f(o.qty) or 0.0, status=str(_val(o.status)).lower(),
        type=str(_val(getattr(o, "order_type", None) or getattr(o, "type", None) or "market")).lower(),
        order_class=(str(_val(o.order_class)).lower() if getattr(o, "order_class", None) else None),
        stop_price=_f(getattr(o, "stop_price", None)), limit_price=_f(getattr(o, "limit_price", None)),
        filled_qty=_f(getattr(o, "filled_qty", None)) or 0.0, filled_avg_price=_f(getattr(o, "filled_avg_price", None)),
        submitted_at=getattr(o, "submitted_at", None), filled_at=getattr(o, "filled_at", None),
        legs=[to_border(leg) for leg in (getattr(o, "legs", None) or [])])


class AlpacaBroker:
    """Alpaca paper trading. Needs ALPACA_KEY / ALPACA_SECRET from a *paper* account."""
    name = "alpaca-paper"

    def __init__(self, key: str | None = None, secret: str | None = None, client=None):
        if client is None:
            from alpaca.trading.client import TradingClient
            key, secret = key or os.getenv("ALPACA_KEY"), secret or os.getenv("ALPACA_SECRET")
            if not (key and secret):
                raise RuntimeError("ALPACA_KEY / ALPACA_SECRET not set (use your PAPER account keys; see .env.example)")
            client = TradingClient(key, secret, paper=True)       # paper=True is hard-wired on purpose
        self.client = client

    # -- reads ----------------------------------------------------------------------------------
    def account(self) -> BAccount:
        a = self.client.get_account()
        return BAccount(equity=float(a.equity), cash=float(a.cash), buying_power=float(a.buying_power),
                        status=str(_val(a.status)), trading_blocked=bool(getattr(a, "trading_blocked", False)
                                                                          or getattr(a, "account_blocked", False)))

    def clock(self) -> BClock:
        c = self.client.get_clock()
        return BClock(now=c.timestamp, is_open=bool(c.is_open), next_open=c.next_open, next_close=c.next_close)

    def positions(self) -> dict[str, float]:
        out = {}
        for p in self.client.get_all_positions():
            q = float(p.qty)
            out[p.symbol] = -abs(q) if str(_val(p.side)).lower() == "short" else abs(q)
        return out

    def open_orders(self, symbol: str | None = None) -> list[BOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500,
                               **({"symbols": [symbol]} if symbol else {}))
        return [to_border(o) for o in self.client.get_orders(req)]

    def recent_orders(self, symbol: str, since: datetime) -> list[BOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], after=since, limit=500, nested=True)
        return [to_border(o) for o in self.client.get_orders(req)]

    # -- writes ---------------------------------------------------------------------------------
    def _submit(self, req) -> BOrder:
        return to_border(self.client.submit_order(req))

    def submit_market(self, symbol, side, qty, client_id):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        return self._submit(MarketOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide(side),
                                               time_in_force=TimeInForce.DAY, client_order_id=client_id))

    def submit_entry(self, symbol, side, qty, client_id, stop=None, take=None, limit=None):
        """Entry order (market, or limit when `limit` is given) that carries its own exits: both stop and take ->
        bracket; one of them -> OTO; neither -> plain order. Exit legs are GTC so they outlive the day."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest
        kw: dict = {}
        if stop is not None:
            kw["stop_loss"] = StopLossRequest(stop_price=round(stop, 2))
        if take is not None:
            kw["take_profit"] = TakeProfitRequest(limit_price=round(take, 2))
        if kw:
            kw["order_class"] = OrderClass.BRACKET if len(kw) == 2 else OrderClass.OTO
        base = dict(symbol=symbol, qty=int(qty), side=OrderSide(side), client_order_id=client_id,
                    time_in_force=TimeInForce.GTC if kw else TimeInForce.DAY, **kw)
        if limit is not None:
            return self._submit(LimitOrderRequest(limit_price=round(limit, 2), **base))
        return self._submit(MarketOrderRequest(**base))

    def cancel(self, order_id: str) -> None:
        self.client.cancel_order_by_id(order_id)


class DryRunBroker:
    """Wraps a real (read-only) broker: reads pass through, writes are logged and pretend to succeed."""

    def __init__(self, inner: Broker):
        self.inner, self.name = inner, inner.name + "+dry-run"
        self.sent: list[tuple] = []

    def __getattr__(self, item):           # account / clock / positions / open_orders / recent_orders
        return getattr(self.inner, item)

    def _fake(self, kind, symbol, side, qty, client_id, **kw) -> BOrder:
        self.sent.append((kind, symbol, side, qty, kw))
        log.info("[dry-run] would send %s %s %s x%s %s", kind, side, symbol, qty, kw or "")
        return BOrder(id=f"dry-{len(self.sent)}", client_id=client_id, symbol=symbol, side=side, qty=qty,
                      status="accepted", type=kind, submitted_at=datetime.now(timezone.utc))

    def submit_market(self, symbol, side, qty, client_id):
        return self._fake("market", symbol, side, qty, client_id)

    def submit_entry(self, symbol, side, qty, client_id, stop=None, take=None, limit=None):
        return self._fake("entry", symbol, side, qty, client_id, stop=stop, take=take, limit=limit)

    def cancel(self, order_id: str) -> None:
        log.info("[dry-run] would cancel order %s", order_id)
