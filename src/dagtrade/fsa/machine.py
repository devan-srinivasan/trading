"""FSA: a strategy as a finite-state machine of Nodes.

    FSA.chain(a, b, c, trade)                         a -> b -> c -> trade -> back to a
    FSA.chain(a, b, c, trade, fail={c: b})            ... and when c fails, go back to b instead of the start
    FSA(start=a, edges={a: dict(ok=b), b: dict(ok=trade, fail=a, max_wait=a), trade: dict(ok=a)})

Edges are named by outcome: "ok", "fail", "max_wait" (a node waited longer than its max_wait) or any name a node
returns with Go("name"). Defaults when an edge is not given:
    ok        -> the start (so the last state loops back to the beginning)
    fail      -> the start
    max_wait  -> wherever "fail" goes
A named outcome without an edge is an error in the strategy.
"""
from __future__ import annotations

import pandas as pd

from .. import tf as tfm
from .node import DEFAULT, DEFAULT_MAX_WAIT, Node, Trade, wait_delta

UNBOUNDED_WARMUP = pd.Timedelta(days=400)     # history loaded for a node whose Range has no start


class FSA:
    def __init__(self, start: Node, edges: dict | None = None, name: str = "strategy", base_tf: str | None = None):
        if not isinstance(start, Node):
            raise TypeError("FSA(start=...) needs a Node")
        self.start, self.name, self.base_tf, self.params = start, name, base_tf, {}
        self.edges: dict[str, dict[str, str]] = {}
        nodes: dict[str, Node] = {}

        def add(n):
            if not isinstance(n, Node):
                raise TypeError(f"FSA edges must point at Nodes, got {type(n).__name__}")
            other = nodes.get(n.name)
            if other is not None and other is not n:
                raise ValueError(f"two states are called {n.name!r}: give one a name=... (state names must be unique)")
            nodes[n.name] = n

        add(start)
        for src, outs in (edges or {}).items():
            add(src)
            if not isinstance(outs, dict):
                raise TypeError(f"edges[{src.name}] must be a dict like dict(ok=next_node, fail=other_node)")
            for outcome, dst in outs.items():
                add(dst)
                self.edges.setdefault(src.name, {})[str(outcome)] = dst.name
        self.nodes = nodes
        for n in nodes.values():
            if isinstance(n, Trade) and n.exit is not None and n.exit.name in nodes:
                raise ValueError(f"{n.exit.name!r} is a Trade's exit check and cannot also be a state")
        if not any(isinstance(n, Trade) for n in nodes.values()):
            raise ValueError("an FSA needs at least one Trade state (the state that places orders)")
        self.order = self._order()

    @classmethod
    def chain(cls, *states: Node, fail: dict | None = None, edges: dict | None = None, **kw) -> "FSA":
        """States in order: each one's "ok" goes to the next, the last one's back to the first."""
        if not states:
            raise ValueError("FSA.chain needs at least one state")
        e: dict = {}
        for a, b in zip(states, states[1:]):
            e.setdefault(a, {})["ok"] = b
        for src, dst in (fail or {}).items():
            e.setdefault(src, {})["fail"] = dst
        for src, outs in (edges or {}).items():
            e.setdefault(src, {}).update(outs)
        if len(states) == 1:
            e.setdefault(states[0], {})
        return cls(start=states[0], edges=e, **kw)

    # -- structure ------------------------------------------------------------------------------------
    def _order(self) -> list[str]:
        """States in a readable order: walk "ok" edges from the start first, then the rest."""
        seen, out = set(), []

        def walk(name):
            while name not in seen:
                seen.add(name)
                out.append(name)
                name = self.edges.get(name, {}).get("ok", self.start.name)
        walk(self.start.name)
        for name in self.nodes:
            if name not in seen:
                walk(name)
        return out

    def target(self, state: str, outcome: str) -> str:
        e = self.edges.get(state, {})
        if outcome in e:
            return e[outcome]
        if outcome in ("ok", "fail"):
            return self.start.name
        if outcome == "max_wait":
            return e.get("fail", self.start.name)
        raise ValueError(f"state {state!r} returned outcome {outcome!r} but has no edge for it "
                         f"(edges: {sorted(e) or 'none'}; add {outcome}=<node> to its edges)")

    def max_wait(self, name: str):
        """(candles, duration) for a state: candles as int, duration as Timedelta; both None = wait forever."""
        n = self.nodes[name]
        w = n.max_wait
        if isinstance(n, Trade):
            return None, None
        if w is DEFAULT:
            return (None, None) if name == self.start.name else (DEFAULT_MAX_WAIT, None)
        if w is None:
            return None, None
        return (w, None) if isinstance(w, int) else (None, wait_delta(w))

    def all_nodes(self) -> list[Node]:
        """States plus Trade exit checks (everything that reads candles)."""
        out = list(self.nodes.values())
        out += [n.exit for n in self.nodes.values() if isinstance(n, Trade) and n.exit is not None]
        return out

    def resolve_tfs(self, base_tf: str | None = None) -> tuple[dict[int, str], str]:
        """The FSA's interval (base): the run's choice, else the finest interval a node names, else daily.
        Nodes whose Range names no interval run on the base, so one strategy works on any timeframe."""
        nodes = self.all_nodes()
        explicit = [n.tf for n in nodes if n.tf and not isinstance(n, Trade)]
        base = base_tf or self.base_tf or (tfm.finest(explicit) if explicit else "1d")
        return {id(n): (n.tf if n.tf and not isinstance(n, Trade) else base) for n in nodes}, base

    def required_history(self, at: pd.Timestamp, base_tf: str | None = None) -> pd.Timedelta:
        tfs, _ = self.resolve_tfs(base_tf)
        spans = [n.history.span(at, tfs[id(n)]) for n in self.all_nodes()]
        spans = [UNBOUNDED_WARMUP if x is None else x for x in spans]
        return max(spans, default=pd.Timedelta(0)) * 1.1 + pd.Timedelta(days=5)

    def describe(self, base_tf: str | None = None) -> dict:
        """JSON-able structure for the dashboard's state-machine diagram."""
        tfs, base = self.resolve_tfs(base_tf)

        def node_info(n: Node) -> dict:
            h = n.history
            cand, dur = self.max_wait(n.name) if n.name in self.nodes else (None, None)
            params = n.describe_params() if isinstance(n, Trade) else \
                {k: v for k, v in n.params.items() if isinstance(v, (int, float, str, bool, type(None)))}
            return {"id": n.name, "kind": n.kind, "tf": tfs[id(n)], "agnostic": n.tf is None or isinstance(n, Trade),
                    "history": (f"the last {h.candles} candles" if h.candles else f"{h.start or 'all'} → {h.end}")
                    + (f" of {n.tf}" if n.tf and not isinstance(n, Trade) else ""),
                    "params": params, "type": type(n).__name__, "start": n.name == self.start.name,
                    "max_wait": (f"{cand} candles" if cand else str(dur) if dur is not None else None),
                    "needs": list(n.needs)}

        edges = []
        for name in self.order:
            given = self.edges.get(name, {})
            for outcome, dst in given.items():
                edges.append({"from": name, "to": dst, "outcome": outcome, "default": False})
            for outcome in ("ok", "fail"):
                if outcome not in given:
                    edges.append({"from": name, "to": self.target(name, outcome), "outcome": outcome, "default": True})
        nodes = [node_info(self.nodes[n]) for n in self.order]
        exits = [dict(node_info(n.exit), kind="exit", exit_of=n.name) for n in self.nodes.values()
                 if isinstance(n, Trade) and n.exit is not None]
        return {"name": self.name, "base_tf": base, "start": self.start.name, "nodes": nodes, "exits": exits,
                "edges": edges, "format": "fsa"}
