# dagtrade - notes for Claude

Python 3.11 in `.vnv/` (run `.vnv/bin/python`, `.vnv/bin/dagtrade`, `.vnv/bin/python -m pytest`). Package in `src/dagtrade`.

## Invariants (do not break)
- **Time model**: node outputs are indexed by *availability time* (bar close). `executor.asof_align` is the only place timeframes mix. Bars frames: index = bar OPEN (UTC), `avail` column = close. Never align by open time.
- **Fills**: decision at close of bar i -> fill at open of bar i+1 (`sim/broker.py`). Stop beats take on the same bar.
- Every node must be causal. `dagtrade check` / `graph.verify_causal` (truncation test) must pass for every strategy; tests in `tests/test_graph.py` assert it catches leaks.
- Never `bool(node)` / `node in list` / `==` for identity: nodes overload operators. Use `id(node)`.
- Daily bars are stamped 00:00 UTC of the trading date and available the next 00:00 UTC.

## The base API (keep it small)
`Node` (`graph/node.py`): `history: Range`, `run(candles, upstream) -> structured output`; engine aligns outputs as-of. `Trade(signal, take_profit, stop_loss, notional, trail, max_hold)` is the only terminal (TP/SL REQUIRED); per-signal dict keys override defaults (price=limit entry, usd/qty, absolute or fractional TP/SL). It emits target/usd/stop/take/limit/entry; the simulator and live planner consume exactly those, so a new instruction type means extending Trade + `sim/broker.py` + `live/reconcile.py`. Optional sugar (`nodes/`, `indicators.py`) is built ON the base; the core must never import it. Outputs are object Series of dicts; the dashboard flattens them to `node.key` columns (`runner.node_frame`).
Candle completeness: `DataSource.bars(end)` returns only candles whose `avail <= end`; resampled candles complete at their nominal end (capped at 16:00). Never weaken this: live correctness depends on it.

## Live (paper) trading - `src/dagtrade/live/`
- `reconcile.plan()` is PURE: entry = one bracket/OTO market order on a fresh Trade `entry`; exit = cancel legs then close. Keep it tiny and unit-tested (`tests/test_live.py`).
- Only `AlpacaBroker` touches Alpaca, always `paper=True`. Never add a live-money path unless asked. Orders use deterministic `client_order_id`s `dt.<slug>.<kind>.<token>`.
- Failures: transient -> immediate retries -> re-run whole DAG (`LiveRunner._one(redo=True)`) -> 10 s pause; permanent -> 60 s pause. Unsent entries are `pending` until a newer candle supersedes them.
- State in `live/` (git-ignored); live candles use `data/live_bars/`. Without keys test with `FakeBroker` + the scripted strategy in `tests/test_live.py`.

## Adding things
- Node: subclass `dagtrade.Node`, set `history = Range(...)`, implement `run(candles, upstream)`. Params are constructor keywords.
- Strategy: `strategies/<name>/strategy.py` (`build`, `DEFAULTS`, `DESCRIPTION`) + `<TICKER>.toml`. `dagtrade new <name>` scaffolds.
- Data provider: implement `fetch(ticker, tf, start, end)` in `data/providers.py` (see `SyntheticProvider` for tests/offline).

## Tests
`pytest` uses `SyntheticProvider` (no network). Real-data checks: `dagtrade run --all --check`.

## Secrets
`.env` holds API keys (ALPACA_KEY/SECRET, legacy Alpha Vantage keys). It is git-ignored; never print or commit it.
