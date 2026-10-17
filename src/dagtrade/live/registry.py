"""Which DAGs are live: live/active.json, edited by the dashboard or the CLI and re-read by the scanner every cycle.

    {"id": "stochastic/SPY@5m", "strategy": "stochastic", "ticker": "SPY", "interval": "5m",
     "flat_by_close": true | false | null (null = the ticker's TOML decides),
     "start_at": state or null, "inputs": {...} (extra ctx.inputs),
     "restart": {"at": state, "memory": {...}, "nonce": "..."} (a request the scanner applies once),
     "mode": "local" | "alpaca", "status": "running" | "stopped", "cash": 10000, "added": "..."}
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import strategies_root
from .models import BClock

MODES, STATUSES = ("local", "alpaca"), ("running", "stopped")
_lock = threading.Lock()


def dag_id(strategy: str, ticker: str, interval: str | None) -> str:
    return f"{strategy}/{ticker.upper()}" + (f"@{interval}" if interval else "")


class Registry:
    def __init__(self, root: Path, project: Path | None = None):
        self.path = Path(root) / "active.json"
        self.project = project                     # where strategies/ lives (default: this repo)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            return []

    def _save(self, items: list[dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        tmp.replace(self.path)

    def get(self, did: str) -> dict | None:
        return next((x for x in self.load() if x["id"] == did), None)

    def add(self, strategy: str, ticker: str, interval: str | None = None, mode: str = "local",
            cash: float = 10_000.0, flat_by_close: bool | None = None, start_at: str | None = None,
            inputs: dict | None = None) -> dict:
        if not (strategies_root(self.project) / strategy / "strategy.py").exists():
            raise ValueError(f"unknown strategy {strategy!r}")
        if not ticker.strip():
            raise ValueError("enter a ticker")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self._check_builds(strategy, ticker, interval, flat_by_close, start_at)
        with _lock:
            items = self.load()
            did = dag_id(strategy, ticker, interval or None)
            if any(x["id"] == did for x in items):
                raise ValueError(f"{did} is already live")
            if mode == "alpaca":
                self._check_alpaca_symbol(items, ticker.upper(), did)
            e = {"id": did, "strategy": strategy, "ticker": ticker.upper(), "interval": interval or None,
                 "flat_by_close": flat_by_close, "start_at": start_at or None, "inputs": dict(inputs or {}),
                 "mode": mode, "status": "running", "cash": float(cash), "added": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            items.append(e)
            self._save(items)
            return e

    def _check_builds(self, strategy, ticker, interval, flat_by_close, start_at=None, memory=None):
        """Refuse a DAG that cannot run (bad interval, flat-by-close on daily candles, a strategy that errors, a
        start state that does not exist or lacks the inputs it needs)."""
        from ..config import deployment_for
        from ..fsa import FsaRunner
        from ..runner import build_fsa
        try:
            dep = deployment_for(strategy, ticker, interval=interval or None, root=self.project,
                                 flat_by_close=flat_by_close)
            fsa, _ = build_fsa(dep)
            if start_at or memory:
                FsaRunner(fsa, flat_by_close=dep.flat_by_close, base_tf=dep.base_tf).reset(start_at, memory)
        except Exception as ex:  # noqa: BLE001
            raise ValueError(f"{strategy}/{ticker.upper()} cannot run: {ex}") from ex

    def request_restart(self, did: str, at: str | None = None, memory: dict | None = None) -> dict:
        """Ask the scanner to restart this DAG's FSA at a state (default: the start), with `memory` as the outputs
        of earlier states. Validated now; applied on the scanner's next cycle."""
        e = self.get(did)
        if e is None:
            raise KeyError(did)
        self._check_builds(e["strategy"], e["ticker"], e.get("interval"), e.get("flat_by_close"), at, memory)
        req = {"at": at or None, "memory": dict(memory or {}), "nonce": datetime.now(timezone.utc).isoformat()}
        return self.update(did, restart=req)

    @staticmethod
    def _check_alpaca_symbol(items, ticker, did):
        clash = [x["id"] for x in items if x["mode"] == "alpaca" and x["ticker"] == ticker and x["id"] != did
                 and x["status"] == "running"]
        if clash:
            raise ValueError(f"{clash[0]} already trades {ticker} on Alpaca: one strategy per symbol per Alpaca account")

    def update(self, did: str, **changes) -> dict:
        with _lock:
            items = self.load()
            e = next((x for x in items if x["id"] == did), None)
            if e is None:
                raise KeyError(did)
            if changes.get("mode") not in (None, *MODES) or changes.get("status") not in (None, *STATUSES):
                raise ValueError("bad mode/status")
            nxt = {**e, **{k: v for k, v in changes.items() if v is not None}}
            if nxt["mode"] == "alpaca" and nxt["status"] == "running":
                self._check_alpaca_symbol(items, nxt["ticker"], did)
            e.update(nxt)
            self._save(items)
            return e

    def remove(self, did: str):
        with _lock:
            self._save([x for x in self.load() if x["id"] != did])


class MarketClock:
    """Market open/closed: Alpaca's clock when keys exist (read-only), else regular hours on weekdays."""

    def __init__(self):
        self._client, self._cache = None, (0.0, None)

    def __call__(self) -> BClock:
        if time.time() - self._cache[0] < 30 and self._cache[1] is not None:
            return self._cache[1]
        c = None
        try:
            if self._client is None:
                from alpaca.trading.client import TradingClient
                from ..data.providers import alpaca_keys
                k, s = alpaca_keys()
                if k and s:
                    self._client = TradingClient(k, s, paper=True)
            if self._client is not None:
                x = self._client.get_clock()
                c = BClock(now=x.timestamp, is_open=bool(x.is_open), next_open=x.next_open, next_close=x.next_close)
        except Exception:  # noqa: BLE001
            c = None
        c = c or self._weekday_hours()
        self._cache = (time.time(), c)
        return c

    @staticmethod
    def _weekday_hours() -> BClock:
        import pandas as pd
        now = pd.Timestamp.now(tz="America/New_York")
        open_t, close_t = now.normalize() + pd.Timedelta(hours=9, minutes=30), now.normalize() + pd.Timedelta(hours=16)
        is_open = now.weekday() < 5 and open_t <= now < close_t
        nxt = open_t if now < open_t and now.weekday() < 5 else open_t + pd.Timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += pd.Timedelta(days=1)
        return BClock(now=now.to_pydatetime(), is_open=is_open, next_open=nxt.to_pydatetime(),
                      next_close=(close_t if now < close_t else close_t + pd.Timedelta(days=1)).to_pydatetime())
