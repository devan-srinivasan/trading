"""dagtrade command line.

  dagtrade list                         strategies, deployments and saved runs
  dagtrade data fetch AAPL --tf 5m      download + cache bars
  dagtrade run strategies/x/AAPL.toml   backtest (or: dagtrade run x/AAPL, or --all)
  dagtrade check x/AAPL                 lookahead test: hide the future, make sure nothing changes
  dagtrade sweep x/AAPL fast=10,20 ...  parameter grid; ranked in-sample, holdout shown beside it
  dagtrade new my_idea                  scaffold a strategy
  dagtrade dash                         open the dashboard
  dagtrade report RUN_ID                standalone HTML report

  dagtrade live add x/AAPL [--interval 5m] [--flat-by-close] [--mode local|alpaca]   make a DAG live (local paper by default)
  dagtrade live list|start|stop|remove|mode ID                     manage live DAGs (ids like x/AAPL@5m)
  dagtrade live restart ID [--start-at STATE]                     restart a live DAG's state machine
  dagtrade live check | once | run [--dry-run]                     show decisions / one scan / the always-on scanner
  dagtrade live status|pause|resume|flatten                        state, stop sending orders, close everything
  dagtrade serve [--dry-run]                                       dashboard AND always-on scanner in one process
  dagtrade logs [all|errors|backtest|live|data|server|activity] [-n 50] [--level WARNING] [--grep x] [-f]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import time
from pathlib import Path

import pandas as pd

from .config import Deployment, discover_deployments, load_deployment, strategies_root
from .data.source import project_root


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(project_root() / ".env")
    except ImportError:
        pass


def resolve(target: str) -> Deployment:
    p = Path(target)
    if p.exists():
        return load_deployment(p)
    cand = strategies_root() / f"{target}.toml"
    if cand.exists():
        return load_deployment(cand)
    raise SystemExit(f"cannot find deployment {target!r} (try 'dagtrade list')")


def pct(x, d=1):
    return "   n/a" if x is None else f"{x:+.{d}%}"


def fmt_run(out) -> str:
    m, dep = out.metrics, out.deployment
    s, t = m["strategy"], m["trades"]
    lines = [f"\n== {dep.label}  {dep.start} -> {out.meta['end']}  base={out.meta['base_tf']}  run={out.run_id}"]
    hdr = f"{'':<16}{'return':>9}{'CAGR':>9}{'Sharpe':>8}{'MaxDD':>9}"
    lines.append(hdr)
    for label, key in (("strategy", "strategy"), (f"buy&hold {dep.ticker}", "buy_hold"), (dep.benchmark, "spy")):
        x = m.get(key)
        if x:
            lines.append(f"{label:<16}{pct(x['total_return']):>9}{pct(x['cagr']):>9}{x['sharpe']:>8.2f}"
                         f"{pct(x['max_drawdown']):>9}")
    lines.append(f"trades {t['n_trades']}  win {t.get('win_rate', 0):.0%}  profit factor {t.get('profit_factor', 0):.2f}"
                 f"  exposure {t.get('exposure', 0):.0%}")
    sp = m.get("split")
    if sp:
        a, b = sp["in_sample"], sp["holdout"]
        lines.append(f"in-sample Sharpe {a.get('sharpe', 0):.2f} ({pct(a.get('total_return'))}) | "
                     f"holdout from {sp['split_date']}: Sharpe {b.get('sharpe', 0):.2f} ({pct(b.get('total_return'))})")
    bl = m.get("baseline")
    if bl:
        lines.append(f"beat {bl['percentile']:.0f}% of {bl['n']} random-timing runs (median random {pct(bl['random_median'])})")
    if out.meta.get("causal"):
        lines.append("  " + out.meta["causal"].splitlines()[0])
    for i in out.insights:
        lines.append("  ! " + textwrap.fill(i, 100, subsequent_indent="    "))
    return "\n".join(lines)


def cmd_list(a):
    from .store import RunStore
    print("Deployments (strategy x ticker):")
    for d in discover_deployments():
        print(f"  {d.label:<28} start {d.start}  base_tf {d.base_tf or 'auto'}")
    runs = RunStore().list()
    print(f"\nSaved runs: {len(runs)}")
    for r in runs[:15]:
        print(f"  {r['id']:<40} return {pct(r['total_return'])}  sharpe {r['sharpe'] or 0:.2f}  trades {r['n_trades']}")


def cmd_data(a):
    from .data import DataSource
    ds = DataSource()
    start = a.start or "2018-01-01"
    for t in a.tickers:
        df = ds.bars(t.upper(), a.tf, start, a.end or pd.Timestamp.now(tz="UTC"))
        print(f"{t.upper()} {a.tf}: {len(df)} bars  {df.index[0]} -> {df.index[-1]}")


class _Bars:
    """Terminal progress: one bar over all runs, one bar for the node currently walking its candles."""

    def __init__(self, total: int):
        from tqdm import tqdm
        self.tqdm = tqdm
        self.outer = tqdm(total=total, desc="backtests", unit="run", position=0, dynamic_ncols=True,
                          bar_format="{desc} {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} runs [{elapsed}<{remaining}]")
        self.inner, self.node = None, None

    def start(self, label: str):
        self.outer.set_description(f"backtests  {label}")

    def node_progress(self, name: str, done: int, total: int):
        if self.node != name or self.inner is None:
            if self.inner is not None:
                self.inner.close()
            self.node = name
            self.inner = self.tqdm(total=total, desc=f"  {name}", unit="candle", position=1, leave=False,
                                   dynamic_ncols=True, unit_scale=True)
        self.inner.update(done - self.inner.n)

    def done(self, text: str):
        if self.inner is not None:
            self.inner.close()
            self.inner, self.node = None, None
        self.outer.write(text)
        self.outer.update(1)

    def close(self):
        self.outer.set_description("backtests")
        self.outer.close()


def cmd_run(a):
    from .runner import run_deployment
    deps = discover_deployments() if a.all else [resolve(t) for t in a.targets]
    bars = _Bars(len(deps))
    try:
        for dep in deps:
            if a.start:
                dep.start = a.start
            bars.start(dep.label)
            try:
                out = run_deployment(dep, cache=not a.no_cache, check_causal=a.check, progress=bars.node_progress)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("dagtrade.run").exception("backtest %s failed", dep.label)
                bars.done(f"\n== {dep.label}: FAILED - {e}")
                if a.verbose:
                    raise
                continue
            bars.done(fmt_run(out))
    finally:
        bars.close()


def cmd_check(a):
    from .data import DataSource
    from .fsa import verify_causal
    from .runner import build_fsa
    for t in a.targets:
        dep = resolve(t)
        fsa, _ = build_fsa(dep)
        rep = verify_causal(lambda: build_fsa(dep)[0], DataSource(), dep.ticker, dep.start_ts(), dep.end_ts(),
                            base_tf=dep.base_tf, cfg=dep.sim_config(), inputs=dep.inputs,
                            flat_by_close=dep.flat_by_close, data_start=dep.data_start(fsa))
        print(f"{dep.label}: {rep}")


def cmd_sweep(a):
    from .sweep import parse_grid, sweep
    dep = resolve(a.target)
    df = sweep(dep, parse_grid(a.grid))
    if df.empty:
        raise SystemExit("no successful combos")
    with pd.option_context("display.width", 200, "display.max_columns", 30, "display.float_format", "{:.3f}".format):
        print(df.to_string())
    best = df.iloc[0]
    print(f"\nBest in-sample Sharpe {best.is_sharpe:.2f} -> holdout Sharpe {best.ho_sharpe:.2f}. "
          f"If the holdout is far below, you are looking at overfitting, not an edge.")


def cmd_new(a):
    d = strategies_root() / a.name
    if d.exists():
        raise SystemExit(f"{d} exists")
    d.mkdir(parents=True)
    (d / "strategy.py").write_text(TEMPLATE.format(name=a.name))
    (d / f"{a.ticker.upper()}.toml").write_text('start = "2018-01-01"\n\n[params]\n\n[sim]\ncommission_bps = 1.0\nslippage_bps = 2.0\n')
    print(f"created {d}/strategy.py and {a.ticker.upper()}.toml - edit build(), then: dagtrade run {a.name}/{a.ticker.upper()}")


TEMPLATE = '''"""{name}: describe the idea in one paragraph."""
from dagtrade import FSA, Node, Range, Trade
from dagtrade.indicators import rsi, sma

DESCRIPTION = "What this strategy does, in plain English."
DEFAULTS = dict(rsi_buy=30, take_profit=0.08, stop_loss=0.04)


class Uptrend(Node):
    """State 1: wait until the close is above its 50-candle average."""
    history = Range(candles=60)          # the last 60 candles of the interval you run on (or Range(start="-60d", interval="d"))

    def run(self, candles, ctx):
        if len(candles) >= 50 and candles.close.iloc[-1] > sma(candles.close, 50).iloc[-1]:
            return True                  # success -> the next state
        return None                      # not yet: ask again next candle


class OversoldBounce(Node):
    """State 2: within 10 candles, wait for RSI(3) to dip below rsi_buy; then buy."""
    history = Range(candles=20)
    max_wait = 10                        # gave up waiting -> back to the start

    def __init__(self, rsi_buy, **kw):
        super().__init__(rsi_buy=rsi_buy, **kw)           # becomes self.rsi_buy

    def run(self, candles, ctx):
        if rsi(candles.close, 3).iloc[-1] < self.rsi_buy:
            return {{"buy": True}}                         # the Trade state places this order
        return None


def build(rsi_buy, take_profit, stop_loss):
    return FSA.chain(Uptrend(), OversoldBounce(rsi_buy), Trade(take_profit=take_profit, stop_loss=stop_loss))
'''


def cmd_dash(a):
    import uvicorn
    print(f"dashboard on http://{a.host}:{a.port}")
    uvicorn.run("dagtrade.viz.server:app", host=a.host, port=a.port, log_level="warning")


def cmd_report(a):
    from .viz.report import write_report
    path = write_report(a.run_id, a.out)
    print(f"wrote {path}")


# ---------------------------------------------------------------------------------------------
def _registry():
    from .live import Registry, live_dir
    return Registry(live_dir())


def _make_runner(a, charts=True):
    from .live import AlpacaBroker, DryRunBroker, LiveRunner

    def alpaca():
        b = AlpacaBroker()
        return DryRunBroker(b) if getattr(a, "dry_run", False) else b
    return LiveRunner(registry=_registry(), alpaca_factory=alpaca, charts=charts)


def cmd_live(a):
    from .live import Journal, live_dir
    reg = _registry()
    args = a.args
    if a.sub == "add":
        if not args or "/" not in args[0]:
            raise SystemExit("usage: dagtrade live add STRATEGY/TICKER [--interval 5m] [--flat-by-close] [--mode local|alpaca]")
        strat, tick = args[0].split("/", 1)
        try:
            e = reg.add(strat, tick, a.interval, a.mode, flat_by_close=a.flat_by_close, start_at=a.start_at)
        except ValueError as ex:
            raise SystemExit(str(ex))
        print(f"added {e['id']} ({e['mode']} paper, ${e['cash']:,.0f}). It starts on the scanner's next cycle "
              f"(dagtrade serve, or dagtrade live run).")
        return
    if a.sub == "restart":
        if not args:
            raise SystemExit("usage: dagtrade live restart DAG_ID [--start-at STATE]")
        try:
            reg.request_restart(args[0], a.start_at)
        except (KeyError, ValueError) as ex:
            raise SystemExit(str(ex))
        print(f"{args[0]}: restarts at {a.start_at or 'the start state'} on the scanner's next cycle")
        return
    if a.sub in ("start", "stop", "remove", "mode"):
        if not args:
            raise SystemExit(f"usage: dagtrade live {a.sub} DAG_ID" + (" local|alpaca" if a.sub == "mode" else ""))
        try:
            if a.sub == "remove":
                reg.remove(args[0])
            elif a.sub == "mode":
                reg.update(args[0], mode=args[1] if len(args) > 1 else None)
            else:
                reg.update(args[0], status="running" if a.sub == "start" else "stopped")
        except (KeyError, ValueError, IndexError) as ex:
            raise SystemExit(f"{a.sub} failed: {ex}")
        Journal().add(args[0], "note", f"{a.sub} (CLI)")
        print(f"{a.sub}: {args[0]}")
        return
    if a.sub == "list":
        items = reg.load()
        if not items:
            print("no live DAGs. Add one: dagtrade live add stochastic/SPY --interval 5m")
        for e in items:
            print(f"  {e['status']:<8} {e['mode']:<7} {e['id']}")
        return
    if a.sub == "status":
        p = live_dir() / "status.json"
        if not p.exists():
            raise SystemExit("no status yet - the scanner has not completed a cycle (dagtrade serve / live run)")
        st = json.loads(p.read_text())
        age = time.time() - (st.get("heartbeat") or 0)
        clk = st.get("clock") or {}
        print(f"scanner heartbeat {age:.0f}s ago {'(NOT RUNNING?)' if age > 120 else ''}  paused={st.get('paused')}  "
              f"market {'OPEN' if clk.get('is_open') else 'closed'}")
        for d in st.get("deployments", []):
            ac = d.get("account") or {}
            print(f"  {d['label']:<30} {d.get('mode', ''):<7} bar {d.get('last_bar', '-')}  position {d.get('position', 0) or 0:+g}  "
                  f"step {d.get('current_step', '-')}  P&L {ac.get('realized', 0) or 0:+.2f}"
                  f"{'  ERROR ' + d['error'] if d.get('error') else ''}")
        print("\nrecent journal:")
        for r in Journal().tail(10)[::-1]:
            print(f"  {r['ts'][5:19]}  {r['label']:<24} {r['type']:<9} {r['text']}")
        return
    if a.sub in ("pause", "resume"):
        p = live_dir() / "PAUSED"
        p.touch() if a.sub == "pause" else p.unlink(missing_ok=True)
        Journal().add("*", "note", "PAUSED from CLI" if a.sub == "pause" else "resumed from CLI")
        print("paused: no orders will be sent" if a.sub == "pause" else "resumed")
        return
    if a.sub == "flatten":
        return _flatten(a)
    r = _make_runner(a, charts=a.sub != "check")
    if a.sub == "check":
        from .live import stages
        r._sync_registry()
        if not r.items:
            raise SystemExit("no running live DAGs (dagtrade live add ...)")
        for li in r.items:
            snap = li.runner.snapshot()
            print(f"\n{li.label}  [{li.mode}, {li.base_tf}]  feed {li.settings.feed}\n"
                  f"  active state: {snap['current']} (waited {snap['waited']} candles"
                  f"{', max ' + str(snap['max_wait']) if snap['max_wait'] else ''})\n"
                  f"  states: " + "  ->  ".join(f"{x['name']} [{x['state']}]" for x in stages(li)))
        return
    if a.sub == "once":
        st = r.cycle()
        print(json.dumps(st, indent=1, default=str) if a.verbose else f"cycle done: {len(st.get('deployments', []))} DAGs")
        return
    if a.sub == "run":
        _install_signal_stop(r)


def _install_signal_stop(r):
    import signal
    import threading
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print("live scanner started - Ctrl+C to stop. Local DAGs paper trade here; Alpaca DAGs use your Alpaca PAPER account.")
    r.run_forever(stop)


def _flatten(a):
    """Close every live DAG: stop it (a local DAG closes its position), and for Alpaca DAGs cancel + close at Alpaca."""
    from .live import AlpacaBroker, Journal, live_dir
    from .live.models import slug
    reg = _registry()
    items = [e for e in reg.load() if not a.args or e["id"] in a.args]
    alpaca = None
    for e in items:
        if e["mode"] == "alpaca":
            alpaca = alpaca or AlpacaBroker()
            sym = e["ticker"]
            pre = f"dt.{slug(e['id'])}."
            mine = alpaca.open_orders(sym)
            for o in mine:
                alpaca.cancel(o.id)
            if mine:
                time.sleep(2)
            q = alpaca.positions().get(sym, 0.0)
            if q:
                alpaca.submit_market(sym, "sell" if q > 0 else "buy", int(abs(q)), f"{pre}cls.flatten{int(time.time())}")
            print(f"{e['id']}: cancelled {len(mine)} Alpaca orders, closing {q:+.0f} {sym}")
        reg.update(e["id"], status="stopped")
        Journal().add(e["id"], "action", "FLATTEN (CLI): stopped" + (" and closed at Alpaca" if e["mode"] == "alpaca" else ""))
    (live_dir() / "PAUSED").touch()
    print("stopped those DAGs and paused the scanner (dagtrade live resume to re-enable). "
          "Local DAGs close their positions on the scanner's next cycle.")


def cmd_logs(a):
    from .logs import FILES, log_dir, read_logs
    if a.follow:
        import subprocess
        f = log_dir() / FILES.get(a.area, FILES["all"])
        print(f"following {f} (Ctrl+C to stop)")
        subprocess.call(["tail", "-n", str(a.n), "-F", str(f)])
        return
    rows = read_logs(a.area, a.n, a.level, a.grep or "")
    if not rows:
        print(f"no {a.area} log entries at {a.level}+ (files are in {log_dir()})")
    for r in rows[::-1]:
        print(f"{r['time']}  {r['level']:<7}  {r['logger']:<20}  {r['message']}")


def cmd_serve(a):
    import os
    import uvicorn
    if not a.no_live:
        os.environ["DAGTRADE_LIVE"] = "1"
        os.environ["DAGTRADE_DRYRUN"] = "1" if a.dry_run else "0"
    print(f"dashboard on http://{a.host}:{a.port}" + ("  (live scanner ON)" if os.environ.get("DAGTRADE_LIVE") == "1" else ""))
    uvicorn.run("dagtrade.viz.server:app", host=a.host, port=a.port, log_level="warning")


def main(argv=None):
    _load_env()
    ap = argparse.ArgumentParser(prog="dagtrade", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("data"); sp = p.add_subparsers(dest="sub", required=True)
    f = sp.add_parser("fetch"); f.add_argument("tickers", nargs="+"); f.add_argument("--tf", default="1d")
    f.add_argument("--start"); f.add_argument("--end"); f.set_defaults(fn=cmd_data)
    p = sub.add_parser("run"); p.add_argument("targets", nargs="*"); p.add_argument("--all", action="store_true")
    p.add_argument("--start"); p.add_argument("--check", action="store_true", help="also run the lookahead test")
    p.add_argument("--no-cache", action="store_true"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("check"); p.add_argument("targets", nargs="+"); p.set_defaults(fn=cmd_check)
    p = sub.add_parser("sweep"); p.add_argument("target"); p.add_argument("grid", nargs="+", help="name=v1,v2,..")
    p.set_defaults(fn=cmd_sweep)
    p = sub.add_parser("new"); p.add_argument("name"); p.add_argument("--ticker", default="SPY"); p.set_defaults(fn=cmd_new)
    p = sub.add_parser("dash"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_dash)
    p = sub.add_parser("report"); p.add_argument("run_id"); p.add_argument("--out"); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("live"); p.add_argument("sub", choices=["add", "list", "start", "stop", "remove", "mode", "check",
                                                               "once", "run", "status", "pause", "resume", "flatten",
                                                               "restart"])
    p.add_argument("--start-at", help="`add` / `restart`: begin at this state instead of the start state")
    p.add_argument("args", nargs="*", help="e.g. stochastic/SPY (add) or a DAG id like stochastic/SPY@5m")
    p.add_argument("--interval", help="candle interval for `add`, e.g. 5m"); p.add_argument("--mode", default="local", choices=["local", "alpaca"])
    p.add_argument("--flat-by-close", action=argparse.BooleanOptionalAction, default=None,
                   help="`add`: day trade (out before 16:00 ET); default: whatever the ticker's TOML says")
    p.add_argument("--dry-run", action="store_true", help="Alpaca DAGs: read the real account but only LOG the orders"); p.set_defaults(fn=cmd_live)
    p = sub.add_parser("logs"); p.add_argument("area", nargs="?", default="all",
                                                choices=["all", "errors", "backtest", "live", "data", "server", "activity"])
    p.add_argument("-n", type=int, default=50); p.add_argument("--level", default="INFO", choices=["INFO", "WARNING", "ERROR"])
    p.add_argument("--grep"); p.add_argument("-f", "--follow", action="store_true"); p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-live", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    from .logs import setup_logging
    setup_logging(verbose=a.verbose)
    if a.cmd == "run" and not a.all and not a.targets:
        ap.error("give a deployment (e.g. wick_reversal/AAPL) or --all")
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
