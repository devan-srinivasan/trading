"""Graph execution: topological order, timeframe resolution, as-of alignment, caching, causality verification."""
from __future__ import annotations

import hashlib
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .. import tf as tfm
from ..data.source import project_root
from .node import Node, Trade

log = logging.getLogger("dagtrade.graph")


class Context:
    """What a node can see while computing."""

    def __init__(self, data, ticker: str, start, end, until: pd.Timestamp | None = None, live: bool = False):
        self.data, self.ticker, self.start, self.end = data, ticker, pd.Timestamp(start), pd.Timestamp(end)
        self.until = until            # causality tests: hide any bar not fully formed by this time
        self.live = live              # True when driving the real-time runner (the newest row is "just closed")
        self._bars: dict[str, pd.DataFrame] = {}
        self.index: pd.DatetimeIndex = pd.DatetimeIndex([])   # set per node by the executor
        self.tf: str | None = None

    def bars(self, tf: str) -> pd.DataFrame:
        """Full bars frame at `tf` (index = bar OPEN time, `avail` column). Re-indexed by avail for nodes."""
        if tf not in self._bars:
            df = self.data.bars(self.ticker, tf, self.start, self.end)
            if self.until is not None:
                df = df[df["avail"] <= self.until]
            self._bars[tf] = df
        raw = self._bars[tf]
        out = raw.set_index("avail")
        out.index.name = None
        return out

    def raw_bars(self, tf: str) -> pd.DataFrame:
        self.bars(tf)
        return self._bars[tf]

    def grid(self, tf: str) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.raw_bars(tf)["avail"])

    def fingerprint(self, base_tf: str) -> str:
        df = self.raw_bars(base_tf)
        h = hashlib.sha1(pd.util.hash_pandas_object(df, index=True).to_numpy().tobytes())
        h.update(f"{self.ticker}|{self.start}|{self.end}|{self.until}".encode())
        return h.hexdigest()[:16]


def asof_align(obj, src_index: pd.DatetimeIndex, grid: pd.DatetimeIndex):
    """Value at grid time t = latest src row with src_time <= t (NaN/False before the first row)."""
    if len(src_index) == len(grid) and (src_index == grid).all():
        return obj
    pos = src_index.searchsorted(grid, side="right") - 1
    miss = pos < 0
    pos = np.where(miss, 0, pos)
    out = obj.iloc[pos].copy() if len(obj) else obj.reindex(range(len(grid)))
    out.index = grid
    if miss.any():
        if isinstance(out, pd.DataFrame):
            for c in out.columns:
                out[c] = _blank_before_start(out[c], miss)
        else:
            out = _blank_before_start(out, miss)
    return out


def _blank_before_start(s: pd.Series, miss: np.ndarray) -> pd.Series:
    """Rows before the source's first value: False for booleans, NaN for everything else."""
    if s.dtype == bool:
        s = s.copy()
        s[miss] = False
        return s
    if s.dtype == object or str(s.dtype) in ("category", "string"):   # e.g. text labels from a classifier node
        s = s.astype(object)
        s[miss] = None
        return s
    s = s.astype(float)
    s[miss] = np.nan
    return s


@dataclass
class GraphResult:
    graph: "Graph"
    base_tf: str
    order: list[Node]
    names: dict[int, str]                 # id(node) -> unique name
    outputs: dict[int, object]            # id(node) -> output on its own grid
    tfs: dict[int, str]
    ctx: Context
    cache_hits: int = 0
    intents: pd.DataFrame | None = None
    base_bars: pd.DataFrame | None = None  # raw base bars (open-time index + avail)

    def out(self, node: Node):
        return self.outputs[id(node)]

    def by_name(self, name: str):
        for n in self.order:
            if self.names[id(n)] == name:
                return self.outputs[id(n)]
        raise KeyError(name)


