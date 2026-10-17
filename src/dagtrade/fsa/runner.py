"""FsaRunner: drives one FSA through time. The same runner is used by backtests and by the live scheduler.

    runner = FsaRunner(fsa, inputs={"cash": 10_000}, flat_by_close=False)
    intents = runner.step(now, view, account)      # once per closed candle of the FSA's interval

A step calls the ACTIVE state when a new candle of its interval has closed. Its answer picks the next state; a new
state runs in the same step if it has a candle to look at (so a whole chain can pass on one candle), but no state
runs twice in one step. The Trade state turns its signal into Intents (entry / close / ...) that the planner sends
to the broker; it reads the account to know whether it filled and when it is flat again.
All state is plain data (`runner.state`), so the live scheduler saves it and resumes after a restart.
"""
from __future__ import annotations

import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from .. import tf as tfm
from .candles import CandleView
from .machine import FSA
from .node import Account, Ctx, Intent, Trade, TradeState, interpret

MAX_HOPS = 25               # transitions per step (a safety net against a strategy that loops on itself)
NO_ENTRY_MIN = 30           # flat-by-close: no new entries in the last 30 minutes (or the 2-candle exit window)
WORKING = {"accepted", "new", "pending_new", "partially_filled", "held", "accepted_for_bidding", "calculated",
           "pending_replace"}


def jsonable(v, depth=0):
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(float(v)) else float(v)
    if isinstance(v, str) or v is None:
        return v
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, dict) and depth < 4:
        return {str(k): jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)) and depth < 4:
        return [jsonable(x, depth + 1) for x in v[:50]]
    return str(v)[:120]


