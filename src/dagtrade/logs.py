"""Organized logs. Everything dagtrade does is written to logs/ (git-ignored), one rotating file per area:

    logs/dagtrade.log   everything (INFO and up)
    logs/errors.log     warnings and errors from every area
    logs/backtest.log   backtests, sweeps, the engine
    logs/live.log       the live scanner: decisions, orders, fills, errors
    logs/data.log       market data downloads / cache
    logs/server.log     the dashboard server (requests that failed, background jobs)
plus live/journal.jsonl: the structured live activity feed shown on each live DAG's page.
View them with `dagtrade logs [area]` or the dashboard's Logs tab.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path

from .data.source import project_root

FORMAT = "%(asctime)s  %(levelname)-7s  %(name)-20s  %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
AREAS = {
    "backtest": ("dagtrade.run", "dagtrade.sweep", "dagtrade.fsa", "dagtrade.sim", "dagtrade.cli"),
    "live": ("dagtrade.live",),
    "data": ("dagtrade.data",),
    "server": ("dagtrade.server", "uvicorn.error"),
}
FILES = {"all": "dagtrade.log", "errors": "errors.log", **{a: f"{a}.log" for a in AREAS}}
_done = False


def log_dir() -> Path:
    d = project_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _Prefix(logging.Filter):
    def __init__(self, prefixes):
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record):
        return record.name.startswith(self.prefixes)


def setup_logging(verbose: bool = False, console: bool = True) -> None:
    """Attach the file handlers (once per process) and a console handler (warnings, or INFO with -v)."""
    global _done
    if _done:
        return
    _done = True
    fmt = logging.Formatter(FORMAT, DATEFMT)

    def handler(name, level, flt=None):
        h = logging.handlers.RotatingFileHandler(log_dir() / FILES[name], maxBytes=5_000_000, backupCount=3,
                                                 encoding="utf-8")
        h.setLevel(level)
        h.setFormatter(fmt)
        if flt:
            h.addFilter(flt)
        return h

    for name in ("dagtrade", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = name == "dagtrade" and console
        lg.addHandler(handler("all", logging.INFO))
        lg.addHandler(handler("errors", logging.WARNING))
        for area, prefixes in AREAS.items():
            lg.addHandler(handler(area, logging.INFO, _Prefix(prefixes)))
    if console:
        root = logging.getLogger()
        if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            root.addHandler(sh)
        for h in root.handlers:
            h.setLevel(logging.INFO if verbose else logging.WARNING)
        root.setLevel(logging.INFO if verbose else logging.WARNING)


_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})  (\w+)\s+  (\S+)\s+  (.*)$")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def read_logs(area: str = "all", n: int = 300, level: str = "INFO", q: str = "") -> list[dict]:
    """Newest-first entries from one area's log (multi-line tracebacks stay attached to their entry).
    area="activity" reads the live journal instead."""
    q = q.lower()
    if area == "activity":
        from .live import live_dir
        p = live_dir() / "journal.jsonl"
        rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        out = [{"time": r["ts"].replace("T", " ")[:19], "level": "ERROR" if r["type"] == "error" else "INFO",
                "logger": r["label"], "message": f"[{r['type']}] {r['text']}"} for r in rows]
    else:
        p = log_dir() / FILES.get(area, FILES["all"])
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-max(n * 6, 3000):]
        out = []
        for ln in lines:
            m = _LINE.match(ln)
            if m:
                out.append({"time": m.group(1), "level": m.group(2), "logger": m.group(3), "message": m.group(4)})
            elif out:
                out[-1]["message"] += "\n" + ln                 # traceback / continuation line
    lv = LEVELS.get(level.upper(), 20)
    out = [r for r in out if LEVELS.get(r["level"], 20) >= lv and (not q or q in (r["message"] + r["logger"]).lower())]
    return out[-n:][::-1]
