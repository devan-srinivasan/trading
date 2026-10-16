"""Turn a saved run into the JSON the dashboard draws."""
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


def run_payload(store: RunStore, run_id: str) -> dict:
    meta = store.meta(run_id)
    bars = store.frame(run_id, "bars")
    trades = store.frame(run_id, "trades")
    nodes = store.frame(run_id, "nodes")
    daily = store.frame(run_id, "daily")
    eqf = store.frame(run_id, "equity")

    t = _secs(bars.index)
    chart = {"t": t, "o": _col(bars["open"], 4), "h": _col(bars["high"], 4), "l": _col(bars["low"], 4),
             "c": _col(bars["close"], 4), "v": _col(bars["volume"], 0)}

    show_text = len(trades) <= 25
    markers = []
    for r in trades.itertuples():
        long_ = r.side == "long"
        markers.append({"time": int(r.entry_time.timestamp()), "kind": "entry", "side": r.side,
                        "text": ("L" if long_ else "S") if show_text else ""})
        markers.append({"time": int(r.exit_time.timestamp()), "kind": "exit", "side": r.side, "win": bool(r.pnl > 0),
                        "text": f"{r.ret:+.1%} {r.exit_reason}" if show_text else ""})
    markers.sort(key=lambda m: m["time"])
    chart["markers"] = markers

    info = meta.get("node_info", {})
    overlays, panes = {}, {}
    for c in nodes.columns:
        i = info.get(c, {"place": "pane", "kind": "num", "node": c, "main": True})
        series = {"label": c, "node": i["node"], "kind": i["kind"], "main": i["main"], "v": _col(nodes[c], 5),
                  "labels": i.get("labels")}
        (overlays if i["place"] == "overlay" else panes)[c] = series
    chart["overlays"], chart["panes"] = overlays, panes

    try:                                                   # real paper-account fills (live runs only)
        lf = store.frame(run_id, "live_fills")
        snapped = []
        if len(lf) and len(bars):
            pos = np.clip(bars.index.searchsorted(pd.DatetimeIndex(lf["time"]), side="right") - 1, 0, None)
            for r, bi in zip(lf.itertuples(), pos):
                snapped.append({"time": int(bars.index[bi].timestamp()), "side": r.side, "qty": float(r.qty),
                                "price": float(r.price), "kind": r.kind, "at": int(pd.Timestamp(r.time).timestamp())})
        chart["live_fills"] = snapped
    except FileNotFoundError:
        chart["live_fills"] = []

    eq = {"t": _secs(daily.index)}
    for c in daily.columns:
        eq[c] = _col(daily[c], 2)
    d = daily["strategy"]
    eq["drawdown"] = _col(d / d.cummax() - 1, 5)
    chart["equity"] = eq

    tr = trades.copy()
    for c in ("entry_time", "exit_time"):
        tr[c] = _secs(pd.DatetimeIndex(tr[c])) if len(tr) else []
    tr_rows = [{k: (None if (isinstance(v, float) and not np.isfinite(v)) else v) for k, v in row.items()}
               for row in tr.to_dict("records")]
    return _clean({"meta": meta, "trades": tr_rows, "chart": chart})
