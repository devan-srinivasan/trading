"""Plain data types shared by the live runner and broker adapters (no Alpaca imports here)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

ACTIVE_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_replace", "pending_cancel",
                   "held", "accepted_for_bidding", "calculated"}


@dataclass
class BOrder:
    id: str
    client_id: str
    symbol: str
    side: str                       # "buy" | "sell"
    qty: float
    status: str
    type: str = "market"
    order_class: str | None = None
    stop_price: float | None = None
    limit_price: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    legs: list["BOrder"] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def kind(self) -> str:
        """ent / cls / stp / tp / oco for orders we created (client id `dt.<slug>.<kind>.<token>`), else 'foreign'."""
        parts = self.client_id.split(".")
        return parts[2] if len(parts) >= 4 and parts[0] == "dt" else "foreign"


@dataclass
class BAccount:
    equity: float
    cash: float
    buying_power: float
    status: str = "ACTIVE"
    trading_blocked: bool = False


@dataclass
class BClock:
    now: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


from ..fsa.planner import slug  # noqa: E402,F401  (one definition, shared with the backtest engine)