class Graph:
    """A strategy: a DAG of Nodes ending in a Trade node."""

    def __init__(self, trade: Trade, name: str = "strategy", base_tf: str | None = None, params: dict | None = None):
        if not isinstance(trade, Trade):
            raise TypeError("a Graph must end in a Trade node")
        self.trader, self.name, self.base_tf, self.params = trade, name, base_tf, params or {}

    # -- structure ----------------------------------------------------------------------------
    def topo(self) -> list[Node]:
        seen, order = set(), []

        def visit(n: Node):
            if id(n) in seen:
                return
            seen.add(id(n))
            for i in n.inputs:
                visit(i)
            order.append(n)

        visit(self.trader)
        return order

    def resolve_tfs(self, order: list[Node], base_tf: str | None) -> tuple[dict[int, str], str]:
        explicit = [n.tf for n in order if n.tf]
        base = base_tf or self.base_tf or (tfm.finest(explicit) if explicit else None)
        if base is None:
            raise ValueError("graph has no timeframe")
        tfs: dict[int, str] = {}
        for n in order:
            if n is self.trader:
                tfs[id(n)] = base
            elif n.tf:
                tfs[id(n)] = n.tf
            else:
                ins = [tfs[id(i)] for i in n.inputs]
                tfs[id(n)] = tfm.finest(ins) if ins else base
        return tfs, base

    def unique_names(self, order: list[Node]) -> dict[int, str]:
        names, used = {}, {}
        for n in order:
            k = used.get(n.name, 0)
            used[n.name] = k + 1
            names[id(n)] = n.name if k == 0 else f"{n.name}#{k + 1}"
        return names

    def describe(self, base_tf: str | None = None) -> dict:
        """JSON-able structure for the dashboard's DAG diagram."""
        order = self.topo()
        tfs, base = self.resolve_tfs(order, base_tf)
        names = self.unique_names(order)
        depth: dict[int, int] = {}
        for n in order:
            depth[id(n)] = 1 + max((depth[id(i)] for i in n.inputs), default=-1)
        return {"name": self.name, "base_tf": base, "nodes": [
            {"id": names[id(n)], "kind": n.kind, "tf": tfs[id(n)], "depth": depth[id(n)],
             "inputs": [names[id(i)] for i in n.inputs],
             "params": {k: v for k, v in n.params.items() if isinstance(v, (int, float, str, bool, type(None)))},
             "history": f"{n.history.start or 'all'} → {n.history.end}", "type": type(n).__name__} for n in order]}

    def required_history(self, at: pd.Timestamp) -> pd.Timedelta:
        """How far before `at` the nodes' history Ranges reach (so enough candles are loaded). Nodes whose Range
        has no start (all loaded history) do not count: the deployment's warmup_days covers those."""
        spans = [n.history.span(at) for n in self.topo()]
        spans = [x for x in spans if x is not None]
        return max(spans, default=pd.Timedelta(0)) * 1.1 + pd.Timedelta(days=5)

    def run(self, data, ticker: str, start, end, base_tf: str | None = None, cache: bool = True,
            cache_dir: Path | None = None, until: pd.Timestamp | None = None, live: bool = False) -> GraphResult:
        order = self.topo()
        tfs, base = self.resolve_tfs(order, base_tf)
        names = self.unique_names(order)
        ctx = Context(data, ticker, start, end, until=until, live=live)
        cdir = Path(cache_dir) if cache_dir else project_root() / ".cache" / "nodes"
        if cache:
            cdir.mkdir(parents=True, exist_ok=True)
        fp = ctx.fingerprint(base)
        keys: dict[int, str] = {}
        outputs: dict[int, object] = {}
        hits = 0
        for n in order:
            tf = tfs[id(n)]
            grid = ctx.grid(tf)
            key = hashlib.sha1("\x00".join([n.spec(), tf, fp, *(keys[id(i)] for i in n.inputs)]).encode()).hexdigest()[:20]
            keys[id(n)] = key
            use_cache = cache and n.cacheable
            path = cdir / f"{key}.pkl"
            if use_cache and path.exists():
                try:
                    outputs[id(n)] = pickle.loads(path.read_bytes())
                    hits += 1
                    continue
                except Exception:  # noqa: BLE001
                    path.unlink(missing_ok=True)
            ins = []
            for i in n.inputs:
                src_grid = ctx.grid(tfs[id(i)])
                ins.append(asof_align(outputs[id(i)], src_grid, grid))
            ctx.index, ctx.tf = grid, tf
            res = n.compute(ctx, *ins)
            res = self._normalise(res, grid, names[id(n)])
            outputs[id(n)] = res
            if use_cache:
                path.write_bytes(pickle.dumps(res, protocol=pickle.HIGHEST_PROTOCOL))
        gr = GraphResult(self, base, order, names, outputs, tfs, ctx, cache_hits=hits)
        gr.intents = outputs[id(self.trader)]
        gr.base_bars = ctx.raw_bars(base)
        return gr

    @staticmethod
    def _normalise(res, grid: pd.DatetimeIndex, name: str):
        if isinstance(res, np.ndarray):
            res = pd.Series(res, index=grid) if res.ndim == 1 else pd.DataFrame(res, index=grid)
        if not isinstance(res, (pd.Series, pd.DataFrame)):
            raise TypeError(f"node {name!r} returned {type(res).__name__}; expected Series/DataFrame/ndarray")
        if len(res) != len(grid):
            raise ValueError(f"node {name!r} returned {len(res)} rows but its grid has {len(grid)}")
        res = res.copy()
        res.index = grid
        return res


