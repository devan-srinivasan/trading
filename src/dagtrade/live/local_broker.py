"""Local paper trading: a broker that lives in this process, uses LIVE candles, and never sends anything to Alpaca.

It is the backtest's own PaperBroker (sim/paper.py: the same fill rules, so live results are comparable with
backtests) plus persistence and the live Broker interface. Whole shares, like a real account.
Each live DAG gets its own account (default $10,000 cash), saved to live/local/<dag>.json so it survives restarts.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..sim.paper import ACTIVE, PaperBroker
from .models import BAccount, BClock, BOrder, slug


def _dt(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc)


class LocalBroker(PaperBroker):
    name = "local-paper"
    EQUITY_POINTS = 5000

    def __init__(self, key: str, symbol: str, root: Path, clock, cash: float = 10_000.0,
                 commission_bps: float = 1.0, slippage_bps: float = 2.0, now_fn=time.time):
        self.key, self.symbol, self.clock_src, self.now_fn = key, symbol.upper(), clock, now_fn
        self.path = Path(root) / "local" / f"{slug(key)}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        saved = None
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
            except Exception:  # noqa: BLE001
                saved = None
        super().__init__(cash, commission_bps, slippage_bps, whole_shares=True, record_equity=True, state=saved)
        self._save()

    # -- persistence ---------------------------------------------------------------------------------
    def _save(self):
        self.s["equity"] = self.s["equity"][-self.EQUITY_POINTS:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, default=str))
        tmp.replace(self.path)

    # -- reads (Broker interface) --------------------------------------------------------------------
    def clock(self) -> BClock:
        return self.clock_src()

    def account(self) -> BAccount:
        return BAccount(equity=self.equity(), cash=self.s["cash"], buying_power=max(0.0, self.s["cash"]))

    def positions(self) -> dict[str, float]:
        return {self.symbol: self.s["qty"]} if abs(self.s["qty"]) > 1e-9 else {}

    def _border(self, o: dict) -> BOrder:
        return BOrder(id=o["id"], client_id=o["client_id"], symbol=self.symbol, side=o["side"], qty=o["qty"],
                      status=o["status"], type=o["type"], stop_price=o.get("stop_price"),
                      limit_price=o.get("limit_price"), filled_qty=o["qty"] if o["status"] == "filled" else 0.0,
                      filled_avg_price=o.get("fill_price"), submitted_at=_dt(o["submitted_at"]),
                      filled_at=_dt(o.get("filled_at")))

    def open_orders(self, symbol: str | None = None) -> list[BOrder]:
        return [self._border(o) for o in self.s["orders"] if o["status"] in ACTIVE]

    def recent_orders(self, symbol: str, since: datetime) -> list[BOrder]:
        t = since.timestamp()
        return [self._border(o) for o in self.s["orders"] if o["submitted_at"] >= t]

    # -- writes (Broker interface) -------------------------------------------------------------------
    def submit_entry(self, symbol, side, qty, client_id, stop=None, take=None, limit=None):
        o = super().submit_entry(side, qty, client_id, self.now_fn(), stop=stop, take=take, limit=limit)
        self._save()
        return self._border(o)

    def submit_market(self, symbol, side, qty, client_id, why: str = "close"):
        o = super().submit_market(side, qty, client_id, self.now_fn(), why=why)
        self._save()
        return self._border(o)

    def cancel(self, order_id: str) -> None:
        super().cancel(order_id)
        self._save()

    def replace_stop(self, order_id: str | None, stop: float) -> None:
        super().replace_stop(order_id, stop)
        self._save()

    # -- the engine ----------------------------------------------------------------------------------
    def on_candles(self, bars: pd.DataFrame) -> int:
        """Feed closed candles (open-time index, OHLC + `avail`). Processes each new one once; returns how many."""
        done = 0
        if self.s["last_avail"] is None and len(bars):     # brand-new account: start from the latest candle
            bars = bars.iloc[-1:]
        for t_open, c in bars.iterrows():
            avail = pd.Timestamp(c["avail"]).timestamp()
            if self.s["last_avail"] is not None and avail <= self.s["last_avail"]:
                continue
            self.process(pd.Timestamp(t_open).timestamp(), avail, float(c["open"]), float(c["high"]),
                         float(c["low"]), float(c["close"]))
            done += 1
        if done:
            self._save()
        return done

    def flatten_now(self, reason: str = "stopped") -> None:
        """Cancel everything and close the position at the last known price (used on stop / move to Alpaca)."""
        self.flatten(self.now_fn(), reason)
        self._save()

    def summary(self) -> dict:
        """Account numbers for the dashboard."""
        from .pnl import position_pnl
        p = position_pnl(self.s["fills"], self.s["last_px"])
        return {"start_cash": self.s["start_cash"], "cash": round(self.s["cash"], 2), "equity": round(self.equity(), 2),
                "qty": self.s["qty"], "last_price": self.s["last_px"], **p,
                "return": self.equity() / self.s["start_cash"] - 1 if self.s["start_cash"] else 0.0}