class FsaRunner:
    def __init__(self, fsa: FSA, inputs: dict | None = None, flat_by_close: bool = False, base_tf: str | None = None,
                 state: dict | None = None):
        self.fsa, self.inputs, self.flat_by_close = fsa, dict(inputs or {}), flat_by_close
        self.tfs, self.base = fsa.resolve_tfs(base_tf)
        if flat_by_close and not tfm.is_intraday(self.base):
            raise ValueError(f"flat by close needs intraday candles (e.g. 5m, 1h), not {self.base}")
        self.events: list[dict] = []          # transitions made by this runner object (backtests keep them all)
        self.state = state or {}
        if not self.state or self.state.get("current") not in fsa.nodes:
            self.reset()

    # -- state ----------------------------------------------------------------------------------------
    def reset(self, at: str | None = None, memory: dict | None = None, now: float | None = None) -> None:
        """Start (again) at the start state, or at `at` with `memory` as the outputs of earlier states."""
        at = at or self.fsa.start.name
        if at not in self.fsa.nodes:
            raise ValueError(f"no state called {at!r} (states: {', '.join(self.fsa.order)})")
        memory = dict(memory or {})
        missing = [n for n in self.fsa.nodes[at].needs if n not in memory]
        if missing:
            raise ValueError(f"starting at {at!r} needs the output of {', '.join(missing)}: provide it as inputs")
        start = self.fsa.start.name
        self.state = {"current": at, "memory": jsonable(memory), "path": [start] + ([at] if at != start else []),
                      "entered_at": now, "waited": 0, "last_seen": {}, "trade": None, "last_entry": None,
                      "trades": 0}
        if isinstance(self.fsa.nodes[at], Trade):
            self.state["trade"] = {"phase": "enter", "signal": memory.get("signal", {})}

    @property
    def current(self) -> str:
        return self.state["current"]

    # -- one step ------------------------------------------------------------------------------------
    def step(self, now: pd.Timestamp, view: CandleView, account: Account) -> list[Intent]:
        now = pd.Timestamp(now)
        t = now.timestamp()
        st = self.state
        intents: list[Intent] = []
        ran: set[str] = set()
        for _ in range(MAX_HOPS):
            name = st["current"]
            node = self.fsa.nodes[name]
            if name in ran:
                break
            reason = ""
            if isinstance(node, Trade):
                ran.add(name)
                outcome, data, its, reason = self._trade(node, now, view, account)
                intents += its
            else:
                tf = self.tfs[id(node)]
                idx = view.latest(tf, now)
                if idx is None:
                    break
                cav = pd.Timestamp(view.frame(tf)["avail"].iloc[idx]).timestamp()
                if cav <= st["last_seen"].get(name, -math.inf):
                    break                                       # no new candle of this state's interval yet
                cand, dur = self.fsa.max_wait(name)
                ran.add(name)
                if dur is not None and st["entered_at"] is not None and t - st["entered_at"] > dur.total_seconds():
                    outcome, data, reason = "max_wait", {}, f"waited longer than {dur}"
                else:
                    ctx = self._ctx(name, now, account)
                    res = node.run(view.window(node, tf, idx), ctx)
                    st["last_seen"][name] = cav
                    st["waited"] += 1
                    outcome, data = interpret(res)
                    reason = str(data.pop("reason", "")) if outcome in ("fail", "max_wait") else ""
                    if outcome is None and cand is not None and st["waited"] >= cand:
                        outcome, reason = "max_wait", f"no answer within {cand} candles"
            if outcome is None:
                break
            self._move(name, outcome, data, t, reason)
        return intents

    def _ctx(self, name: str, now: pd.Timestamp, account: Account) -> Ctx:
        st = self.state
        ent = st["entered_at"]
        return Ctx(now=now, state=name, memory=st["memory"], inputs=self.inputs, account=account,
                   entered_at=pd.Timestamp(ent, unit="s", tz="UTC") if ent is not None else None,
                   waited=st["waited"], minutes_to_close=self._to_close(now),
                   last_entry=pd.Timestamp(st["last_entry"], unit="s", tz="UTC") if st["last_entry"] else None)

    def _to_close(self, now: pd.Timestamp) -> float | None:
        if not tfm.is_intraday(self.base):
            return None
        ny = now.tz_convert("America/New_York")
        return 16 * 60 - (ny.hour * 60 + ny.minute + ny.second / 60)

    def _move(self, src: str, outcome: str, data: dict, t: float, reason: str = "") -> None:
        st = self.state
        dst = self.fsa.target(src, outcome)
        if outcome != "fail" and outcome != "max_wait" and not isinstance(self.fsa.nodes[src], Trade):
            st["memory"][src] = jsonable(data)
        start = self.fsa.start.name
        if dst == start:
            st["memory"], st["path"] = {}, [start]
        elif dst in st["path"]:
            keep = st["path"][: st["path"].index(dst) + 1]
            for n in st["path"][len(keep):]:
                st["memory"].pop(n, None)
            st["path"] = keep
        else:
            st["path"].append(dst)
        ev = {"t": t, "from": src, "to": dst, "outcome": outcome, "reason": reason}
        self.events.append(ev)
        st["current"], st["entered_at"], st["waited"] = dst, t, 0
        st["last_seen"].pop(dst, None)                          # a state just entered may look at the latest candle
        st["trade"] = {"phase": "enter", "signal": jsonable(data)} if isinstance(self.fsa.nodes[dst], Trade) else None

    # -- the Trade state ------------------------------------------------------------------------------
    def _trade(self, node: Trade, now: pd.Timestamp, view: CandleView, account: Account):
        """-> (outcome or None, data, intents, reason)"""
        st = self.state
        t = now.timestamp()
        raw = st["trade"] or {"phase": "enter", "signal": {}}
        mtc = self._to_close(now)
        idx = view.latest(self.base, now)
        close = float(view.frame(self.base)["close"].iloc[idx]) if idx is not None else None
        if raw["phase"] == "enter":
            if self.flat_by_close and mtc is not None and mtc <= max(NO_ENTRY_MIN, 2 * tfm.minutes(self.base)):
                return "fail", {}, [], "too close to the close for a new entry"   # it would have to exit at once
            if abs(account.position) > 1e-9:
                return "fail", {}, [], "already in a position"
            if close is None:
                return None, {}, [], ""
            o = node.order(raw.get("signal") or {}, close)
            token = str(int(t))
            ts = TradeState(phase="pending", token=token, side=o["side"], limit=o["limit"], stop=o["stop"],
                            take=o["take"], sl_frac=o["sl_frac"], trail=o["trail"], max_hold=o["max_hold"])
            st["trade"] = asdict(ts)
            return None, {}, [Intent("entry", token, side=o["side"], usd=o["usd"], pct=o["pct"], price=o["ref"],
                                     stop=o["stop"], take=o["take"], limit=o["limit"], reason="entry")], ""
        ts = TradeState(**{k: v for k, v in raw.items() if k != "signal"})
        out = self._trade_progress(node, ts, now, view, account, close, mtc)
        st["trade"] = asdict(ts)
        return out

    def _trade_progress(self, node, ts: TradeState, now, view, account, close, mtc):
        st = self.state
        t = now.timestamp()
        pos = account.position
        if ts.phase == "pending":
            status = account.status(ts.token)
            if abs(pos) > 1e-9:
                ts.phase, ts.entry_time, ts.held = "holding", t, 0
                st["last_entry"] = t
            elif status == "filled":                          # filled and already stopped out on that same candle
                st["last_entry"], st["trades"] = t, st["trades"] + 1
                return "ok", {}, [], "closed on its entry candle"
            elif status in WORKING and ts.limit is not None:
                return "fail", {}, [Intent("cancel_entry", ts.token, reason="limit not filled")], "limit entry not filled"
            elif status in WORKING:
                return None, {}, [], ""                       # market order still on its way
            else:
                return "fail", {}, [], f"entry not filled ({status or 'not sent'})"
        if ts.phase in ("holding", "closing") and abs(pos) < 1e-9:
            st["trades"] += 1
            return "ok", {}, [], "position closed"
        if ts.phase == "closing":
            return None, {}, [], ""
        ts.held += 1
        why = None
        if self.flat_by_close and mtc is not None and mtc <= 2 * tfm.minutes(self.base):
            why = "flat by close"
        elif ts.max_hold and ts.held >= ts.max_hold:
            why = f"max_hold ({ts.max_hold} candles)"
        elif node.exit is not None:
            ex = node.exit
            tf = self.tfs[id(ex)]
            idx = view.latest(tf, now)
            if idx is not None:
                cav = pd.Timestamp(view.frame(tf)["avail"].iloc[idx]).timestamp()
                if ts.exit_idx is None or cav > ts.exit_idx:
                    ts.exit_idx = cav
                    if ex.run(view.window(ex, tf, idx), self._ctx(st["current"], now, account)):
                        why = "exit signal"
        if why:
            ts.phase = "closing"
            return None, {}, [Intent("close", ts.token, reason=why)], ""
        if ts.trail and close is not None:
            d = 1 if ts.side == "buy" else -1
            cand = close * (1 - d * ts.sl_frac)
            if ts.stop is None or (d > 0 and cand > ts.stop) or (d < 0 and cand < ts.stop):
                ts.stop = cand
                return None, {}, [Intent("stop", ts.token, stop=cand, reason="trail")], ""
        return None, {}, [], ""

    # -- what the dashboard shows --------------------------------------------------------------------
    def snapshot(self) -> dict:
        st = self.state
        cand, dur = self.fsa.max_wait(st["current"])
        return {"current": st["current"], "path": list(st["path"]), "memory": jsonable(st["memory"]),
                "waited": st["waited"], "max_wait": cand if cand else (str(dur) if dur is not None else None),
                "entered_at": st["entered_at"], "trade": jsonable(st["trade"]), "trades": st["trades"]}
