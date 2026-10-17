# dagtrade - notes for Claude

Python 3.11 in `.vnv/` (run `.vnv/bin/python`, `.vnv/bin/dagtrade`, `.vnv/bin/python -m pytest`). Package in `src/dagtrade`.

## Invariants (do not break)
- **Strategies are FSAs** (`src/dagtrade/fsa/`): states (Nodes) + edges named by outcome. ONE engine loop for backtest and live: per candle of the FSA's interval, broker processes the candle -> `FsaRunner.step` -> `planner.plan` -> broker. Backtest = `fsa/engine.backtest` (replay); live = `live/runner.LiveRunner` (same runner, state persisted).
- **Time model**: bars frames have index = candle OPEN (UTC) and `avail` = close. A state only ever gets candles with `avail <= now` (`CandleView.latest/window`); that is the whole no-lookahead guarantee. Never align by open time.
- **Fills**: decision at close of candle i -> fill at open of candle i+1. Stop beats take on the same candle. ONE set of fill rules: `sim/paper.PaperBroker` (backtests, fractional) = `LocalBroker` (live local paper, whole shares, persisted). The first backtest decision is at the close of the candle BEFORE the start.
- `dagtrade check` / `fsa.verify_causal` (replay with the future hidden; transitions + fills before the cutoff must match) must pass for every strategy; `tests/test_fsa.py` asserts it catches a leaking window.
- Daily bars are stamped 00:00 UTC of the trading date and available the next 00:00 UTC.
- Candle completeness: `DataSource.bars(end)` returns only candles whose `avail <= end`; resampled candles complete at their nominal end (capped at 16:00). Never weaken this: live correctness depends on it.

## The base API (keep it small) - `fsa/node.py`, `fsa/machine.py`
- `Node`: `history: Range`, `max_wait` (default: start state forever, others 20 candles of their interval; int candles | "2h" | None), `needs` (memory keys run() reads, checked on a mid-start), `run(candles, ctx)` -> `None` wait | `True`/dict ok | `False` fail | `Go("name", **data)`. Nodes are wired ONLY by FSA edges (positional args raise). `ctx`: Mapping over memory (earlier states' ok outputs) + `.inputs` (cash + TOML [inputs]) + `.account` (read-only snapshot) + `.now/.entered_at/.waited/.last_entry/.minutes_to_close`.
- `FSA(start, edges={node: dict(ok=, fail=, max_wait=, <name>=)})` / `FSA.chain(...)`. Defaults: ok -> start, fail -> start, max_wait -> fail's target; unknown named outcome = error. Names must be unique. A state runs when a NEW candle of its interval closed; after a transition the new state runs in the same step; no state twice per step. Memory cleared on entering start; truncated on back edges.
- `Trade(take_profit, stop_loss, notional, *, size, trail, max_hold, exit=Node)` is the only state that orders (TP/SL REQUIRED). Phases enter -> pending -> holding -> closing (`FsaRunner._trade`); it reads `ctx.account` (position, order status by token) to know fills. Emits `Intent`s (entry/close/cancel_entry/stop); `fsa/planner.plan` (PURE, shared by engine and live, re-exported as `live.reconcile.plan`) sizes them (`pct` from equity; no borrowing; live whole shares + `CASH_BUFFER`). A new instruction type = extend Trade/Intent + planner + PaperBroker + broker adapters.
- `flat_by_close` is an engine rule in `FsaRunner` (no entries in the last 30 min / 2-candle exit window; close 2 candles before 16:00), not a node. Per-interval settings live in `<TICKER>@<interval>.toml`. `Range(candles=N)` / no interval = timeframe-agnostic.
- Optional sugar (`nodes/`: `Indicator` with `check()` + `.as_exit()`, `RegimeNode`, `SignalNode`; `indicators.py`) is built ON the base; the core must never import it.
- Results: FILLS ARE THE ONLY SOURCE of metrics. Backtest (`runner.run_deployment`) and live (`viz/server._live_report`) both call `analytics/ledger.ledger_report(fills, candles, cash0, ...)`. `test_metrics_come_only_from_fills_and_match_the_broker` guards parity. Charts: `runner.node_frame(fsa, trace, close)` -> `fsa.state` pane + `state.key` outputs while remembered + `trade.stop/take`.

## Live (paper) trading - `src/dagtrade/live/`
- Which DAGs are live: `live/active.json` (`registry.py`), edited by the dashboard / `dagtrade live add|stop|restart|...` and re-read every cycle. Entry: strategy, ticker, interval, flat_by_close, start_at, inputs, mode (`local` | `alpaca`), status, cash, optional `restart` request {at, memory, nonce} applied once.
- Per DAG `live/state/<slug>.json` (`DepState`): FSA runner state, outstanding intents, events, trace. Each cycle: new candles (max `MAX_CATCH_UP`) -> local broker `on_candles` -> `runner.step` -> intents queued; every cycle `plan(intents)` -> `_execute`. Unsent entries from an older candle are dropped as obsolete.
- Only `AlpacaBroker` touches Alpaca, always `paper=True`. Never add a live-money path unless asked. Orders use deterministic `client_order_id`s `dt.<slug>.<kind>.<token>` (token = decision epoch).
- The runner writes `live/status.json`, `live/dags/<dag>.json` (fsa snapshot, `stages`, `dag` describe, events, intents, orders, account, equity) and `live/journal.jsonl` (transition / intent / action / fill / note / error); the dashboard only reads these files + the registry.
- Failures: transient -> immediate retries -> re-read the account and re-plan (`_one(redo=True)`) -> 10 s pause (intents kept); permanent -> 60 s pause (intent dropped; the Trade state then fails back).
- Tests: `tests/test_live.py` (planner, scheduler, retries, Alpaca requests) and `tests/test_local_live.py` (local engine, registry, restart, registry-driven runner).

## Dashboard - `src/dagtrade/viz/`
Plain JS, no build step: `static/js/{core,run,backtests,live,dags,logs,main}.js` share one global scope and load in that order (`index.html`, and `report.py` inlines the same list). `fsaDiagram()` in run.js draws any FSA describe() (old DAG runs: inputs -> ok edges).

## Adding things
- State: subclass `dagtrade.Node`, set `history = Range(...)`, implement `run(candles, ctx)`. Params are constructor keywords.
- Strategy: `strategies/<name>/strategy.py` (`build(**params) -> FSA`, `DEFAULTS`, `DESCRIPTION`) + `<TICKER>.toml` / `<TICKER>@<tf>.toml`. `dagtrade new <name>` scaffolds.
- Data provider: implement `fetch(ticker, tf, start, end)` in `data/providers.py` (see `SyntheticProvider` for tests/offline).

## Tests
`pytest` uses `SyntheticProvider` (no network). Real-data checks: `dagtrade run --all --check`.

## Secrets
`.env` holds API keys (ALPACA_KEY/SECRET, legacy Alpha Vantage keys). It is git-ignored; never print or commit it.
