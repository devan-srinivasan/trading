"""Dashboard server: `dagtrade dash` (viewer only) or `dagtrade serve` (viewer + always-on live scanner)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import discover_deployments
from ..store import RunStore
from .payload import run_payload

log = logging.getLogger("dagtrade.server")
STATIC = Path(__file__).parent / "static"
store = RunStore()
_runner = {"thread": None, "stop": threading.Event(), "runner": None, "error": None}


def _start_live():
    """Start the live scanner in a background thread (only when launched via `dagtrade serve`)."""
    from ..live import AlpacaBroker, DryRunBroker, LiveRunner
    try:
        deps = [d for d in discover_deployments() if d.live.get("enabled")]
        broker = AlpacaBroker()
        if os.environ.get("DAGTRADE_DRYRUN") == "1":
            broker = DryRunBroker(broker)
        runner = LiveRunner(deps, broker)
    except Exception as e:  # noqa: BLE001
        _runner["error"] = str(e)
        log.error("live scanner not started: %s", e)
        return
    _runner["runner"] = runner

    def loop():
        try:
            runner.run_forever(_runner["stop"])
        except Exception as e:  # noqa: BLE001
            _runner["error"] = str(e)
            log.error("live scanner stopped: %s", e)

    t = threading.Thread(target=loop, name="dagtrade-live", daemon=True)
    t.start()
    _runner["thread"] = t


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("DAGTRADE_LIVE") == "1":
        _start_live()
    yield
    _runner["stop"].set()


app = FastAPI(title="dagtrade", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/runs")
def runs():
    return store.list()


@app.get("/api/deployments")
def deployments():
    return [{"label": d.label, "strategy": d.strategy, "ticker": d.ticker, "start": d.start,
             "base_tf": d.base_tf, "path": str(d.path), "live": bool(d.live.get("enabled"))}
            for d in discover_deployments()]


@app.get("/api/run/{run_id}")
def run(run_id: str):
    if not (store.dir(run_id) / "meta.json").exists():
        raise HTTPException(404, "no such run")
    return JSONResponse(run_payload(store, run_id))


@app.post("/api/run")
def rerun(body: dict):
    from ..runner import run_deployment
    label = body.get("label", "")
    dep = next((d for d in discover_deployments() if d.label == label), None)
    if not dep:
        raise HTTPException(404, f"unknown deployment {label}")
    try:
        out = run_deployment(dep, store=store)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    return {"id": out.run_id}


@app.delete("/api/run/{run_id}")
def delete(run_id: str):
    store.delete(run_id)
    return {"ok": True}


# ---- live ------------------------------------------------------------------------------------
def _live_dir() -> Path:
    from ..live import live_dir
    return live_dir()


@app.get("/api/live")
def live_status():
    d = _live_dir()
    p = d / "status.json"
    st = json.loads(p.read_text()) if p.exists() else {}
    age = time.time() - st["heartbeat"] if st.get("heartbeat") else None
    from ..live import Journal
    deps = [x.label for x in discover_deployments() if x.live.get("enabled")]
    return {"status": st, "alive": age is not None and age < 150, "age": age, "paused": (d / "PAUSED").exists(),
            "journal": Journal(d / "journal.jsonl").tail(80), "enabled": deps,
            "in_process": _runner["thread"] is not None and _runner["thread"].is_alive(),
            "scanner_error": _runner["error"]}


@app.post("/api/live/{action}")
def live_control(action: str):
    if action not in ("pause", "resume"):
        raise HTTPException(404, "unknown action")
    from ..live import Journal
    p = _live_dir() / "PAUSED"
    p.touch() if action == "pause" else p.unlink(missing_ok=True)
    Journal(_live_dir() / "journal.jsonl").add("*", "note", "PAUSED from dashboard - no orders will be sent"
                                               if action == "pause" else "resumed from dashboard (fresh baseline)")
    return {"paused": action == "pause"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
