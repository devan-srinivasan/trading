"""End-to-end: deployment -> FSA -> backtest (fills) -> ledger (metrics) -> save."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .analytics.ledger import LedgerReport, ledger_report
from .config import Deployment, load_strategy
from .data import DataSource
from .fsa import FSA, BacktestResult, Trade, backtest, verify_causal
from .fsa.node import _check_pct
from .store import RunStore

log = logging.getLogger("dagtrade.run")
PCT_ACCOUNT_CASH = 10_000.0          # starting cash of a backtest whose Trade sizes orders as % of the account


class Cancelled(Exception):
    """A backtest was stopped by the user (see run_deployment's `cancel`)."""


@dataclass
class RunOutput:
    run_id: str
    deployment: Deployment
    result: BacktestResult             # fills, transitions, trace
    ledger: LedgerReport               # everything computed from the fills
    metrics: dict
    insights: list[str]
    meta: dict


def build_fsa(dep: Deployment, params: dict | None = None) -> tuple[FSA, dict]:
    """The deployment's strategy as an FSA, with its parameters (DEFAULTS < TOML [params] < `params`)."""
    mod = load_strategy(dep.strategy, root=dep.path.parent.parent.parent if dep.path else None)
    p = {**getattr(mod, "DEFAULTS", {}), **dep.params, **(params or {})}
    fsa = mod.build(**p)
    if not isinstance(fsa, FSA):
        raise TypeError(f"strategies/{dep.strategy}/strategy.py: build() must return an FSA "
                        f"(e.g. FSA.chain(entry, Trade(...))), got {type(fsa).__name__}")
    fsa.name, fsa.params = dep.strategy, p
    if dep.size is not None:                          # % of the account per order, for any strategy
        for n in fsa.nodes.values():
            if isinstance(n, Trade):
                n.size = n.params["size"] = _check_pct(float(dep.size))
    if dep.flat_by_close:                             # checked here so a bad combination fails before any work
        from . import tf as tfm
        _, base = fsa.resolve_tfs(dep.base_tf)
        if not tfm.is_intraday(base):
            raise ValueError(f"flat by close needs intraday candles (e.g. 5m, 1h), not {base}")
    return fsa, p


def _trade_states(fsa: FSA) -> list[Trade]:
    return [n for n in fsa.nodes.values() if isinstance(n, Trade)]


def _daily_close(data: DataSource, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    df = data.bars(symbol, "1d", start - pd.Timedelta(days=10), end)
    s = df["close"].copy()
    s.index = pd.DatetimeIndex(s.index.tz_convert("UTC").tz_localize(None).normalize())
    return s


def insights_for(m: dict, dep: Deployment) -> list[str]:
    out = []
    t, s = m["trades"], m["strategy"]
    if t["n_trades"] < 30:
        out.append(f"Only {t['n_trades']} trades - too few to trust any statistic here (aim for 100+).")
    bh = m.get("buy_hold", {})
    if bh and s.get("total_return") is not None and s["total_return"] < bh["total_return"]:
        if s["sharpe"] >= bh["sharpe"]:
            out.append(f"Lower return than buy & hold {dep.ticker} ({s['total_return']:+.1%} vs "
                       f"{bh['total_return']:+.1%}) but better risk-adjusted (Sharpe {s['sharpe']:.2f} vs "
                       f"{bh['sharpe']:.2f}) - it is simply invested less of the time.")
        else:
            out.append(f"Worse than just buying and holding {dep.ticker} on both return ({s['total_return']:+.1%} vs "
                       f"{bh['total_return']:+.1%}) and Sharpe ({s['sharpe']:.2f} vs {bh['sharpe']:.2f}).")
    sp = m.get("split")
    if sp and sp["in_sample"].get("sharpe") is not None and sp["holdout"].get("sharpe") is not None:
        a, b = sp["in_sample"]["sharpe"], sp["holdout"]["sharpe"]
        if a > 0.5 and b < a * 0.5:
            out.append(f"Holdout Sharpe ({b:.2f}) is much weaker than in-sample ({a:.2f}) - classic overfitting sign.")
    bl = m.get("baseline")
    if bl and bl["percentile"] < 75:
        out.append(f"Beat only {bl['percentile']:.0f}% of random-timing runs with the same exposure - "
                   f"the edge may be luck or just market exposure.")
    if s.get("max_drawdown", 0) < -0.3:
        out.append(f"Max drawdown {s['max_drawdown']:.0%}: would you really sit through that?")
    rel = m.get("vs_spy", {})
    if rel.get("beta") is not None and rel["beta"] > 0.85 and rel.get("alpha", 0) < 0.02:
        out.append(f"Beta to SPY is {rel['beta']:.2f} with little alpha - mostly riding the market.")
    return out


def _as_series_kind(s: pd.Series):
    """Classify one output column: ('bool'|'num'|'cat', series, labels) or None if it cannot be drawn."""
    nonnull = s.dropna()
    if not len(nonnull):
        return None
    vals = list(nonnull)
    if all(isinstance(v, (bool, np.bool_)) for v in vals):
        return "bool", s.map(lambda v: bool(v) if v is not None and v == v else False).astype(bool), None
    if all(isinstance(v, (int, float, np.number)) and not isinstance(v, (bool, np.bool_)) for v in vals):
        return "num", pd.to_numeric(s, errors="coerce").astype(np.float32), None
    if all(isinstance(v, str) for v in vals) and len(set(vals)) <= 8:           # regime labels etc.
        labels = sorted(set(vals))
        return "cat", s.map(lambda v, L=labels: float(L.index(v)) if isinstance(v, str) else np.nan), labels
    return None


def node_frame(fsa: FSA, trace: pd.DataFrame, close: pd.Series) -> tuple[pd.DataFrame, dict]:
    """What the FSA was doing on every candle, as plottable columns: the active state (a pane), every state's output
    while it is remembered (`state.key`; prices are drawn on the chart), and the open position's stop / take."""
    cols, info = {}, {}
    med_close = float(np.nanmedian(close.to_numpy())) if len(close) else 1.0
    idx = trace.index

    def add(name, node_name, kind, vals, labels=None, place=None):
        if len(cols) >= 80:
            return
        med = float(np.nanmedian(vals.to_numpy(dtype=float))) if kind == "num" and vals.notna().any() else 0.0
        place = place or ("pane" if kind in ("bool", "cat") else
                          "overlay" if med_close and 0.3 < med / med_close < 3.0 else "pane")
        cols[name] = vals.to_numpy()
        info[name] = {"node": node_name, "kind": kind, "place": place, "main": True}
        if labels:
            info[name]["labels"] = labels

    order = list(fsa.order)
    if len(trace):
        add("fsa.state", "fsa", "cat", trace["state"].map(lambda v: float(order.index(v)) if v in order else np.nan),
            labels=order)
    rows = list(trace["values"]) if len(trace) else []
    for name in order:
        if isinstance(fsa.nodes[name], Trade) or fsa.nodes[name].plot == "hidden":
            continue
        outs = [(r.get(name) if isinstance(r.get(name), dict) else {}) for r in rows]
        keys = list(dict.fromkeys(k for d in outs for k in d))
        for k in keys:
            got = _as_series_kind(pd.Series([d.get(k) for d in outs], index=idx, dtype=object))
            if got and got[0] != "bool":                  # a remembered output exists only after success: flags
                add(f"{name}.{k}", name, got[0], got[1], got[2])      # like "buy" are always true there
    for k in ("stop", "take"):
        if len(trace) and trace[k].notna().any():
            add(f"trade.{k}", "trade", "num", trace[k].astype(float), place="overlay")
    return pd.DataFrame(cols, index=idx), info


def run_deployment(dep: Deployment, data: DataSource | None = None, store: RunStore | None = None,
                   params: dict | None = None, save: bool = True, cache: bool = True,
                   check_causal: bool = False, run_id: str | None = None,
                   extra_meta: dict | None = None, progress=None, cancel=None) -> RunOutput:
    """progress: callback(stage, done, total). cancel: a threading.Event; once set, the run stops at the next
    check (between download chunks, every 500 candles of the replay, before scoring, before saving) by raising
    Cancelled, and nothing is saved. (`cache` is accepted for compatibility; FSA replays are not cached.)"""
    import time as _time
    t0 = _time.time()
    data = data or DataSource()
    if cancel is not None:
        def check():
            if cancel.is_set():
                raise Cancelled(f"backtest {dep.label} cancelled")

        def progress(name, done, total, _inner=progress):
            check()
            if _inner:
                _inner(name, done, total)
    else:
        def check():
            return None
    data.on_chunk = progress
    fsa, p = build_fsa(dep, params)
    start, end = dep.start_ts(), dep.end_ts()
    log.info("backtest %s start %s -> %s interval=%s params=%s", dep.label, start.date(), end.date(),
             dep.base_tf or "default", p)
    cfg = dep.sim_config()
    trades_ = _trade_states(fsa)
    if "capital" not in dep.sim:
        # fixed $ orders: the order size is the money at work; % orders: a real account, like a local live DAG
        cfg.capital = PCT_ACCOUNT_CASH if any(t.size for t in trades_) else max(t.notional for t in trades_)
    kw = dict(base_tf=dep.base_tf, cfg=cfg, inputs=dep.inputs, flat_by_close=dep.flat_by_close,
              start_at=dep.start_at, label=dep.label, data_start=dep.data_start(fsa))
    res = backtest(fsa, data, dep.ticker, start, end, progress=progress, **kw)
    check()
    bars, base_tf = res.bars, res.base_tf
    s_idx = res.start_idx
    if len(bars) - s_idx < 5:
        raise ValueError(f"no bars at/after start {dep.start} for {dep.ticker} - check dates and data availability")
    # Results come ONLY from the fills (the same ledger the live dashboard uses).
    bench_close = {}
    for key, sym in (("buy_hold", dep.ticker), ("spy", dep.benchmark)):
        try:
            bench_close[key] = _daily_close(data, sym, start, end)
        except Exception as e:  # noqa: BLE001
            log.warning("benchmark %s unavailable: %s", sym, e)
    led = ledger_report(res.fills, bars, cfg.capital, base_tf, start=start, benchmarks=bench_close,
                        holdout=dep.holdout, cost_bps=2 * (cfg.commission_bps + cfg.slippage_bps))
    metrics = led.metrics
    tnames = {t.name for t in trades_}
    metrics["fsa"] = {"transitions": len(res.events),
                      "entries_not_filled": sum(1 for e in res.events if e["from"] in tnames and e["outcome"] == "fail"),
                      "max_wait_expiries": sum(1 for e in res.events if e["outcome"] == "max_wait")}
    insights = insights_for(metrics, dep)

    causal_msg = None
    if check_causal:
        rep = verify_causal(lambda: build_fsa(dep, params)[0], data, dep.ticker, start, end, **kw)
        causal_msg = str(rep)
        if not rep.ok:
            insights.insert(0, causal_msg)

    now = datetime.now(timezone.utc)
    run_id = run_id or f"{dep.strategy}_{dep.ticker}_{base_tf}_{now.strftime('%Y%m%d-%H%M%S')}"
    nodes_df, node_info = node_frame(fsa, res.trace, bars["close"].iloc[s_idx:])
    meta = {"id": run_id, "strategy": dep.strategy, "ticker": dep.ticker, "created": now.isoformat(),
            "start": dep.start, "end": str(end.date()), "base_tf": base_tf, "flat_by_close": dep.flat_by_close,
            "size": next((t.size for t in trades_ if t.size), None), "capital": cfg.capital, "params": p,
            "inputs": dep.inputs, "start_at": dep.start_at, "benchmark": dep.benchmark,
            "sim": cfg.__dict__, "holdout": dep.holdout, "metrics": metrics, "insights": insights,
            "dag": fsa.describe(base_tf), "node_info": node_info, "causal": causal_msg,
            "events": [{**e, "t": int(e["t"])} for e in res.events[-2000:]],
            "description": getattr(load_strategy(dep.strategy, root=dep.path.parent.parent.parent if dep.path else None),
                                   "DESCRIPTION", "")}
    meta.update(extra_meta or {})
    out = RunOutput(run_id, dep, res, led, metrics, insights, meta)
    s_ = metrics["strategy"]
    log.info("backtest %s done in %.1fs: return %+.2f%% sharpe %.2f maxDD %.1f%% trades %d transitions %d%s%s",
             dep.label, _time.time() - t0, 100 * s_.get("total_return", 0), s_.get("sharpe", 0),
             100 * s_.get("max_drawdown", 0), metrics["trades"]["n_trades"], len(res.events),
             f" -> saved {run_id}" if save else "", f" | {causal_msg.splitlines()[0]}" if causal_msg else "")
    check()
    if save:
        store = store or RunStore()
        lo = max(s_idx - 100, 0)
        frames = {
            "bars": bars.iloc[lo:][["open", "high", "low", "close", "volume"]],
            "equity": pd.DataFrame({"equity": led.equity, "exposure": led.exposure}),
            "daily": led.daily,
            "trades": led.trades, "fills": res.fills,
            "nodes": nodes_df.reindex(bars.index[lo:]),       # same rows as the plotted candles
        }
        store.save(run_id, meta, frames)
    return out