# -- causality verification -------------------------------------------------------------------
@dataclass
class CausalReport:
    ok: bool
    leaks: list[tuple[str, str]] = field(default_factory=list)   # (node name, first differing timestamp)
    checked: int = 0

    def __str__(self):
        if self.ok:
            return f"causal: OK ({self.checked} cutoffs checked, no node changed when the future was hidden)"
        lines = ["LOOKAHEAD DETECTED - these nodes changed when future data was hidden:"]
        lines += [f"  - {n}: first differs at {t}" for n, t in self.leaks]
        return "\n".join(lines)


def verify_causal(graph: Graph, data, ticker, start, end, base_tf=None, n_cutoffs: int = 4,
                  seed: int = 0) -> CausalReport:
    """Re-run the graph with the future hidden at several cutoffs; every node's values up to the cutoff must be
    identical to the full run. Any difference means a node peeked ahead."""
    full = graph.run(data, ticker, start, end, base_tf=base_tf, cache=False)
    grid = full.ctx.grid(full.base_tf)
    if len(grid) < 50:
        return CausalReport(True, [], 0)
    rng = np.random.default_rng(seed)
    lo, hi = int(len(grid) * 0.3), len(grid) - 2
    cuts = sorted(rng.choice(np.arange(lo, hi), size=min(n_cutoffs, hi - lo), replace=False))
    leaks: dict[str, str] = {}
    for c in cuts:
        t = grid[c]
        part = graph.run(data, ticker, start, end, base_tf=base_tf, cache=False, until=t)
        for n in full.order:
            a, b = full.out(n), part.out(n)
            sub = a.loc[a.index <= t]
            b = b.loc[b.index <= t]
            if len(sub) != len(b):
                leaks.setdefault(full.names[id(n)], str(t))
                continue
            same = _same(sub, b)
            if not same.all():
                bad = same.index[~same.to_numpy()] if hasattr(same, "index") else []
                leaks.setdefault(full.names[id(n)], str(bad[0]) if len(bad) else str(t))
    return CausalReport(not leaks, list(leaks.items()), len(cuts))


def _same(a, b) -> pd.Series:
    if isinstance(a, pd.DataFrame):
        ok = pd.Series(True, index=a.index)
        for c in a.columns:
            ok &= _same(a[c], b[c])
        return ok
    if a.dtype == object or b.dtype == object:
        return pd.Series([_eq(x, y) for x, y in zip(a.to_numpy(), b.to_numpy())], index=a.index)
    x, y = a.to_numpy(dtype=float, na_value=np.nan), b.to_numpy(dtype=float, na_value=np.nan)
    return pd.Series(np.isclose(x, y, rtol=1e-9, atol=1e-12, equal_nan=True), index=a.index)


def _eq(x, y) -> bool:
    """Equality for node outputs (dicts, lists, numbers incl. NaN, None)."""
    if isinstance(x, dict) and isinstance(y, dict):
        return x.keys() == y.keys() and all(_eq(x[k], y[k]) for k in x)
    if isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)):
        return len(x) == len(y) and all(_eq(p, q) for p, q in zip(x, y))
    try:
        if isinstance(x, (int, float, np.number)) and isinstance(y, (int, float, np.number)) and not isinstance(x, bool):
            return bool(np.isclose(float(x), float(y), rtol=1e-9, atol=1e-12, equal_nan=True))
    except (TypeError, ValueError):
        pass
    return bool(x == y)
