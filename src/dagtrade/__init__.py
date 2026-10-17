"""dagtrade - strategies as finite-state machines of nodes: candles in, an outcome out, ending in a Trade."""
from .fsa import FSA, Go, Node, Trade, verify_causal
from .range import Range

__version__ = "0.3.0"
__all__ = ["FSA", "Go", "Node", "Range", "Trade", "verify_causal"]
