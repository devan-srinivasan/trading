"""The always-on live scheduler: live candles -> each strategy's FSA -> orders to its broker.

Each live DAG has its own broker:
  * "local"  - LocalBroker: this process's paper engine (the backtest's fill rules), $10k account per DAG
  * "alpaca" - the Alpaca PAPER account (shared by all Alpaca-mode DAGs)
Which DAGs are live comes from live/active.json (the Registry), re-read every cycle, so the dashboard can add, stop,
start, move and restart DAGs without restarting the scanner.

One `cycle()`:
  1. sync with the registry (start new DAGs, retire stopped ones, apply "restart at <state>" requests)
  2. read each broker once (account / clock / positions / open orders)
  3. for each DAG, for every candle of its interval that closed since the last cycle: a local broker processes it
     (fills, exit legs), then the FSA steps (the active state looks at its candles and maybe moves on); the Trade
     state's intents (entry / close / ...) are queued
  4. plan(outstanding intents, position, orders) -> broker actions -> send them
  5. write live/status.json, live/dags/<dag>.json, live/charts/<dag>.json and live/journal.jsonl
The FSA's state is saved after every step (live/state/<dag>.json), so a restart resumes where it was.
If an order fails: retry immediately; still failing -> re-read the account and re-plan; only then pause.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .. import tf as tfm
from ..config import Deployment, deployment_for
from ..data import AlpacaProvider, BarStore, DataSource
from ..data.source import project_root
from ..fsa import FSA, Account, CandleView, FsaRunner, Intent, Trade
from ..fsa.runner import jsonable
from .broker import Broker
from .local_broker import LocalBroker
from .models import BClock, slug
from .pnl import position_pnl
from .reconcile import Action, DepState, plan
from .registry import MarketClock, Registry

log = logging.getLogger("dagtrade.live")

CHART_BARS = 400           # candles kept for each live DAG's chart
POLL_SECONDS = 20          # scan frequency while the market is open
MIN_LOOKBACK_DAYS = 30
MAX_CATCH_UP = 3           # after an outage, step through at most this many missed candles (older ones are stale)
EVENTS_KEPT = 200


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
    """[live] in a strategy TOML: only the data feed. Order size, take-profit and stop-loss come from the Trade."""
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
    fsa: FSA
    runner: FsaRunner
    base_tf: str
    settings: LiveSettings
    data: DataSource
    key: str = ""
    broker: Broker | None = None
    mode: str = "alpaca"
    interval: str | None = None
    added: float = 0.0
    next_check: float = 0.0
    last_bars: pd.DataFrame | None = None
    chart_src: dict | None = None
    orders_cache: list = field(default_factory=list)
    orders_at: float = 0.0
    notes: list = field(default_factory=list)
    start_at: str | None = None
    start_memory: dict | None = None

    @property
    def label(self) -> str:
        return self.key or self.dep.label


def _default_data(dep: Deployment, s: LiveSettings) -> DataSource:
    return DataSource([AlpacaProvider(feed=s.feed)], BarStore(project_root() / "data" / "live_bars"),
                      slack=pd.Timedelta(seconds=20))


def build_live(dep: Deployment, data_factory=None, key: str | None = None, broker: Broker | None = None,
               mode: str = "alpaca", cash: float = 10_000.0) -> LiveDeployment:
    from ..runner import build_fsa
    s = LiveSettings.from_dict(dep.live)
    fsa, _ = build_fsa(dep)
    runner = FsaRunner(fsa, inputs={"cash": cash, **dep.inputs}, flat_by_close=dep.flat_by_close, base_tf=dep.base_tf)
    return LiveDeployment(dep, fsa, runner, runner.base, s, (data_factory or _default_data)(dep, s),
                          key=key or dep.label, broker=broker, mode=mode, interval=dep.base_tf)


def stages(li: LiveDeployment, position: float = 0.0) -> list[dict]:
    """Every state, in order, for the dashboard: done (on the path before the active one) / active / idle."""
    snap = li.runner.snapshot()
    out = []
    for name in li.fsa.order:
        n = li.fsa.nodes[name]
        if name == snap["current"]:
            state = "active"
        elif name in snap["path"]:
            state = "done"
        else:
            state = "idle"
        cand, dur = li.fsa.max_wait(name)
        row = {"name": name, "kind": n.kind, "interval": li.runner.tfs[id(n)], "state": state,
               "output": snap["memory"].get(name), "edges": li.fsa.edges.get(name, {}),
               "max_wait": f"{cand} candles" if cand else (str(dur) if dur is not None else None)}
        if state == "active":
            row["waited"] = snap["waited"]
            row["since"] = snap["entered_at"]
            if isinstance(n, Trade):
                row["output"] = {**(snap.get("trade") or {}), "position": position}
                row["output"].pop("signal", None)
        out.append(row)
    return out


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

    def tail(self, n: int = 100, label: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(x) for x in self.path.read_text().splitlines()]
        if label:
            rows = [r for r in rows if r["label"] in (label, "*")]
        return rows[-n:][::-1]


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
                log.warning("unreadable (or pre-FSA) state for %s; starting fresh", label)
        return DepState()

    def save(self, label: str, st: DepState):
        (self.root / f"{slug(label)}.json").write_text(json.dumps(asdict(st), default=str))


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
    """Pass `registry` (normal use: DAGs come from live/active.json) or, for tests/legacy, `deployments` + `broker`."""

    def __init__(self, deployments: list[Deployment] | None = None, broker: Broker | None = None, data_factory=None,
                 root: Path | None = None, clock_fn=time.time, charts: bool = True, sleep_fn=time.sleep,
                 retry_delays=RETRY_DELAYS, registry: Registry | None = None, alpaca_factory=None, market_clock=None):
        self.root = root or live_dir()
        (self.root / "dags").mkdir(parents=True, exist_ok=True)
        self.broker, self.clock_fn, self.charts = broker, clock_fn, charts
        self.sleep_fn, self.retry_delays = sleep_fn, tuple(retry_delays)
        self.data_factory, self.registry, self.alpaca_factory = data_factory, registry, alpaca_factory
        self.market_clock = market_clock or (broker.clock if broker is not None else MarketClock())
        self._alpaca: Broker | None = broker
        self.entry_errors: dict[str, str] = {}
        self.items: list[LiveDeployment] = []
        self.journal, self.states = Journal(self.root / "journal.jsonl"), StateStore(self.root)
        if registry is None:
            self.items = [build_live(d, data_factory, broker=broker) for d in (deployments or []) if d.live.get("enabled")]
            syms: dict[str, str] = {}
            for li in self.items:
                if li.dep.ticker in syms:
                    raise ValueError(f"{li.label} and {syms[li.dep.ticker]} both trade {li.dep.ticker}: one strategy "
                                     f"per symbol per account (their orders would collide)")
                syms[li.dep.ticker] = li.label
                self._load_fsa(li)
        self.status: dict = {"deployments": [], "heartbeat": None, "mode": broker.name if broker else "registry"}

    # -- controls ---------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return (self.root / "PAUSED").exists()

    def set_paused(self, v: bool):
        p = self.root / "PAUSED"
        p.touch() if v else p.unlink(missing_ok=True)
        self.journal.add("*", "note", "PAUSED - no orders will be sent" if v else "resumed")

    # -- registry ---------------------------------------------------------------------------------
    def _alpaca_broker(self) -> Broker:
        if self._alpaca is None:
            if self.alpaca_factory is None:
                raise RuntimeError("no Alpaca broker configured")
            self._alpaca = self.alpaca_factory()
        return self._alpaca

    def _load_fsa(self, li: LiveDeployment):
        """Resume the FSA from its saved state, or start it (at the start state, or where the registry says)."""
        st = self.states.load(li.label)
        if st.fsa:
            li.runner.state = st.fsa
            if li.runner.state.get("current") not in li.fsa.nodes:          # the strategy changed: start over
                li.runner.reset()
        else:
            li.runner.reset(li.start_at, li.start_memory)

    def _build_entry(self, e: dict) -> LiveDeployment:
        dep = deployment_for(e["strategy"], e["ticker"], interval=e.get("interval"), root=self.registry.project,
                             flat_by_close=e.get("flat_by_close"))
        dep.live = {**{k: v for k, v in dep.live.items() if k == "feed"}, "enabled": True}
        dep.inputs = {**dep.inputs, **(e.get("inputs") or {})}
        if e["mode"] == "local":
            sim = dep.sim_config()
            broker = LocalBroker(e["id"], e["ticker"], self.root, clock=self.market_clock, cash=e.get("cash", 10_000.0),
                                 commission_bps=sim.commission_bps, slippage_bps=sim.slippage_bps, now_fn=self.clock_fn)
        else:
            broker = self._alpaca_broker()
        li = build_live(dep, self.data_factory, key=e["id"], broker=broker, mode=e["mode"], cash=e.get("cash", 10_000.0))
        li.interval = e.get("interval")
        li.added = pd.Timestamp(e.get("added") or pd.Timestamp.now(tz="UTC")).timestamp()
        li.start_at = e.get("start_at")
        self._load_fsa(li)
        return li

    def _sync_registry(self):
        if self.registry is None:
            return
        running = {e["id"]: e for e in self.registry.load() if e["status"] == "running"}
        for li in list(self.items):
            e = running.get(li.key)
            if e is None or e["mode"] != li.mode:
                why = "stopped" if e is None else f"moved to {e['mode']}"
                if isinstance(li.broker, LocalBroker):
                    li.broker.flatten_now(why)                     # never leave a local position unmanaged
                self._write_detail(li, extra={"running": False})
                self.journal.add(li.key, "note", f"{why}" + (" - local position closed at the last price"
                                                             if isinstance(li.broker, LocalBroker) else ""))
                self.items.remove(li)
        have = {li.key for li in self.items}
        for did, e in running.items():
            if did in have:
                continue
            try:
                self.items.append(self._build_entry(e))
                self.entry_errors.pop(did, None)
                self.journal.add(did, "note", f"started ({e['mode']} paper) at state {self.items[-1].runner.current}")
            except Exception as ex:  # noqa: BLE001
                if self.entry_errors.get(did) != str(ex):
                    self.journal.add(did, "error", f"cannot start: {ex}")
                self.entry_errors[did] = str(ex)
        for li in self.items:                                      # "restart at <state>" from the dashboard / CLI
            req = (running.get(li.key) or {}).get("restart")
            if not req:
                continue
            st = self.states.load(li.label)
            if st.restart_nonce == req.get("nonce"):
                continue
            st.restart_nonce = req.get("nonce")
            try:
                li.runner.reset(req.get("at"), req.get("memory"), now=self.clock_fn())
                st.fsa, st.intents = li.runner.state, []
                st.last_avail = None                                # act on the latest closed candle right away
                self.journal.add(li.label, "note", f"restarted at state {li.runner.current} (dashboard)")
            except ValueError as ex:
                self.journal.add(li.label, "error", f"cannot restart: {ex}")
            self.states.save(li.label, st)
            li.next_check = 0.0

    # -- one pass ---------------------------------------------------------------------------------
    def cycle(self) -> dict:
        now = self.clock_fn()
        self._sync_registry()
        reads: dict[int, tuple] = {}
        rows = []
        for li in self.items:
            b = li.broker
            if id(b) not in reads:
                try:
                    reads[id(b)] = (b.account(), b.clock(), b.positions(), b.open_orders(), None)
                except Exception as e:  # noqa: BLE001
                    reads[id(b)] = (None, None, None, None, str(e))
                    self.journal.add(li.label, "error", f"broker read failed: {e}")
            acct, clock, positions, orders, err = reads[id(b)]
            if err:
                rows.append({"label": li.label, "mode": li.mode, "error": f"broker read failed: {err}"})
                continue
            blocked = acct.trading_blocked or acct.status.upper() not in ("ACTIVE", "ACCOUNT_UPDATED")
            try:
                rows.append(self._one(li, now, acct, clock, positions, orders, blocked))
            except Exception as e:  # noqa: BLE001
                log.exception("cycle failed for %s", li.label)
                self.journal.add(li.label, "error", f"cycle failed: {e}")
                rows.append({"label": li.label, "mode": li.mode, "error": str(e)})
        for did, err in self.entry_errors.items():
            rows.append({"label": did, "error": f"cannot start: {err}"})
        try:
            clk = self.market_clock()
            clock_info = {"is_open": clk.is_open, "next_open": str(clk.next_open), "next_close": str(clk.next_close)}
        except Exception:  # noqa: BLE001
            clock_info = None
        alp = next((r[0] for li in self.items if li.mode == "alpaca" and (r := reads.get(id(li.broker))) and r[0]), None)
        self.status = {"heartbeat": now, "mode": self.broker.name if self.broker else "registry", "paused": self.paused,
                       "error": None, "clock": clock_info, "deployments": rows,
                       "account": None if alp is None else {"equity": alp.equity, "cash": alp.cash,
                                                            "buying_power": alp.buying_power, "status": alp.status}}
        self._write_status()
        return self.status

    def _status_fn(self, li: LiveDeployment, orders):
        pre = f"dt.{slug(li.label)}.ent."

        def status(token: str) -> str | None:
            cid = pre + token
            if isinstance(li.broker, LocalBroker):
                return li.broker.status(cid)
            for o in orders:
                if o.client_id == cid:
                    return o.status
            for o in li.orders_cache:
                if o.get("client_id") == cid:
                    return o.get("status")
            return None
        return status

    def _account(self, li, acct, positions, orders, price) -> Account:
        return Account(cash=getattr(acct, "cash", 0.0), equity=getattr(acct, "equity", 0.0),
                       position=positions.get(li.dep.ticker, 0.0), last_price=price,
                       order_status=self._status_fn(li, orders))

    def _step_new_candles(self, li: LiveDeployment, st: DepState, now: float, clock: BClock, acct, positions, orders):
        """Load fresh candles; for each candle of the FSA's interval that closed since the last cycle, let a local
        broker process it, then step the FSA. Returns (acct, positions, orders) refreshed for local brokers."""
        nowts = pd.Timestamp(now, unit="s", tz="UTC")
        lookback = max(pd.Timedelta(days=MIN_LOOKBACK_DAYS), li.fsa.required_history(nowts, li.base_tf))
        tfs = list(dict.fromkeys([li.base_tf, *li.runner.tfs.values()]))
        view = CandleView(li.data, li.dep.ticker, nowts - lookback, nowts, tfs)
        bars = view.frame(li.base_tf)
        if not len(bars):
            raise LookupError(f"no candles for {li.dep.ticker}")
        li.last_bars = bars
        avail = pd.DatetimeIndex(bars["avail"])
        av = avail.asi8 / 1e9
        if st.last_avail is None:
            todo = [len(bars) - 1]                              # fresh start: only the candle that just closed
        else:
            todo = [int(i) for i in np.nonzero(av > st.last_avail + 1e-6)[0]]
            if len(todo) > MAX_CATCH_UP:
                self.journal.add(li.label, "note", f"skipped {len(todo) - MAX_CATCH_UP} missed candles (too old to act on)")
                todo = todo[-MAX_CATCH_UP:]
        if todo:                                                # an entry not sent before a newer candle is obsolete
            stale = [d for d in st.intents if d["kind"] == "entry" and d["token"].isdigit()
                     and int(d["token"]) < av[todo[-1]] - 1]
            if stale:
                self.journal.add(li.label, "note", "dropped an unsent entry: a newer candle made it obsolete")
                st.intents = [d for d in st.intents if d not in stale]
        if li.mode == "alpaca" and todo:
            self._refresh_orders(li, now, force=True)          # fresh order statuses for the Trade state
        for i in todo:
            if isinstance(li.broker, LocalBroker):
                if li.broker.on_candles(bars.iloc[: i + 1]):
                    acct, positions, orders = li.broker.account(), li.broker.positions(), li.broker.open_orders()
                    self._journal_fills(li)
            before = li.runner.current
            n_ev = len(li.runner.events)
            intents = li.runner.step(avail[i], view, self._account(li, acct, positions, orders, float(bars["close"].iloc[i])))
            for ev in li.runner.events[n_ev:]:
                self.journal.add(li.label, "transition", f"{ev['from']} -> {ev['to']} ({ev['outcome']}"
                                 + (f": {ev['reason']}" if ev["reason"] else "") + ")", **ev)
                st.events = (st.events + [jsonable(ev)])[-EVENTS_KEPT:]
            li.runner.events.clear()
            for it in intents:
                self.journal.add(li.label, "intent", f"{it.kind} {it.side or ''} ({it.reason})".replace("  ", " "),
                                 **{k: v for k, v in it.to_dict().items() if v not in (None, "")})
            st.intents += [it.to_dict() for it in intents]
            st.last_avail = float(av[i])
            stp = tak = None
            for o in orders:
                if o.active and o.type == "stop":
                    stp = o.stop_price
                elif o.active and o.type == "limit" and o.kind != "ent":
                    tak = o.limit_price
            st.trace = (st.trace + [{"t": float(av[i]), "state": li.runner.current,
                                     "values": jsonable(li.runner.state["memory"]), "stop": stp, "take": tak}])[-CHART_BARS:]
            if before == li.runner.current and not intents:
                pass
        st.fsa = li.runner.state
        tfd = tfm.delta(li.base_tf).total_seconds()
        nxt = av[-1] + tfd + 2
        if tfm.is_intraday(li.base_tf) and clock is not None and not clock.is_open:
            nxt = max(nxt, clock.next_open.timestamp() + tfd + 2)
        li.next_check = max(now + 5, nxt)
        self._make_chart(li, st, bars, now)
        return acct, positions, orders, bool(todo)

    def _one(self, li: LiveDeployment, now: float, acct, clock: BClock, positions, orders, blocked: bool,
             redo: bool = False) -> dict:
        st = self.states.load(li.label)
        if st.fsa:
            li.runner.state = st.fsa
        sym = li.dep.ticker
        new_bar = False
        if now >= li.next_check or redo:
            acct, positions, orders, new_bar = self._step_new_candles(li, st, now, clock, acct, positions, orders)
        pos_qty = positions.get(sym, 0.0)
        my_orders = [o for o in orders if o.symbol == sym]
        notes: list[str] = []
        acted = False
        if blocked:
            notes.append("account is blocked/not active: no orders")
        elif st.intents and now >= st.retry_not_before:
            intents = [Intent(**d) for d in st.intents]
            acts, plan_notes, keep = plan(intents, pos_qty, my_orders, li.label, now, self.paused,
                                          equity=getattr(acct, "equity", None), cash=getattr(acct, "cash", None))
            notes += plan_notes
            acted = bool(acts)
            failure = self._execute(li, acts, st, now, sym)
            st.intents = [i.to_dict() for i in (intents if failure == "transient" else keep)]
            if failure == "transient" and not redo:
                # Retries are exhausted. Do not sit on a stale plan: re-read the account and plan again right now.
                self.states.save(li.label, st)
                self.journal.add(li.label, "note", "order failed after retries (transient) -> re-reading the account "
                                 "and re-planning")
                try:
                    a2 = li.broker.account()
                    return self._one(li, self.clock_fn(), a2, clock, li.broker.positions(), li.broker.open_orders(),
                                     blocked, redo=True)
                except Exception as e:  # noqa: BLE001
                    self.journal.add(li.label, "error", f"re-plan failed: {e}")
                    st = self.states.load(li.label)
            if failure == "transient":
                st.retry_not_before = now + 10          # short pause; the next poll starts again from scratch
            elif failure == "permanent":
                st.retry_not_before = now + 60
        elif st.intents:
            notes.append(f"backing off after an error until {datetime.fromtimestamp(st.retry_not_before):%H:%M:%S}")
        for n in notes:
            if n not in li.notes[-3:]:
                self.journal.add(li.label, "note", n)
        li.notes = (li.notes + notes)[-6:]
        self.states.save(li.label, st)
        if li.chart_src is not None and li.chart_src.get("_written") != li.chart_src["updated"]:
            self._write_chart(li)
            li.chart_src["_written"] = li.chart_src["updated"]
        if acted:
            pos_qty = li.broker.positions().get(sym, 0.0)
            my_orders = [o for o in li.broker.open_orders() if o.symbol == sym]
        detail = self._write_detail(li, st=st, pos_qty=pos_qty, refresh_orders=new_bar or acted, now=now)
        return {"label": li.label, "mode": li.mode, "strategy": li.dep.strategy, "symbol": sym, "base_tf": li.base_tf,
                "last_bar": datetime.fromtimestamp(st.last_avail, timezone.utc).isoformat(timespec="minutes")
                if st.last_avail else None,
                "current_step": li.runner.current, "position": pos_qty, "account": detail.get("account"),
                "intents": st.intents, "fsa": li.runner.snapshot(),
                "orders": [{"kind": o.kind, "side": o.side, "qty": o.qty, "status": o.status, "type": o.type,
                            "stop": o.stop_price, "limit": o.limit_price} for o in my_orders],
                "last_action": st.last_action, "last_error": st.last_error, "notes": li.notes[-3:]}

    # -- per-DAG detail file for the dashboard -----------------------------------------------------
    def _journal_fills(self, li):
        fills = getattr(li.broker, "s", {}).get("fills", [])
        seen = getattr(li, "_fills_seen", None)
        if seen is None:
            li._fills_seen = len(fills)
            return
        for f in fills[seen:]:
            self.journal.add(li.label, "fill", f"{f['side']} {f['qty']:g} @ {f['price']:.2f} ({f['reason']})")
        li._fills_seen = len(fills)

    def _refresh_orders(self, li: LiveDeployment, now: float, force: bool = False):
        if not (force or now - li.orders_at > 60 or not li.orders_cache):
            return
        try:
            lo = now - 90 * 86400
            if li.mode == "alpaca" and li.added <= now:          # skip unrelated older activity on the account
                lo = max(lo, li.added - 86400)
            since = datetime.fromtimestamp(lo, timezone.utc)
            li.orders_cache = [{"id": o.id, "client_id": o.client_id, "side": o.side, "qty": o.qty,
                                "type": o.type, "status": o.status, "stop": o.stop_price, "limit": o.limit_price,
                                "fill_price": o.filled_avg_price,
                                "submitted": o.submitted_at.isoformat() if o.submitted_at else None,
                                "filled": o.filled_at.isoformat() if o.filled_at else None}
                               for o in li.broker.recent_orders(li.dep.ticker, since)]
            li.orders_at = now
        except Exception as e:  # noqa: BLE001
            log.warning("could not list orders for %s: %s", li.label, e)

    def _write_detail(self, li: LiveDeployment, st: DepState | None = None, pos_qty: float | None = None,
                      refresh_orders: bool = True, now: float | None = None, extra: dict | None = None) -> dict:
        now = now or self.clock_fn()
        b = li.broker
        self._refresh_orders(li, now, force=refresh_orders)
        last_px = float(li.last_bars["close"].iloc[-1]) if li.last_bars is not None and len(li.last_bars) else None
        if isinstance(b, LocalBroker):
            account = b.summary()
            equity = b.s["equity"][-2000:]
            fills = b.s["fills"][-1000:]
        else:
            fills = sorted(({"side": o["side"], "qty": o["qty"], "price": o["fill_price"], "time": o["filled"]}
                            for o in li.orders_cache if o["status"] == "filled" and o["fill_price"]),
                           key=lambda f: f["time"] or "")
            account = {**position_pnl(fills, last_px), "last_price": last_px}
            equity = []
        detail = {"id": li.label, "mode": li.mode, "broker": b.name, "strategy": li.dep.strategy, "ticker": li.dep.ticker,
                  "base_tf": li.base_tf, "updated": now, "running": True,
                  "fsa": li.runner.snapshot(), "stages": stages(li, pos_qty or 0.0),
                  "dag": li.fsa.describe(li.base_tf), "events": (st.events if st else [])[-40:][::-1],
                  "intents": st.intents if st else [], "last_avail": st.last_avail if st else None,
                  "last_price": last_px, "position": pos_qty,
                  "account": account, "equity": equity, "orders": li.orders_cache[::-1][:200], "fills": fills,
                  "last_action": st.last_action if st else None, "last_error": st.last_error if st else None,
                  **(extra or {})}
        try:
            p = self.root / "dags" / f"{slug(li.label)}.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(detail, default=str))
            tmp.replace(p)
        except OSError:
            pass
        return detail

    # -- sending ----------------------------------------------------------------------------------
    def _call(self, fn, *args, **kw):
        """Send one broker request; on a transient error retry IMMEDIATELY (a few quick tries). Safe to repeat:
        every order carries a deterministic client_order_id, so a genuine duplicate is rejected."""
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

    def _execute(self, li, acts: list[Action], st: DepState, now: float, sym: str):
        """Send the planned actions. Returns the failure kind: None, 'transient' or 'permanent'."""
        b = li.broker
        for a in acts:
            try:
                if a.kind == "cancel":
                    self._call(b.cancel, a.order_id)
                elif a.kind == "close":
                    self._call(b.submit_market, sym, a.side, a.qty, a.client_id)
                elif a.kind == "entry":
                    self._call(b.submit_entry, sym, a.side, a.qty, a.client_id, a.stop, a.take, a.limit)
                elif a.kind == "replace_stop":
                    self._call(b.replace_stop, a.order_id, a.stop)
                self.journal.add(li.label, "action", str(a), kind=a.kind, side=a.side, qty=a.qty, stop=a.stop,
                                 take=a.take, client_id=a.client_id)
                if a.kind not in ("cancel", "replace_stop"):
                    st.last_action = {"ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
                                      "text": str(a)}
                st.last_error = None
            except Exception as e:  # noqa: BLE001
                if self._is_duplicate(e):
                    self.journal.add(li.label, "note", f"{a.kind} already sent earlier ({a.client_id})")
                    continue
                kind = classify_error(e)
                self.journal.add(li.label, "error", f"{a} FAILED ({kind}): {e}")
                st.last_error = f"{a}: {e}"
                return kind
        return None

    # -- the live chart: recent candles + what each state was doing (the account's trades are added by the dashboard)
    def _make_chart(self, li: LiveDeployment, st: DepState, bars: pd.DataFrame, now: float):
        from ..runner import node_frame
        tail = bars.iloc[-CHART_BARS:]
        av = pd.DatetimeIndex(tail["avail"]).asi8 / 1e9
        by_t = {round(r["t"]): r for r in st.trace}
        rows = [by_t.get(round(x)) for x in av]
        trace = pd.DataFrame({"state": [r["state"] if r else None for r in rows],
                              "values": [r["values"] if r else {} for r in rows],
                              "stop": [r.get("stop") if r else None for r in rows],
                              "take": [r.get("take") if r else None for r in rows]}, index=tail.index)
        trace["stop"] = pd.to_numeric(trace["stop"], errors="coerce")
        trace["take"] = pd.to_numeric(trace["take"], errors="coerce")
        nf, info = node_frame(li.fsa, trace, tail["close"])
        li.chart_src = {"t": [int(x.timestamp()) for x in tail.index], "base_tf": li.base_tf, "info": info,
                        "updated": now,
                        "bars": {c: [None if v != v else round(float(v), 4) for v in tail[c]]
                                 for c in ("open", "high", "low", "close", "volume")},
                        "nodes": {c: [None if v is None or (isinstance(v, float) and v != v) else
                                      (bool(v) if isinstance(v, (bool, np.bool_)) else round(float(v), 5))
                                      for v in nf[c].tolist()] for c in nf.columns}}

    def _write_chart(self, li: LiveDeployment):
        if not li.chart_src:
            return
        try:
            d = self.root / "charts"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{slug(li.label)}.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: v for k, v in li.chart_src.items() if k != "_written"}, default=str))
            tmp.replace(p)
        except OSError as e:
            log.warning("could not write chart for %s: %s", li.label, e)

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
            self.journal.add("*", "note", "scanner started")
            while not stop.is_set():
                t0 = time.time()
                self.cycle()
                open_ = (self.status.get("clock") or {}).get("is_open")
                stop.wait(max(1.0, (POLL_SECONDS if open_ else 60) - (time.time() - t0)))
            self.journal.add("*", "note", "scanner stopped")
        finally:
            lock.release()
