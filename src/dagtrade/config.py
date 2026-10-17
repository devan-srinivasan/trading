"""Deployments: a strategy (code) + a ticker + settings. Stored as TOML next to the strategy:

    strategies/stochastic/strategy.py     <- build(**params) -> FSA, DEFAULTS, DESCRIPTION
    strategies/stochastic/AAPL.toml       <- this strategy on AAPL (its default interval)
    strategies/stochastic/AAPL@5m.toml    <- the same strategy on AAPL's 5-minute candles (own params, dates, ...)

A strategy is timeframe-agnostic; the interval is a run setting (`base_tf`). `flat_by_close = true` makes it a
day-trading run: no new entries near the close and out of the position before 16:00 ET.
"""
from __future__ import annotations

import importlib.util
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pandas as pd

from .data.source import project_root
from .sim.broker import SimConfig

STRATEGIES_DIR = "strategies"


@dataclass
class Deployment:
    strategy: str
    ticker: str
    start: str
    end: str | None = None
    base_tf: str | None = None
    flat_by_close: bool = False
    size: float | None = None               # orders as this fraction of the account (reinvests); None = the strategy's
    start_at: str | None = None             # begin the first pass at this state instead of the start state
    inputs: dict = field(default_factory=dict)   # [inputs]: extra values every node sees as ctx.inputs (+ "cash")
    warmup_days: int | None = None          # extra history to load; default: whatever the nodes' Ranges need
    benchmark: str = "SPY"
    holdout: float = 0.2
    params: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)
    live: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def label(self) -> str:
        return f"{self.strategy}/{self.ticker}" + (f"@{self.base_tf}" if self.path and "@" in self.path.stem else "")

    def data_start(self, fsa) -> pd.Timestamp:
        """First candle to load: the backtest start minus the warm-up the DAG's nodes need."""
        warm = pd.Timedelta(days=self.warmup_days) if self.warmup_days is not None else pd.Timedelta(0)
        return self.start_ts() - max(warm, fsa.required_history(self.start_ts(), self.base_tf))

    def sim_config(self) -> SimConfig:
        return SimConfig(**self.sim)

    def end_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.end, tz="UTC") if self.end else pd.Timestamp.now(tz="UTC").normalize()

    def start_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.start, tz="UTC")


def strategies_root(root: Path | None = None) -> Path:
    return (root or project_root()) / STRATEGIES_DIR


def load_strategy(name: str, root: Path | None = None) -> ModuleType:
    path = strategies_root(root) / name / "strategy.py"
    if not path.exists():
        have = sorted(p.parent.name for p in strategies_root(root).glob("*/strategy.py"))
        raise FileNotFoundError(f"strategy {name!r} not found at {path}. Available: {have}")
    modname = f"dagtrade_strategy_{name}"
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod            # so node classes defined in it can be fingerprinted for the cache
    spec.loader.exec_module(mod)
    if not hasattr(mod, "build"):
        raise AttributeError(f"{path} must define build(**params) -> FSA")
    return mod


def load_deployment(path: str | Path) -> Deployment:
    p = Path(path)
    raw = tomllib.loads(p.read_text())
    known = {"strategy", "ticker", "start", "end", "base_tf", "flat_by_close", "size", "start_at", "inputs",
             "warmup_days", "benchmark", "holdout", "params", "sim", "live"}
    extra = set(raw) - known
    if extra:
        raise ValueError(f"{p}: unknown keys {sorted(extra)}")
    raw.setdefault("strategy", p.parent.name)
    ticker, _, interval = p.stem.partition("@")            # AAPL.toml or AAPL@5m.toml
    raw.setdefault("ticker", ticker.upper())
    if interval:
        raw.setdefault("base_tf", interval)
    return Deployment(path=p, **raw)


def discover_deployments(root: Path | None = None) -> list[Deployment]:
    return [load_deployment(p) for p in sorted(strategies_root(root).glob("*/*.toml"))]


def deployment_for(strategy: str, ticker: str, start: str | None = None, end: str | None = None,
                   interval: str | None = None, root: Path | None = None,
                   flat_by_close: bool | None = None) -> Deployment:
    """A deployment for any strategy x ticker x interval: <TICKER>@<interval>.toml if it exists, else <TICKER>.toml,
    else defaults; then the optional overrides."""
    from .range import parse_interval
    folder = strategies_root(root) / strategy
    if not (folder / "strategy.py").exists():
        raise FileNotFoundError(f"unknown strategy {strategy!r}")
    interval = parse_interval(interval) if interval else None
    t = ticker.upper()
    cands = ([folder / f"{t}@{interval}.toml"] if interval else []) + [folder / f"{t}.toml"]
    if not interval:                     # a strategy with only interval setups (e.g. SPY@1m.toml): use the first
        cands += sorted(folder.glob(f"{t}@*.toml"))
    toml = next((c for c in cands if c.exists()), None)
    dep = load_deployment(toml) if toml else Deployment(strategy=strategy, ticker=t, start="2018-01-01",
                                                        path=folder / f"{t}.toml")
    if start:
        dep.start = start
    if end:
        dep.end = end
    if interval:
        dep.base_tf = interval
    if flat_by_close is not None:
        dep.flat_by_close = bool(flat_by_close)
    return dep
