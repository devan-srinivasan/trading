"""dagtrade - strategies as a DAG of nodes: candles in, structured data out, ending in a Trade."""
from .graph import Graph, Node, Trade, verify_causal
from .range import Range

__version__ = "0.2.0"
__all__ = ["Graph", "Node", "Range", "Trade", "verify_causal"]
