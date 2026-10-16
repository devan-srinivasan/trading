"""dagtrade command line.

  dagtrade list                         strategies, deployments and saved runs
  dagtrade data fetch AAPL --tf 5m      download + cache bars
  dagtrade run strategies/x/AAPL.toml   backtest (or: dagtrade run x/AAPL, or --all)
  dagtrade check x/AAPL                 lookahead test: hide the future, make sure nothing changes
  dagtrade sweep x/AAPL fast=10,20 ...  parameter grid; ranked in-sample, holdout shown beside it
  dagtrade new my_idea                  scaffold a strategy
  dagtrade dash                         open the dashboard
  dagtrade report RUN_ID                standalone HTML report

  dagtrade live check                   verify Alpaca paper keys + show what each live strategy would do right now
  dagtrade live once|run [--dry-run]    one scan cycle / the always-on scanner that trades the paper account
  dagtrade live status|pause|resume     see state, stop sending orders, start again
  dagtrade live flatten [x/AAPL]        cancel our orders + close positions (then pauses)
  dagtrade serve [--dry-run]            dashboard AND always-on scanner in one process
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


def cmd_run(a):
    from .runner import run_deployment
    deps = discover_deployments() if a.all else [resolve(t) for t in a.targets]
    for dep in deps:
        if a.start:
            dep.start = a.start
        try:
            out = run_deployment(dep, cache=not a.no_cache, check_causal=a.check)
        except Exception as e:  # noqa: BLE001
            print(f"\n== {dep.label}: FAILED - {e}")
            if a.verbose:
                raise
            continue
        print(fmt_run(out))


def cmd_check(a):
    from .data import DataSource
    from .graph import verify_causal
    from .runner import build_graph
    for t in a.targets:
        dep = resolve(t)
        g, _ = build_graph(dep)
        start = dep.start_ts() - pd.Timedelta(days=dep.warmup_days)
        rep = verify_causal(g, DataSource(), dep.ticker, start, dep.end_ts(), base_tf=dep.base_tf)
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
from dagtrade import Graph, Node, Range, Trade
from dagtrade.indicators import rsi

DESCRIPTION = "What this strategy does, in plain English."
DEFAULTS = dict(rsi_buy=30, take_profit=0.08, stop_loss=0.04)


class OversoldBounce(Node):
    history = Range(start="-60d", interval="d")          # the candles this node looks at (any interval: m 5m h d w)

    def __init__(self, rsi_buy, **kw):
        super().__init__(rsi_buy=rsi_buy, **kw)           # becomes self.rsi_buy

    def run(self, candles, upstream):                     # candles in ...
        return {{"buy": bool(rsi(candles.close, 3).iloc[-1] < self.rsi_buy)}}    # ... structured data out


def build(rsi_buy, take_profit, stop_loss):
    return Graph(Trade(OversoldBounce(rsi_buy), take_profit=take_profit, stop_loss=stop_loss))
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
def _live_deployments(only=None):
    deps = [d for d in discover_deployments() if d.live.get("enabled")]
    if only:
        deps = [d for d in deps if d.label in only]
    return deps


def _make_runner(a, charts=True):
    from .live import AlpacaBroker, DryRunBroker, LiveRunner
    deps = _live_deployments(getattr(a, "only", None))
    if not deps:
        raise SystemExit("no live deployments: add a [live] section with enabled = true to a strategy TOML "
                         "(see strategies/wick_reversal/AAPL.toml)")
    broker = AlpacaBroker()
    if getattr(a, "dry_run", False):
        broker = DryRunBroker(broker)
    return LiveRunner(deps, broker, charts=charts)


def cmd_live(a):
    from .live import live_dir
    if a.sub == "status":
        import json
        p = live_dir() / "status.json"
        if not p.exists():
            raise SystemExit("no status yet - the runner has not completed a cycle (dagtrade live run)")
        st = json.loads(p.read_text())
        age = time.time() - (st.get("heartbeat") or 0)
        print(f"mode {st.get('mode')}  heartbeat {age:.0f}s ago {'(RUNNER NOT RUNNING?)' if age > 120 else ''}  "
              f"paused={st.get('paused')}")
        if st.get("account"):
            ac = st["account"]
            print(f"account equity ${ac['equity']:,.0f}  cash ${ac['cash']:,.0f}  market {'OPEN' if st['clock']['is_open'] else 'closed'}")
        for d in st.get("deployments", []):
            dec = d.get("decision") or {}
            print(f"  {d['label']:<28} bar {d.get('last_bar','-')}  wants {dec.get('target', 0):+.0f} (${dec.get('usd', 0):,.0f})  "
                  f"position {d.get('position', 0):+.0f}  {d.get('last_action', {}) and d['last_action']['text'] or ''}"
                  f"{'  ERROR ' + d['last_error'] if d.get('last_error') else ''}")
        from .live import Journal
        print("\nrecent journal:")
        for r in Journal().tail(10)[::-1]:
            print(f"  {r['ts'][5:19]}  {r['label']:<22} {r['type']:<9} {r['text']}")
        return
    if a.sub in ("pause", "resume"):
        from .live import Journal
        p = live_dir() / "PAUSED"
        p.touch() if a.sub == "pause" else p.unlink(missing_ok=True)
        Journal().add("*", "note", "PAUSED from CLI" if a.sub == "pause" else "resumed from CLI (fresh baseline)")
        print("paused: no orders will be sent" if a.sub == "pause" else "resumed")
        return
    if a.sub == "flatten":
        return _flatten(a)
    r = _make_runner(a, charts=a.sub != "check")
    if a.sub == "check":
        ac, ck = r.broker.account(), r.broker.clock()
        print(f"Alpaca [{r.broker.name}] OK  equity ${ac.equity:,.2f}  buying power ${ac.buying_power:,.2f}  "
              f"market {'OPEN' if ck.is_open else 'closed'} (next open {ck.next_open:%a %H:%M})")
        pos = r.broker.positions()
        print(f"open positions: {pos or 'none'}")
        from .live import compute_decision
        for li in r.items:
            try:
                d = compute_decision(li, pd.Timestamp.now(tz="UTC"))
                print(f"\n{li.label}  [{li.base_tf}] order size ${d.usd:,.0f}  feed {li.settings.feed}\n"
                      f"  last closed candle {pd.Timestamp(d.avail, unit='s', tz='UTC'):%Y-%m-%d %H:%M}Z  price {d.price:.2f}\n"
                      f"  trade node wants: {d.target:+.0f}  new order now: {d.entry}  stop {d.stop}  take {d.take}\n"
                      f"  nodes: {d.nodes}")
            except Exception as e:  # noqa: BLE001
                print(f"\n{li.label}: could not compute a decision - {e}")
        return
    if a.sub == "once":
        st = r.cycle()
        print(json.dumps(st, indent=1, default=str) if a.verbose else f"cycle done: {len(st.get('deployments', []))} deployments")
        return
    if a.sub == "run":
        _install_signal_stop(r)


def _install_signal_stop(r):
    import signal
    import threading
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(f"live runner started [{r.broker.name}] - Ctrl+C to stop. Orders go to your Alpaca PAPER account.")
    r.run_forever(stop)


def _flatten(a):
    from .live import AlpacaBroker, Journal
    from .live.models import slug
    broker = AlpacaBroker()
    deps = _live_deployments(a.only or None) or discover_deployments()
    pos = broker.positions()
    for d in deps:
        sym = d.ticker
        pre = f"dt.{slug(d.label)}."
        mine = [o for o in broker.open_orders(sym) if o.client_id.startswith(pre)]
        for o in mine:
            broker.cancel(o.id)
        q = pos.get(sym, 0.0)
        if mine:
            time.sleep(2)
        if q:
            broker.submit_market(sym, "sell" if q > 0 else "buy", int(abs(q)), f"{pre}cls.flatten{int(time.time())}")
        Journal().add(d.label, "action", f"FLATTEN: cancelled {len(mine)} orders, closing {q:+.0f} {sym}")
        print(f"{d.label}: cancelled {len(mine)} orders, closing {q:+.0f} {sym}")
    from .live import live_dir
    (live_dir() / "PAUSED").touch()
    print("runner paused (dagtrade live resume to re-enable)")


def cmd_serve(a):
    import os
    import uvicorn
    if not a.no_live and _live_deployments():
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
    p = sub.add_parser("live"); p.add_argument("sub", choices=["check", "once", "run", "status", "pause", "resume", "flatten"])
    p.add_argument("only", nargs="*", help="limit to these deployments, e.g. wick_reversal/AAPL")
    p.add_argument("--dry-run", action="store_true", help="read the real account but only LOG the orders"); p.set_defaults(fn=cmd_live)
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-live", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if a.cmd == "run" and not a.all and not a.targets:
        ap.error("give a deployment (e.g. wick_reversal/AAPL) or --all")
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
