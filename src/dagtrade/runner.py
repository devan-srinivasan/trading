"""End-to-end: deployment -> graph -> simulate -> analyse -> save."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import tf as tfm
from .analytics import daily_equity, equity_stats, random_timing_baseline, relative_stats, split_stats, trade_stats
from .config import Deployment, load_strategy
from .data import DataSource
from .graph import Graph, GraphResult, asof_align, verify_causal
from .sim import SimResult, simulate
from .store import RunStore

log = logging.getLogger("dagtrade.run")


@dataclass
class RunOutput:
    run_id: str
    deployment: Deployment
    graph_result: GraphResult
    sim: SimResult
    metrics: dict
    insights: list[str]
    meta: dict


def build_graph(dep: Deployment, params: dict | None = None) -> tuple[Graph, dict]:
    mod = load_strategy(dep.strategy, root=dep.path.parent.parent.parent if dep.path else None)
    p = {**getattr(mod, "DEFAULTS", {}), **dep.params, **(params or {})}
    g = mod.build(**p)
    g.name = dep.strategy
    g.params = p
    return g, p


def _daily_close(data: DataSource, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    df = data.bars(symbol, "1d", start - pd.Timedelta(days=10), end)
    s = df["close"].copy()
    s.index = pd.DatetimeIndex(s.index.tz_convert("UTC").tz_localize(None).normalize())
    return s


def _rebase(series: pd.Series, dates: pd.DatetimeIndex, capital: float) -> pd.Series:
    """Buy-and-hold curve on `dates`, starting at `capital` (uses the last close before the first date as entry)."""
    s = series.reindex(series.index.union(dates)).ffill()
    before = series[series.index < dates[0]]
    base = before.iloc[-1] if len(before) else s.loc[dates[0]]
    return (s.loc[dates] / base * capital).rename(series.name)


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


def node_frame(gr: GraphResult, base_grid: pd.DatetimeIndex, close: pd.Series) -> tuple[pd.DataFrame, dict]:
    """Every node's structured output flattened to plottable columns (`node.key`) on the base candle grid."""
    cols, info = {}, {}
    med_close = float(np.nanmedian(close.to_numpy())) if len(close) else 1.0

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

    for n in gr.order:
        nm = gr.names[id(n)]
        if n.kind == "trade" or n.plot == "hidden":
            continue
        out = asof_align(gr.out(n), gr.ctx.grid(gr.tfs[id(n)]), base_grid)
        rows = [d if isinstance(d, dict) else {} for d in out]
        keys = list(dict.fromkeys(k for d in rows for k in d))
        for k in keys:
            col = pd.Series([d.get(k) for d in rows], index=base_grid, dtype=object)
            got = _as_series_kind(col)
            if got:
                add(f"{nm}.{k}", nm, got[0], got[1], got[2])
    t = gr.intents                                          # the Trade node's stop / take-profit levels
    for k in ("stop", "take"):
        if t[k].notna().any():
            add(f"trade.{k}", gr.names[id(gr.graph.trader)], "num", t[k], place="overlay")
    return pd.DataFrame(cols, index=base_grid), info


