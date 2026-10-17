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
from ..logs import setup_logging  # noqa: E402

setup_logging(console=False)
STATIC = Path(__file__).parent / "static"
store = RunStore()
_runner = {"thread": None, "stop": threading.Event(), "runner": None, "error": None}


def _registry():
    from ..live import Registry, live_dir
    return Registry(live_dir())


def _start_live() -> str | None:
    """Start the live scanner in a background thread of this process. Returns an error message or None."""
    from ..live import AlpacaBroker, DryRunBroker, LiveRunner
    if _runner["thread"] is not None and _runner["thread"].is_alive():
        return None

    def alpaca():
        b = AlpacaBroker()
        return DryRunBroker(b) if os.environ.get("DAGTRADE_DRYRUN") == "1" else b

    try:
        runner = LiveRunner(registry=_registry(), alpaca_factory=alpaca)
    except Exception as e:  # noqa: BLE001
        _runner["error"] = str(e)
        log.error("live scanner not started: %s", e)
        return str(e)
    _runner.update(runner=runner, error=None, stop=threading.Event())

    def loop():
        try:
            runner.run_forever(_runner["stop"])
        except Exception as e:  # noqa: BLE001
            _runner["error"] = str(e)
            log.error("live scanner stopped: %s", e)

    t = threading.Thread(target=loop, name="dagtrade-live", daemon=True)
    t.start()
    _runner["thread"] = t
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("DAGTRADE_LIVE") == "1":
        _start_live()
    yield
    _runner["stop"].set()


app = FastAPI(title="dagtrade", lifespan=lifespan)


@app.middleware("http")
async def no_cache(request, call_next):
    """Local tool: always serve the newest page/JS so dashboard updates show up on a plain refresh."""
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("%s %s crashed", request.method, request.url.path)
        raise
    if resp.status_code >= 400 and request.url.path.startswith("/api/"):
        log.warning("%s %s -> %s", request.method, request.url.path, resp.status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/logs")
def logs_api(area: str = "all", n: int = 300, level: str = "INFO", q: str = ""):
    from ..logs import read_logs
    return read_logs(area, min(max(n, 1), 2000), level, q)


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


# ---- backtest jobs: run in the background, report progress ----------------------------------------
_jobs: dict[str, dict] = {}


@app.get("/api/strategies")
def strategies():
    from ..config import load_strategy, strategies_root
    out = []
    for p in sorted(strategies_root().glob("*/strategy.py")):
        name = p.parent.name
        setups = sorted({(t.stem.partition("@")[0].upper(), t.stem.partition("@")[2] or None)
                         for t in p.parent.glob("*.toml")}, key=lambda x: (x[0], x[1] or ""))
        tickers = sorted({t for t, _ in setups})
        dag, err = None, None
        try:
            mod = load_strategy(name)
            desc, defaults = getattr(mod, "DESCRIPTION", ""), getattr(mod, "DEFAULTS", {})
            from ..config import deployment_for
            from ..runner import build_fsa
            g, _ = build_fsa(deployment_for(name, tickers[0] if tickers else "SPY"))
            dag = g.describe()
        except Exception as e:  # noqa: BLE001
            desc, defaults, err = f"(cannot load: {e})", {}, str(e)
        out.append({"name": name, "description": desc, "defaults": {k: v for k, v in defaults.items()
                                                                       if isinstance(v, (int, float, str, bool))},
                    "tickers": tickers, "intervals": sorted({i for _, i in setups if i}), "dag": dag, "error": err})
    return out


def _deployment_for(body: dict):
    from ..config import deployment_for
    name, ticker = body.get("strategy", "").strip(), body.get("ticker", "").strip().upper()
    if not name or not ticker:
        raise HTTPException(400, "pick a strategy and a ticker")
    try:
        dep = deployment_for(name, ticker, start=body.get("start") or None, end=body.get("end") or None,
                             interval=body.get("interval") or None, flat_by_close=body.get("flat_by_close"))
        if body.get("start_at"):
            dep.start_at = body["start_at"]
        if body.get("inputs"):
            if not isinstance(body["inputs"], dict):
                raise ValueError("inputs must be a JSON object")
            dep.inputs = {**dep.inputs, **body["inputs"]}
        return dep
    except FileNotFoundError:
        raise HTTPException(404, f"unknown strategy {name}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/jobs")
def start_job(body: dict):
    import uuid
    from ..fsa import FsaRunner
    from ..runner import Cancelled, build_fsa, run_deployment
    dep = _deployment_for(body)
    try:
        fsa, _ = build_fsa(dep)
        if dep.start_at:
            FsaRunner(fsa, flat_by_close=dep.flat_by_close, base_tf=dep.base_tf).reset(dep.start_at, dep.inputs)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"cannot build strategy: {e}")
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "label": f"{dep.label}", "state": "running", "stage": "loading candles", "pct": 2.0,
           "seen": [], "run_id": None, "error": None, "started": time.time(), "cancel": threading.Event()}
    _jobs[jid] = job

    def progress(name, done, total):
        if name.startswith("downloading"):                       # data download, before any node runs
            job["pct"] = round(2 + 3 * (done / total if total else 1), 1)
            job["stage"] = f"{name}: {done}/{total}"
            return
        if name == "fsa":                                        # the replay, candle by candle
            job["pct"] = round(5 + 90 * (done / total if total else 1), 1)
            job["stage"] = f"replaying {done:,}/{total:,} candles"
            return
        job["stage"] = f"{name}: {done:,}/{total:,}"

    def work():
        log.info("job %s: backtest %s requested from the dashboard (%s -> %s, interval %s)", jid, dep.label, dep.start,
                 dep.end or "today", dep.base_tf or "default")
        try:
            out = run_deployment(dep, store=store, progress=progress, cancel=job["cancel"])
            job.update(state="done", pct=100.0, stage="done", run_id=out.run_id)
            log.info("job %s: done -> %s", jid, out.run_id)
        except Cancelled:
            job.update(state="cancelled", stage="cancelled")
            log.info("job %s: %s cancelled from the dashboard after %.1fs", jid, dep.label, time.time() - job["started"])
        except Exception as e:  # noqa: BLE001
            log.exception("job %s: backtest %s failed", jid, dep.label)
            job.update(state="error", stage="failed", error=str(e))

    threading.Thread(target=work, name=f"backtest-{jid}", daemon=True).start()
    return {"id": jid}


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    return {k: v for k, v in job.items() if k not in ("seen", "cancel")} | {"elapsed": round(time.time() - job["started"], 1)}


