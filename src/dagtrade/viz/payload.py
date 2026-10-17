"""Turn saved data into the JSON the dashboard draws.

    run_payload(store, id)   a BACKTEST result: metrics, equity vs benchmarks, chart, simulated trades
    live_payload(...)        a LIVE DAG: live candles, the DAG's node outputs, and the account's REAL trades
Both share chart_payload(), so the charts look the same; the data never mixes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..store import RunStore


def _col(s: pd.Series, nd: int | None = None) -> list:
    a = s.to_numpy()
    if a.dtype == bool:
        return a.astype(int).tolist()
    a = a.astype(float)
    if nd is not None:
        a = np.round(a, nd)
    out = a.astype(object)
    out[~np.isfinite(a)] = None
    return out.tolist()


def _secs(idx: pd.DatetimeIndex) -> list[int]:
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return (idx.tz_convert("UTC").astype("int64") // 10**9).tolist()


def _clean(o):
    """JSON has no NaN/Infinity: turn non-finite floats (e.g. profit factor with no losing trades) into null."""
    if isinstance(o, float):
        return o if np.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def chart_payload(bars: pd.DataFrame, trades: pd.DataFrame, nodes: pd.DataFrame, info: dict,
                  live_fills: pd.DataFrame | None = None) -> dict:
    """Candles + trade markers + node panes/overlays (+ optional real fills) for the price chart."""
    chart = {"t": _secs(pd.DatetimeIndex(bars.index)), "o": _col(bars["open"], 4), "h": _col(bars["high"], 4),
             "l": _col(bars["low"], 4), "c": _col(bars["close"], 4), "v": _col(bars["volume"], 0)}
    show_text = len(trades) <= 25
    markers = []
    for r in trades.itertuples():
        long_ = r.side == "long"
        markers.append({"time": int(pd.Timestamp(r.entry_time).timestamp()), "kind": "entry", "side": r.side,
                        "text": ("L" if long_ else "S") if show_text else ""})
        if getattr(r, "open", False):
            continue
        markers.append({"time": int(pd.Timestamp(r.exit_time).timestamp()), "kind": "exit", "side": r.side,
                        "win": bool(r.pnl > 0), "text": f"{r.ret:+.1%} {r.exit_reason}" if show_text else ""})
    markers.sort(key=lambda m: m["time"])
    chart["markers"] = markers
    overlays, panes = {}, {}
    for c in nodes.columns:
        i = info.get(c, {"place": "pane", "kind": "num", "node": c, "main": True})
        series = {"label": c, "node": i["node"], "kind": i["kind"], "main": i["main"], "v": _col(nodes[c], 5),
                  "labels": i.get("labels")}
        (overlays if i["place"] == "overlay" else panes)[c] = series
    chart["overlays"], chart["panes"] = overlays, panes
    snapped = []
    if live_fills is not None and len(live_fills) and len(bars):
        idx = pd.DatetimeIndex(bars.index)
        pos = np.clip(idx.searchsorted(pd.DatetimeIndex(pd.to_datetime(live_fills["time"], utc=True)), side="right") - 1, 0, None)
        for r, bi in zip(live_fills.itertuples(), pos):
            snapped.append({"time": int(idx[bi].timestamp()), "side": r.side, "qty": float(r.qty), "price": float(r.price)})
    chart["live_fills"] = snapped
    return chart


def _trade_rows(trades: pd.DataFrame) -> list[dict]:
    tr = trades.copy()
    for c in ("entry_time", "exit_time"):
        if c in tr:
            tr[c] = _secs(pd.DatetimeIndex(pd.to_datetime(tr[c], utc=True))) if len(tr) else []
    return [{k: (None if (isinstance(v, float) and not np.isfinite(v)) else v) for k, v in row.items()}
            for row in tr.to_dict("records")]


def run_payload(store: RunStore, run_id: str) -> dict:
    """A backtest result."""
    meta = store.meta(run_id)
    bars, trades = store.frame(run_id, "bars"), store.frame(run_id, "trades")
    nodes, daily = store.frame(run_id, "nodes"), store.frame(run_id, "daily")
    chart = chart_payload(bars, trades, nodes, meta.get("node_info", {}))
    eq = {"t": _secs(daily.index)}
    for c in daily.columns:
        eq[c] = _col(daily[c], 2)
    d = daily["strategy"]
    eq["drawdown"] = _col(d / d.cummax() - 1, 5)
    chart["equity"] = eq
    fills = store.frame(run_id, "fills")
    orders = [{"submitted": int(pd.Timestamp(f.time).timestamp()), "side": "buy" if f.qty > 0 else "sell",
               "qty": round(abs(float(f.qty)), 4), "type": {"stop": "stop", "take": "limit"}.get(f.reason, "market"),
               "reason": f.reason, "status": "filled", "fill_price": float(f.price), "fee": float(f.commission),
               "stop": float(f.price) if f.reason == "stop" else None, "limit": float(f.price) if f.reason == "take" else None}
              for f in fills.itertuples()][::-1]
    return _clean({"meta": meta, "trades": _trade_rows(trades), "chart": chart, "orders": orders})


# ---- live -------------------------------------------------------------------------------------------
def live_trades(fills: list[dict], bars: pd.DataFrame | None) -> pd.DataFrame:
    """Round trips from the account's REAL fills (flat -> flat), plus the open position as an unfinished trade."""
    cols = ["entry_time", "exit_time", "side", "qty", "entry_price", "exit_price", "pnl", "ret", "bars_held",
            "exit_reason", "mae", "mfe", "open"]
    rows, cur, pos = [], None, 0.0
    for f in sorted(fills, key=lambda x: x["time"]):
        q = f["qty"] if f["side"] == "buy" else -f["qty"]
        t = pd.Timestamp(f["time"], unit="s", tz="UTC") if isinstance(f["time"], (int, float)) else pd.Timestamp(f["time"])
        if cur is None:
            cur = {"entry_time": t, "side": "long" if q > 0 else "short", "in_q": 0.0, "in_v": 0.0, "out_q": 0.0,
                   "out_v": 0.0, "fees": 0.0, "reason": ""}
        if (q > 0) == (cur["side"] == "long"):
            cur["in_q"] += abs(q)
            cur["in_v"] += abs(q) * f["price"]
        else:
            cur["out_q"] += abs(q)
            cur["out_v"] += abs(q) * f["price"]
            cur["reason"] = f.get("reason") or f.get("role") or "exit"
        cur["fees"] += f.get("fee") or 0.0
        pos += q
        if abs(pos) < 1e-9:
            sgn = 1 if cur["side"] == "long" else -1
            ep, xp = cur["in_v"] / cur["in_q"], cur["out_v"] / cur["out_q"]
            pnl = sgn * (cur["out_v"] - cur["in_v"]) - cur["fees"]
            rows.append({"entry_time": cur["entry_time"], "exit_time": t, "side": cur["side"], "qty": cur["in_q"],
                         "entry_price": ep, "exit_price": xp, "pnl": pnl, "ret": pnl / cur["in_v"],
                         "exit_reason": cur["reason"], "open": False})
            cur, pos = None, 0.0
    if cur is not None and cur["in_q"] > 0 and bars is not None and len(bars):        # still holding
        sgn = 1 if cur["side"] == "long" else -1
        ep, last = cur["in_v"] / cur["in_q"], float(bars["close"].iloc[-1])
        pnl = sgn * (last - ep) * abs(pos) - cur["fees"]
        rows.append({"entry_time": cur["entry_time"], "exit_time": pd.DatetimeIndex(bars.index)[-1], "side": cur["side"],
                     "qty": abs(pos), "entry_price": ep, "exit_price": last, "pnl": pnl, "ret": pnl / cur["in_v"],
                     "exit_reason": "open", "open": True})
    df = pd.DataFrame(rows, columns=[c for c in cols if c not in ("bars_held", "mae", "mfe")])
    if len(df) and bars is not None and len(bars):
        idx = pd.DatetimeIndex(bars.index)
        snap = lambda s: idx[np.clip(idx.searchsorted(pd.DatetimeIndex(s), side="right") - 1, 0, None)]   # noqa: E731
        df["entry_time"], df["exit_time"] = snap(df["entry_time"]), snap(df["exit_time"])
        df["bars_held"] = idx.searchsorted(df["exit_time"]) - idx.searchsorted(df["entry_time"])
    for c in ("bars_held", "mae", "mfe"):
        if c not in df:
            df[c] = np.nan
    return df[cols]


def live_payload(chart_src: dict | None, fills: list[dict]) -> dict:
    """chart_src: what the runner saved for this DAG (recent candles + node outputs). fills: the account's fills."""
    if not chart_src:
        return {"chart": None, "trades": []}
    bars = pd.DataFrame(chart_src["bars"]).set_index(pd.to_datetime(chart_src["t"], unit="s", utc=True))
    nodes = pd.DataFrame(chart_src["nodes"], index=bars.index)
    trades = live_trades(fills, bars)
    window_trades = trades[trades["exit_time"] >= bars.index[0]] if len(trades) else trades
    fills_df = pd.DataFrame([{"time": pd.Timestamp(f["time"], unit="s", tz="UTC") if isinstance(f["time"], (int, float))
                              else pd.Timestamp(f["time"]), "side": f["side"], "qty": f["qty"], "price": f["price"]}
                             for f in fills]) if fills else None
    chart = chart_payload(bars, window_trades, nodes, chart_src.get("info", {}), fills_df)
    return _clean({"chart": chart, "trades": _trade_rows(trades), "base_tf": chart_src.get("base_tf"),
                   "updated": chart_src.get("updated")})
