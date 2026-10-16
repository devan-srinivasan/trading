"""The always-on live runner: scan candles -> run each strategy's DAG -> send orders to the Alpaca PAPER account.

One `cycle()`:
  1. read account / clock / positions / open orders (once, shared by all strategies)
  2. for each live strategy: when a new candle has closed, re-run its DAG on fresh candles -> Decision
  3. plan(Decision, position, orders) -> actions (entry bracket order / exit) -> send them
  4. write live/status.json + live/journal.jsonl so the dashboard (or `dagtrade live status`) can show it
If an order fails: retry immediately; still failing -> re-run the whole DAG on fresh data and re-plan; only then pause.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .. import tf as tfm
from ..config import Deployment
from ..data import AlpacaProvider, BarStore, DataSource
from ..data.source import project_root
from ..graph import Graph
from .broker import Broker
from .models import BClock, slug
from .reconcile import Action, Decision, DepState, plan

log = logging.getLogger("dagtrade.live")

CHART_DAYS = 90            # window of the dashboard's live chart
POLL_SECONDS = 20          # scan frequency while the market is open
MIN_LOOKBACK_DAYS = 30


def live_dir() -> Path:
    d = project_root() / "live"
    (d / "state").mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------------------------
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
RETRY_DELAYS = (0.25, 1.0, 2.5)          # seconds between immediate retries of a failed order call


def classify_error(e: Exception) -> str:
    """'transient' (network blip / rate limit / 5xx: trying again can work) or 'permanent' (rejected: 403 buying
    power, 422 invalid, ...: the same request will fail the same way)."""
    code = getattr(e, "status_code", None)
    if code is not None:
        return "transient" if code in TRANSIENT_HTTP else "permanent"
    text = f"{type(e).__name__} {e}".lower()
    if isinstance(e, (ConnectionError, TimeoutError)) or any(
            w in text for w in ("timed out", "timeout", "connection", "temporarily", "reset by peer", "unreachable")):
        return "transient"
    return "permanent"


@dataclass
class LiveSettings:
    """[live] in a strategy TOML. Order size, take-profit and stop-loss come from the strategy's Trade node."""
    enabled: bool = False
    feed: str = "iex"                    # alpaca data feed: iex (real-time, free) or sip (15-min delayed on free plan)

    @classmethod
    def from_dict(cls, d: dict) -> "LiveSettings":
        bad = set(d) - set(cls.__dataclass_fields__)
        if bad:
            raise ValueError(f"unknown [live] keys {sorted(bad)} (only: enabled, feed)")
        return cls(**d)


@dataclass
class LiveDeployment:
    dep: Deployment
    graph: Graph
    base_tf: str
    settings: LiveSettings
    data: DataSource
    next_check: float = 0.0
    last_chart: float = 0.0
    notes: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.dep.label


def _default_data(dep: Deployment, s: LiveSettings) -> DataSource:
    return DataSource([AlpacaProvider(feed=s.feed)], BarStore(project_root() / "data" / "live_bars"),
                      slack=pd.Timedelta(seconds=20))


def build_live(dep: Deployment, data_factory=None) -> LiveDeployment:
    from ..runner import build_graph
    s = LiveSettings.from_dict(dep.live)
    graph, _ = build_graph(dep)
    _, base = graph.resolve_tfs(graph.topo(), dep.base_tf)
    return LiveDeployment(dep, graph, base, s, (data_factory or _default_data)(dep, s))


# ---------------------------------------------------------------------------------------------
def compute_decision(li: LiveDeployment, now: pd.Timestamp) -> Decision:
    """Run the DAG on the freshest candles and read the LAST closed candle's trade intent."""
    from ..runner import node_frame
    lookback = max(pd.Timedelta(days=MIN_LOOKBACK_DAYS), li.graph.required_history(now))
    gr = li.graph.run(li.data, li.dep.ticker, now - lookback, now, base_tf=li.dep.base_tf, cache=False, live=True)
    if not len(gr.intents):
        raise LookupError(f"no candles for {li.dep.ticker}")
    last, bars = gr.intents.iloc[-1], gr.base_bars
    nf, _ = node_frame(gr, pd.DatetimeIndex(bars["avail"]), bars["close"])
    nodes = {}
    if len(nf):
        for c, v in nf.iloc[-1].items():
            if isinstance(v, (bool, np.bool_)):
                nodes[c] = bool(v)
            elif v is not None and np.isfinite(v):
                nodes[c] = round(float(v), 4)
    px = lambda x: None if x != x else float(x)           # noqa: E731
    return Decision(avail=bars["avail"].iloc[-1].timestamp(), price=float(bars["close"].iloc[-1]),
                    target=float(last["target"]), usd=float(last["usd"]) or float(li.graph.trader.notional),
                    stop=px(last["stop"]), take=px(last["take"]), limit=px(last["limit"]), entry=bool(last["entry"]),
                    nodes=nodes)


