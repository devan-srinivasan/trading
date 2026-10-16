"""Deployments: a strategy (code) + a ticker + settings. Stored as TOML next to the strategy:

    strategies/trend_pullback/strategy.py     <- build(**params) -> Graph, DEFAULTS, DESCRIPTION
    strategies/trend_pullback/AAPL.toml       <- this strategy on AAPL
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
    warmup_days: int = 400
    benchmark: str = "SPY"
    holdout: float = 0.2
    params: dict = field(default_factory=dict)
    sim: dict = field(default_factory=dict)
    live: dict = field(default_factory=dict)
    path: Path | None = None

    @property
    def label(self) -> str:
        return f"{self.strategy}/{self.ticker}"

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
        raise AttributeError(f"{path} must define build(**params) -> Graph")
    return mod


def load_deployment(path: str | Path) -> Deployment:
    p = Path(path)
    raw = tomllib.loads(p.read_text())
    known = {"strategy", "ticker", "start", "end", "base_tf", "warmup_days", "benchmark", "holdout", "params", "sim", "live"}
    extra = set(raw) - known
    if extra:
        raise ValueError(f"{p}: unknown keys {sorted(extra)}")
    raw.setdefault("strategy", p.parent.name)
    raw.setdefault("ticker", p.stem.upper())
    return Deployment(path=p, **raw)


def discover_deployments(root: Path | None = None) -> list[Deployment]:
    return [load_deployment(p) for p in sorted(strategies_root(root).glob("*/*.toml"))]
