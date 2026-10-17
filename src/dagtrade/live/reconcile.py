"""Live bookkeeping per DAG. The planner itself (intents -> broker actions, pure) lives in fsa.planner and is shared
with the backtest engine; it is re-exported here."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..fsa.planner import CASH_BUFFER, MAX_DECISION_AGE, Action, plan  # noqa: F401


@dataclass
class DepState:
    """A live DAG's saved state: the FSA runner's state + outstanding intents + bookkeeping."""
    fsa: dict = field(default_factory=dict)
    intents: list = field(default_factory=list)
    events: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    last_avail: float | None = None
    last_action: dict | None = None
    last_error: str | None = None
    retry_not_before: float = 0.0
    restart_nonce: str | None = None