class Journal:
    def __init__(self, path: Path | None = None):
        self.path = path or live_dir() / "journal.jsonl"
        self._lock = threading.Lock()

    def add(self, label: str, type_: str, text: str, **data):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "label": label, "type": type_,
               "text": text, **({"data": data} if data else {})}
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        (log.error if type_ == "error" else log.info)("[%s] %s %s", label, type_, text)

    def tail(self, n: int = 100) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines()[-n:]][::-1]


class StateStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or live_dir()) / "state"
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, label: str) -> DepState:
        p = self.root / f"{slug(label)}.json"
        if p.exists():
            try:
                return DepState(**json.loads(p.read_text()))
            except Exception:  # noqa: BLE001
                log.warning("unreadable state for %s; starting fresh", label)
        return DepState()

    def save(self, label: str, st: DepState):
        (self.root / f"{slug(label)}.json").write_text(json.dumps(asdict(st), indent=1))


class RunnerLock:
    """Only one runner may trade at a time (two would double-send orders)."""

    def __init__(self, path: Path | None = None):
        self.path, self.fh = path or live_dir() / "runner.lock", None

    def acquire(self) -> bool:
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


# ---------------------------------------------------------------------------------------------
class LiveRunner:
    def __init__(self, deployments: list[Deployment], broker: Broker, data_factory=None, root: Path | None = None,
                 clock_fn=time.time, charts: bool = True, sleep_fn=time.sleep, retry_delays=RETRY_DELAYS):
        self.root = root or live_dir()
        self.broker, self.clock_fn, self.charts = broker, clock_fn, charts
        self.sleep_fn, self.retry_delays = sleep_fn, tuple(retry_delays)
        self.items = [build_live(d, data_factory) for d in deployments if d.live.get("enabled")]
        syms: dict[str, str] = {}
        for li in self.items:
            if li.dep.ticker in syms:
                raise ValueError(f"{li.label} and {syms[li.dep.ticker]} both trade {li.dep.ticker}: one strategy per "
                                 f"symbol per account (their orders would collide)")
            syms[li.dep.ticker] = li.label
        self.journal, self.states = Journal(self.root / "journal.jsonl"), StateStore(self.root)
        self.status: dict = {"deployments": [], "heartbeat": None, "mode": broker.name}

    # -- controls ---------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return (self.root / "PAUSED").exists()

    def set_paused(self, v: bool):
        p = self.root / "PAUSED"
        p.touch() if v else p.unlink(missing_ok=True)
        self.journal.add("*", "note", "PAUSED - no orders will be sent" if v else "resumed")

    # -- one pass ---------------------------------------------------------------------------------
    def cycle(self) -> dict:
        now = self.clock_fn()
        try:
            acct, clock = self.broker.account(), self.broker.clock()
            positions, orders = self.broker.positions(), self.broker.open_orders()
        except Exception as e:  # noqa: BLE001
            self.status.update(error=f"broker read failed: {e}", heartbeat=now)
            self.journal.add("*", "error", f"broker read failed: {e}")
            self._write_status()
            return self.status
        blocked = acct.trading_blocked or acct.status.upper() not in ("ACTIVE", "ACCOUNT_UPDATED")
        rows = []
        for li in self.items:
            try:
                rows.append(self._one(li, now, acct, clock, positions, orders, blocked))
            except Exception as e:  # noqa: BLE001
                self.journal.add(li.label, "error", f"cycle failed: {e}")
                rows.append({"label": li.label, "error": str(e)})
        self.status = {"heartbeat": now, "mode": self.broker.name, "paused": self.paused, "error": None,
                       "account": {"equity": acct.equity, "cash": acct.cash, "buying_power": acct.buying_power,
                                   "status": acct.status},
                       "clock": {"is_open": clock.is_open, "next_open": str(clock.next_open),
                                 "next_close": str(clock.next_close)}, "deployments": rows}
        self._write_status()
        return self.status

    def _one(self, li: LiveDeployment, now: float, acct, clock: BClock, positions, orders, blocked: bool,
             redo: bool = False) -> dict:
        st = self.states.load(li.label)
        sym = li.dep.ticker
        new_bar = False
        # re-run the DAG when a candle has closed, when asked to (redo), or while an entry is still unsent
        if now >= li.next_check or redo or st.pending is not None:
            dec = compute_decision(li, pd.Timestamp(now, unit="s", tz="UTC"))
            new_bar = st.last_avail is None or dec.avail > st.last_avail + 1e-6
            tfd = tfm.delta(li.base_tf).total_seconds()
            if new_bar:
                st.last_avail, st.decision = dec.avail, asdict(dec)
                self.journal.add(li.label, "decision",
                                 f"candle closed {datetime.fromtimestamp(dec.avail, timezone.utc):%Y-%m-%d %H:%M}Z  "
                                 f"{'ENTRY ' if dec.entry else ''}target {dec.target:+.0f}  stop {dec.stop}  "
                                 f"take {dec.take}  px {dec.price:.2f}", nodes=dec.nodes)
                nxt = dec.avail + tfd + 2
                if tfm.is_intraday(li.base_tf) and not clock.is_open:
                    nxt = max(nxt, clock.next_open.timestamp() + tfd + 2)
                li.next_check = max(now + 5, nxt)
            else:
                st.decision = asdict(dec)
                li.next_check = now + (POLL_SECONDS if clock.is_open or not tfm.is_intraday(li.base_tf) else 600)
        if st.decision is None:
            self.states.save(li.label, st)
            return {"label": li.label, "symbol": sym, "base_tf": li.base_tf, "note": "waiting for first decision"}
        dec = Decision(**st.decision)
        if st.pending is not None and st.pending != dec.avail:        # a newer candle superseded the unsent entry
            self.journal.add(li.label, "note", "dropped an unsent entry: a newer candle made it obsolete")
            st.pending = None

        pos_qty = positions.get(sym, 0.0)
        my_orders = [o for o in orders if o.symbol == sym]
        notes: list[str] = []
        if blocked:
            notes.append("account is blocked/not active: no orders")
        elif now >= st.retry_not_before:
            acts, plan_notes = plan(dec, pos_qty, my_orders, li.label, now, new_bar or st.pending == dec.avail,
                                    self.paused)
            notes += plan_notes
            failure = self._execute(li, acts, st, now, sym, dec)
            if failure == "transient" and not redo:
                # Retries are exhausted. Do not sit on a stale plan: re-read the account and re-run the whole DAG on
                # fresh candles right now, then plan again.
                self.states.save(li.label, st)
                self.journal.add(li.label, "note", "order failed after retries (transient) -> re-running the whole "
                                 "DAG on fresh data and re-planning")
                try:
                    return self._one(li, self.clock_fn(), acct, clock, self.broker.positions(),
                                     self.broker.open_orders(), blocked, redo=True)
                except Exception as e:  # noqa: BLE001
                    self.journal.add(li.label, "error", f"re-run failed: {e}")
                    st = self.states.load(li.label)
            if failure == "transient":
                st.retry_not_before = now + 10          # short pause; the next poll starts again from scratch
            elif failure == "permanent":
                st.retry_not_before = now + 60
        else:
            notes.append(f"backing off after an error until {datetime.fromtimestamp(st.retry_not_before):%H:%M:%S}")
        for n in notes:
            if n not in li.notes[-3:]:
                self.journal.add(li.label, "note", n)
        li.notes = (li.notes + notes)[-6:]
        self.states.save(li.label, st)
        if self.charts and new_bar:
            self._refresh_chart(li, now)
        return {"label": li.label, "symbol": sym, "base_tf": li.base_tf,
                "last_bar": datetime.fromtimestamp(dec.avail, timezone.utc).isoformat(timespec="minutes"),
                "decision": asdict(dec), "position": pos_qty,
                "orders": [{"kind": o.kind, "side": o.side, "qty": o.qty, "status": o.status, "type": o.type,
                            "stop": o.stop_price, "limit": o.limit_price} for o in my_orders],
                "last_action": st.last_action, "last_error": st.last_error, "notes": li.notes[-3:],
                "chart_run": f"live_{slug(li.label)}"}

    # -- sending ----------------------------------------------------------------------------------
    def _call(self, fn, *args, **kw):
        """Send one broker request; on a transient error retry IMMEDIATELY (a few quick tries). Safe to repeat:
        every order carries a deterministic client_order_id, so Alpaca rejects a genuine duplicate."""
        for delay in (*self.retry_delays, None):
            try:
                return fn(*args, **kw)
            except Exception as e:  # noqa: BLE001
                if delay is None or classify_error(e) != "transient" or self._is_duplicate(e):
                    raise
                log.warning("transient broker error (%s); retrying in %.2fs", e, delay)
                self.sleep_fn(delay)

    @staticmethod
    def _is_duplicate(e: Exception) -> bool:
        m = str(e).lower()
        return "client_order_id" in m and "unique" in m

    def _execute(self, li, acts: list[Action], st: DepState, now: float, sym: str, dec: Decision):
        """Send the planned actions. Returns the failure kind: None, 'transient' or 'permanent'."""
        b = self.broker
        for a in acts:
            try:
                if a.kind == "cancel":
                    self._call(b.cancel, a.order_id)
                elif a.kind == "close":
                    self._call(b.submit_market, sym, a.side, a.qty, a.client_id)
                elif a.kind == "entry":
                    self._call(b.submit_entry, sym, a.side, a.qty, a.client_id, a.stop, a.take, a.limit)
                    st.pending = None
                self.journal.add(li.label, "action", str(a), kind=a.kind, side=a.side, qty=a.qty, stop=a.stop,
                                 take=a.take, client_id=a.client_id)
                if a.kind != "cancel":
                    st.last_action = {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                                      "text": str(a)}
                st.last_error = None
            except Exception as e:  # noqa: BLE001
                if self._is_duplicate(e):
                    self.journal.add(li.label, "note", f"{a.kind} already sent earlier ({a.client_id})")
                    if a.kind == "entry":
                        st.pending = None
                    continue
                kind = classify_error(e)
                self.journal.add(li.label, "error", f"{a} FAILED ({kind}): {e}")
                st.last_error = f"{a}: {e}"
                if a.kind == "entry":
                    st.pending = dec.avail if kind == "transient" else None      # keep trying only for blips
                return kind
        return None

    # -- dashboard chart: the same backtest pipeline, on fresh candles, plus real fills ------------
    def _refresh_chart(self, li: LiveDeployment, now: float):
        if now - li.last_chart < 120:
            return
        li.last_chart = now
        try:
            from ..runner import run_deployment
            from ..store import RunStore
            start = (pd.Timestamp(now, unit="s", tz="UTC") - pd.Timedelta(days=CHART_DAYS)).date()
            dep = replace(li.dep, start=str(start), end=str((pd.Timestamp(now, unit="s") + pd.Timedelta(days=1)).date()),
                          warmup_days=min(li.dep.warmup_days, 400), holdout=0.0)
            rid = f"live_{slug(li.label)}"
            store = RunStore()
            run_deployment(dep, data=li.data, store=store, run_id=rid,
                           extra_meta={"live": True, "label": li.label, "mode": self.broker.name})
            since = datetime.fromtimestamp(now - CHART_DAYS * 86400, timezone.utc)
            rows = [{"time": o.filled_at, "side": o.side, "qty": o.filled_qty, "price": o.filled_avg_price,
                     "kind": o.kind} for o in self.broker.recent_orders(li.dep.ticker, since)
                    if o.filled_qty and o.filled_avg_price and o.filled_at is not None]
            pd.DataFrame(rows, columns=["time", "side", "qty", "price", "kind"]).to_parquet(
                store.dir(rid) / "live_fills.parquet")
        except Exception as e:  # noqa: BLE001
            self.journal.add(li.label, "note", f"chart refresh skipped: {e}")

    def _write_status(self):
        try:
            (self.root / "status.json").write_text(json.dumps(self.status, default=str, indent=1))
        except OSError:
            pass

    # -- forever ---------------------------------------------------------------------------------
    def run_forever(self, stop: threading.Event | None = None):
        stop = stop or threading.Event()
        lock = RunnerLock(self.root / "runner.lock")
        if not lock.acquire():
            raise RuntimeError("another dagtrade live runner is already running (live/runner.lock)")
        try:
            names = ", ".join(f"{li.label} ({li.base_tf})" for li in self.items) or "none"
            self.journal.add("*", "note", f"runner started [{self.broker.name}] strategies: {names}")
            while not stop.is_set():
                t0 = time.time()
                self.cycle()
                open_ = (self.status.get("clock") or {}).get("is_open")
                stop.wait(max(1.0, (POLL_SECONDS if open_ else 60) - (time.time() - t0)))
            self.journal.add("*", "note", "runner stopped")
        finally:
            lock.release()