@app.post("/api/jobs/{jid}/cancel")
def job_cancel(jid: str):
    """Ask a running backtest to stop. It stops at its next check (normally well under a second) and saves nothing."""
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    if job["state"] == "running":
        job["cancel"].set()
        job["stage"] = "stopping…"
    return {"state": job["state"]}


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
    try:
        return {"ok": True, "freed": store.delete(run_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/runs/delete")
def delete_many(body: dict):
    """Delete several backtests at once: {"ids": [...]}. Returns how many went and the bytes freed."""
    ids = [str(i) for i in body.get("ids") or []]
    known = {r["id"] for r in store.list()}
    bad = [i for i in ids if i not in known]
    if bad:
        raise HTTPException(400, f"unknown backtest ids: {', '.join(bad[:5])}")
    freed = sum(store.delete(i) for i in ids)
    log.info("deleted %d backtests from the dashboard (%.1f MB freed)", len(ids), freed / 1e6)
    return {"deleted": len(ids), "freed": freed}


# ---- live ------------------------------------------------------------------------------------
_clock = None


def _live_dir() -> Path:
    from ..live import live_dir
    return live_dir()


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _detail(did: str) -> dict:
    from ..live.models import slug
    return _read_json(_live_dir() / "dags" / f"{slug(did)}.json") or {}


@app.get("/api/live")
def live_status():
    global _clock
    from ..live import MarketClock
    d = _live_dir()
    st = _read_json(d / "status.json") or {}
    age = time.time() - st["heartbeat"] if st.get("heartbeat") else None
    rows = {r.get("label"): r for r in st.get("deployments", [])}
    dags = []
    for e in _registry().load():
        det, row = _detail(e["id"]), rows.get(e["id"], {})
        stages = det.get("stages") or []
        current = (det.get("fsa") or {}).get("current")
        dags.append({**e, "account": det.get("account"), "position": det.get("position"),
                     "last_bar": row.get("last_bar") or pd_iso(det.get("last_avail")),
                     "current_step": current, "steps": [{"name": x["name"], "state": x["state"]} for x in stages],
                     "last_action": det.get("last_action"), "error": row.get("error") or det.get("last_error"),
                     "updated": det.get("updated")})
    if _clock is None:
        _clock = MarketClock()
    try:
        c = _clock()
        clock = {"is_open": c.is_open, "next_open": str(c.next_open), "next_close": str(c.next_close)}
    except Exception:  # noqa: BLE001
        clock = None
    return {"scanner": {"alive": age is not None and age < 150, "age": age, "error": _runner["error"],
                        "in_process": _runner["thread"] is not None and _runner["thread"].is_alive()},
            "paused": (d / "PAUSED").exists(), "clock": clock, "alpaca_account": st.get("account"), "dags": dags}


def pd_iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="minutes") if epoch else None


WINDOWS = {"today": None, "1w": 7, "1m": 30, "all": None}


def _live_report(e: dict, det: dict, window: str) -> dict:
    """Metrics for a live DAG over a window, from its account's FILLS only (the same ledger as backtests)."""
    import pandas as pd
    from ..analytics.ledger import ledger_report
    from ..config import deployment_for
    from ..data import AlpacaProvider, BarStore, DataSource
    from ..data.source import project_root
    from .payload import _clean, _col, _secs
    now = pd.Timestamp.now(tz="UTC")
    added = pd.Timestamp(e.get("added") or now)
    if window == "today":
        start = pd.Timestamp.now(tz="America/New_York").normalize().tz_convert("UTC")
    elif window in ("1w", "1m"):
        start = now - pd.Timedelta(days=WINDOWS[window])
    else:
        start = added
    start = max(start, added - pd.Timedelta(minutes=1))
    tf = det.get("base_tf") or "1d"
    dep = deployment_for(e["strategy"], e["ticker"], interval=e.get("interval"))
    feed = (dep.live or {}).get("feed", "iex")
    cached = DataSource([AlpacaProvider(feed=feed)], BarStore(project_root() / "data" / "live_bars"), fetch_missing=False)
    try:
        candles = cached.bars(e["ticker"], tf, added - pd.Timedelta(days=1), now)
    except LookupError:
        return {}
    daily = DataSource([AlpacaProvider(feed=feed)], BarStore(project_root() / "data" / "live_bars"))
    bench = {}
    for key, sym in (("buy_hold", e["ticker"]), ("spy", "SPY")):
        try:
            s = daily.bars(sym, "1d", start - pd.Timedelta(days=10), now)["close"]
            s.index = pd.DatetimeIndex(s.index.tz_convert("UTC").tz_localize(None).normalize())
            bench[key] = s
        except Exception:  # noqa: BLE001
            pass
    sim = dep.sim_config()
    led = ledger_report(det.get("fills") or [], candles, float(e.get("cash") or 10_000), tf, start=start,
                        benchmarks=bench, cost_bps=2 * (sim.commission_bps + sim.slippage_bps))
    eq = {}
    if len(led.daily):
        eq = {"t": _secs(led.daily.index)}
        for c in led.daily.columns:
            eq[c] = _col(led.daily[c], 2)
        d = led.daily["strategy"]
        eq["drawdown"] = _col(d / d.cummax() - 1, 5)
    return _clean({"window": window, "start": start.isoformat(), "metrics": led.metrics, "equity": eq,
                   "candles": len(led.equity)})


@app.get("/api/live/dag")
def live_dag(id: str, window: str = "all"):
    from ..live import Journal
    e = _registry().get(id)
    if e is None:
        raise HTTPException(404, f"{id} is not a live DAG")
    from ..live.models import slug
    from .payload import live_payload
    det = _detail(id)
    src = _read_json(_live_dir() / "charts" / f"{slug(id)}.json")
    lp = live_payload(src, det.get("fills") or [])
    try:
        report = _live_report(e, det, window if window in WINDOWS else "all")
    except Exception as ex:  # noqa: BLE001
        log.exception("live report for %s failed", id)
        report = {"error": str(ex)}
    det.pop("fills", None)
    return {"entry": e, "detail": det, "chart": lp["chart"], "trades": lp["trades"], "chart_updated": lp.get("updated"),
            "report": report,
            "journal": Journal(_live_dir() / "journal.jsonl").tail(60, label=id)}


@app.post("/api/live/dags")
def live_add(body: dict):
    try:
        inputs = body.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a JSON object")
        e = _registry().add(body.get("strategy", ""), body.get("ticker", "").strip(), body.get("interval") or None,
                            body.get("mode") or "local", float(body.get("cash") or 10_000), body.get("flat_by_close"),
                            body.get("start_at") or None, inputs)
    except (ValueError, KeyError) as ex:
        raise HTTPException(400, str(ex))
    return e


@app.post("/api/live/dag/action")
def live_dag_action(body: dict):
    from ..live import Journal
    did, action = body.get("id", ""), body.get("action", "")
    reg = _registry()
    try:
        if action == "start":
            e = reg.update(did, status="running")
        elif action == "stop":
            e = reg.update(did, status="stopped")
        elif action == "mode":
            e = reg.update(did, mode=body.get("mode"))
        elif action == "restart":
            mem = body.get("memory") or {}
            if not isinstance(mem, dict):
                raise ValueError("memory must be a JSON object: {state name: its output}")
            e = reg.request_restart(did, body.get("at") or None, mem)
        elif action == "remove":
            reg.update(did, status="stopped")
            reg.remove(did)
            e = None
        else:
            raise HTTPException(400, "unknown action")
    except KeyError:
        raise HTTPException(404, f"{did} is not a live DAG")
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    Journal(_live_dir() / "journal.jsonl").add(did, "note", f"{action}{' -> ' + body.get('mode', '') if action == 'mode' else ''}"
                                               f"{' at ' + (body.get('at') or 'start') if action == 'restart' else ''} (dashboard)")
    return {"ok": True, "entry": e}


@app.post("/api/live/scanner/{action}")
def live_scanner(action: str):
    if action == "start":
        err = _start_live()
        if err:
            raise HTTPException(400, err)
    elif action == "stop":
        _runner["stop"].set()
    else:
        raise HTTPException(404, "unknown action")
    return {"ok": True}


@app.post("/api/live/{action}")
def live_control(action: str):
    if action not in ("pause", "resume"):
        raise HTTPException(404, "unknown action")
    from ..live import Journal
    p = _live_dir() / "PAUSED"
    p.touch() if action == "pause" else p.unlink(missing_ok=True)
    Journal(_live_dir() / "journal.jsonl").add("*", "note", "PAUSED from dashboard - no orders will be sent"
                                               if action == "pause" else "resumed from dashboard")
    return {"paused": action == "pause"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
