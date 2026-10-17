"""Backtest an FSA on historical candles: the same loop the live scheduler runs, replayed candle by candle.

For every candle of the FSA's interval, from the start date:
    1. the paper broker processes the candle (orders sent at the previous close fill at its open; exit legs are
       checked against its high/low)
    2. the FSA steps at the candle's close (the active state looks at its candles, maybe moves on)
    3. the planner turns the Trade state's intents into orders (sent now, filled at the next open)
The result is a list of FILLS (plus what each state was doing, for the chart). Every metric is computed from those
fills by analytics.ledger, exactly as for live trading.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..sim.broker import SimConfig
from ..sim.paper import PaperBroker
from .candles import CandleView
from .machine import FSA
from .node import Account
from .planner import plan, slug
from .runner import FsaRunner


@dataclass
class BacktestResult:
    bars: pd.DataFrame                  # candles of the FSA's interval (open-time index), incl. warm-up
    fills: pd.DataFrame                 # time, qty (signed), price, reason, commission
    events: list                        # state transitions: {t, from, to, outcome, reason}
    trace: pd.DataFrame                 # per candle from the start: state + each state's output (for the chart)
    equity: pd.Series                   # the broker's own mark-to-market (for checks; metrics use the ledger)
    base_tf: str
    start_idx: int
    broker: PaperBroker = field(repr=False, default=None)


def _orders(broker: PaperBroker) -> list:
    out = []
    for o in broker.working():
        parts = o["client_id"].split(".")
        out.append(SimpleNamespace(active=True, id=o["id"], type=o["type"], client_id=o["client_id"],
                                   kind=parts[2] if len(parts) >= 4 and parts[0] == "dt" else "foreign"))
    return out


def execute(broker: PaperBroker, acts, at: float) -> None:
    """Carry out planned actions on a paper broker."""
    for a in acts:
        if a.kind == "entry":
            broker.submit_entry(a.side, a.qty, a.client_id, at, stop=a.stop, take=a.take, limit=a.limit)
        elif a.kind == "close":
            broker.submit_market(a.side, a.qty, a.client_id, at, why=a.reason or "close")
        elif a.kind == "cancel":
            broker.cancel(a.order_id)
        elif a.kind == "replace_stop":
            broker.replace_stop(a.order_id, a.stop)


def backtest(fsa: FSA, data, ticker: str, start, end, *, base_tf: str | None = None, cfg: SimConfig | None = None,
             inputs: dict | None = None, flat_by_close: bool = False, start_at: str | None = None,
             start_memory: dict | None = None, until: pd.Timestamp | None = None, data_start=None,
             progress=None, label: str = "backtest") -> BacktestResult:
    cfg = cfg or SimConfig()
    tfs, base = fsa.resolve_tfs(base_tf)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    data_start = pd.Timestamp(data_start) if data_start is not None else start - fsa.required_history(start, base)
    view = CandleView(data, ticker, data_start, end, [base, *tfs.values()], until=until)
    bars = view.frame(base)
    n = len(bars)
    j0 = int(bars.index.searchsorted(start))
    broker = PaperBroker(cfg.capital, cfg.commission_bps, cfg.slippage_bps, whole_shares=cfg.whole_shares)
    runner = FsaRunner(fsa, inputs={"cash": cfg.capital, **(inputs or {})}, flat_by_close=flat_by_close, base_tf=base)
    runner.reset(start_at, start_memory)
    pre = f"dt.{slug(label)}"
    o_, h_, l_, c_ = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    t_open = bars.index.asi8 / 1e9
    av_ns = pd.DatetimeIndex(bars["avail"]).asi8
    t_av = av_ns / 1e9
    equity = np.full(n, np.nan)
    states, values, stops, takes = [], [], [], []
    outstanding: list = []
    status = lambda tok: broker.status(f"{pre}.ent.{tok}")      # noqa: E731
    # the first decision is made at the close of the candle BEFORE the start, so the first fill can be at the start
    # candle's open (what buying and holding from the start date means)
    for j in range(max(j0 - 1, 0), n):
        broker.process(t_open[j], t_av[j], o_[j], h_[j], l_[j], c_[j])
        acct = Account(cash=broker.cash, equity=broker.equity(c_[j]), position=broker.qty, last_price=c_[j],
                       order_status=status)
        new = runner.step(pd.Timestamp(av_ns[j], tz="UTC"), view, acct)
        if not cfg.allow_short:
            new = [i for i in new if not (i.kind == "entry" and i.side == "sell")]
        outstanding += new
        for _ in range(3):                                # paper cancels are instant: cancel legs, then close
            if not outstanding:
                break
            acts, _notes, outstanding = plan(outstanding, broker.qty, _orders(broker), label, t_av[j],
                                             equity=broker.equity(c_[j]), cash=broker.cash,
                                             whole_shares=cfg.whole_shares)
            execute(broker, acts, t_av[j])
            if not acts:
                break
        equity[j] = broker.equity(c_[j])
        if j < j0:
            continue
        states.append(runner.current)
        values.append(dict(runner.state["memory"]))
        stp = tak = np.nan
        for od in broker.working():
            if od["role"] == "stop":
                stp = od["stop_price"]
            elif od["role"] == "take":
                tak = od["limit_price"]
        stops.append(stp)
        takes.append(tak)
        if progress and (j - j0) % 500 == 0:
            progress("fsa", j - j0, n - j0)
    if n:
        broker.flatten(at=t_open[n - 1], reason="end", px=c_[n - 1])
        equity[n - 1] = broker.equity(c_[n - 1])
    if progress:
        progress("fsa", n - j0, n - j0)
    fl = broker.s["fills"]
    fills = pd.DataFrame({"time": pd.to_datetime([f["time"] for f in fl], unit="s", utc=True),
                          "qty": [f["qty"] * (1 if f["side"] == "buy" else -1) for f in fl],
                          "price": [f["price"] for f in fl], "reason": [f["reason"] for f in fl],
                          "commission": [f["fee"] for f in fl]})
    idx = bars.index[j0:]
    trace = pd.DataFrame({"state": states, "values": values, "stop": stops, "take": takes}, index=idx)
    return BacktestResult(bars, fills, runner.events, trace, pd.Series(equity, index=bars.index, name="equity"),
                          base, j0, broker)
