"""Strategies as finite-state machines: Nodes are states, edges are outcomes (ok / fail / max_wait / named)."""
from .candles import CandleView
from .causal import CausalReport, verify_causal
from .engine import BacktestResult, backtest
from .machine import FSA
from .node import Account, Ctx, Go, Intent, Node, Trade
from .runner import FsaRunner

__all__ = ["FSA", "Node", "Trade", "Go", "Ctx", "Account", "Intent", "FsaRunner", "CandleView", "backtest",
           "BacktestResult", "verify_causal", "CausalReport"]