def run_deployment(dep: Deployment, data: DataSource | None = None, store: RunStore | None = None,
                   params: dict | None = None, save: bool = True, cache: bool = True,
                   check_causal: bool = False, run_id: str | None = None,
                   extra_meta: dict | None = None) -> RunOutput:
    data = data or DataSource()
    graph, p = build_graph(dep, params)
    start, end = dep.start_ts(), dep.end_ts()
    data_start = start - max(pd.Timedelta(days=dep.warmup_days), graph.required_history(start))
    gr = graph.run(data, dep.ticker, data_start, end, base_tf=dep.base_tf, cache=cache)
    base_tf = gr.base_tf
    bars, intents = gr.base_bars, gr.intents
    assert len(bars) == len(intents)
    cfg = dep.sim_config()
    if "capital" not in dep.sim:
        cfg.capital = graph.trader.notional           # compare like with like: the order size is the money at work
    sim = simulate(bars, intents, cfg, trade_from=start)

    s_idx = int(bars.index.searchsorted(start))
    eq = sim.equity.iloc[s_idx:]
    if len(eq) < 5:
        raise ValueError(f"no bars at/after start {dep.start} for {dep.ticker} - check dates and data availability")
    exposure = sim.exposure.iloc[s_idx:]
    trades = sim.trades

    ed = daily_equity(eq, base_tf)
    first = ed.index[0] - pd.Timedelta(days=1)
    ed = pd.concat([pd.Series({first: cfg.capital}), ed]).rename("strategy")
    ed.index = pd.DatetimeIndex(ed.index)
    metrics: dict = {"strategy": equity_stats(ed), "trades": trade_stats(trades, exposure)}

    # benchmarks over the same dates
    bench = {}
    for key, sym in (("buy_hold", dep.ticker), ("spy", dep.benchmark)):
        try:
            b = _rebase(_daily_close(data, sym, start, end), ed.index[1:], cfg.capital)
            b = pd.concat([pd.Series({first: cfg.capital}), b])
            bench[key] = b.reindex(ed.index).ffill().rename(key)
            metrics[key] = equity_stats(bench[key])
        except Exception as e:  # noqa: BLE001
            log.warning("benchmark %s unavailable: %s", sym, e)
    if "spy" in bench:
        metrics["vs_spy"] = relative_stats(ed, bench["spy"])
    if "buy_hold" in bench:
        metrics["vs_buy_hold"] = relative_stats(ed, bench["buy_hold"])
    metrics["split"] = split_stats(ed, trades, dep.holdout, base_tf)
    close_eval = bars["close"].iloc[s_idx:]
    metrics["baseline"] = random_timing_baseline(close_eval, exposure, metrics["strategy"].get("total_return", 0.0),
                                                 cost_bps=2 * (cfg.commission_bps + cfg.slippage_bps))
    insights = insights_for(metrics, dep)

    causal_msg = None
    if check_causal:
        rep = verify_causal(graph, data, dep.ticker, data_start, end, base_tf=dep.base_tf)
        causal_msg = str(rep)
        if not rep.ok:
            insights.insert(0, causal_msg)

    now = datetime.now(timezone.utc)
    run_id = run_id or f"{dep.strategy}_{dep.ticker}_{now.strftime('%Y%m%d-%H%M%S')}"
    base_grid = pd.DatetimeIndex(bars["avail"])
    nodes_df, node_info = node_frame(gr, base_grid, bars["close"])
    nodes_df.index = bars.index                       # plot values at the bar they refer to
    meta = {"id": run_id, "strategy": dep.strategy, "ticker": dep.ticker, "created": now.isoformat(),
            "start": dep.start, "end": str(end.date()), "base_tf": base_tf, "params": p, "benchmark": dep.benchmark,
            "sim": dep.sim_config().__dict__, "holdout": dep.holdout, "metrics": metrics, "insights": insights,
            "dag": graph.describe(dep.base_tf), "node_info": node_info, "causal": causal_msg,
            "description": getattr(load_strategy(dep.strategy, root=dep.path.parent.parent.parent if dep.path else None),
                                   "DESCRIPTION", "")}
    meta.update(extra_meta or {})
    out = RunOutput(run_id, dep, gr, sim, metrics, insights, meta)
    if save:
        store = store or RunStore()
        plot_bars = bars.iloc[max(s_idx - 100, 0):][["open", "high", "low", "close", "volume"]]
        frames = {
            "bars": plot_bars,
            "equity": pd.DataFrame({"equity": sim.equity.iloc[s_idx:], "exposure": exposure}),
            "daily": pd.concat([ed] + list(bench.values()), axis=1),
            "trades": trades, "fills": sim.fills,
            "nodes": nodes_df.iloc[max(s_idx - 100, 0):],
        }
        store.save(run_id, meta, frames)
    return out
