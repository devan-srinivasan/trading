"""Run storage: runs/<id>/{meta.json, bars, equity, trades, fills, nodes}.parquet + runs/index.sqlite for lists."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pandas as pd

from ..data.source import project_root

SCHEMA = """CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, strategy TEXT, ticker TEXT, created TEXT, base_tf TEXT, start TEXT, end TEXT,
  total_return REAL, cagr REAL, sharpe REAL, max_drawdown REAL, n_trades INTEGER, win_rate REAL,
  bh_return REAL, spy_return REAL, holdout_sharpe REAL, holdout_return REAL, luck_percentile REAL, params TEXT)"""


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else project_root() / "runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = self.root / "index.sqlite"
        with self._conn() as c:
            c.execute(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self._db)
        c.row_factory = sqlite3.Row
        return c

    def dir(self, run_id: str) -> Path:
        return self.root / run_id

    def save(self, run_id: str, meta: dict, frames: dict[str, pd.DataFrame]) -> Path:
        d = self.dir(run_id)
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        for name, df in frames.items():
            df.to_parquet(d / f"{name}.parquet")
        (d / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
        m, s = meta["metrics"], meta["metrics"]["strategy"]
        ho = m.get("split", {}).get("holdout", {}) if m.get("split") else {}
        row = (run_id, meta["strategy"], meta["ticker"], meta["created"], meta["base_tf"], meta["start"], meta["end"],
               s.get("total_return"), s.get("cagr"), s.get("sharpe"), s.get("max_drawdown"), m["trades"]["n_trades"],
               m["trades"].get("win_rate"), m.get("buy_hold", {}).get("total_return"),
               m.get("spy", {}).get("total_return"), ho.get("sharpe"), ho.get("total_return"),
               (m.get("baseline") or {}).get("percentile"), json.dumps(meta["params"], default=str))
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runs VALUES (" + ",".join("?" * 19) + ")", row)
        return d

    def list(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY created DESC")]

    def meta(self, run_id: str) -> dict:
        return json.loads((self.dir(run_id) / "meta.json").read_text())

    def frame(self, run_id: str, name: str) -> pd.DataFrame:
        return pd.read_parquet(self.dir(run_id) / f"{name}.parquet")

    def delete(self, run_id: str) -> int:
        """Remove a run's folder and index row. Returns the bytes freed. Only ever touches runs/<id>/."""
        d = self.dir(run_id)
        if not run_id or d.resolve().parent != self.root.resolve():
            raise ValueError(f"not a run id: {run_id!r}")
        freed = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
        shutil.rmtree(d, ignore_errors=True)
        with self._conn() as c:
            c.execute("DELETE FROM runs WHERE id=?", (run_id,))
        return freed
