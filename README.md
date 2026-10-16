# dagtrade

Build a trading strategy as a small chain of **nodes** (candles in, structured data out) ending in a **Trade** that places the order. Backtest it on any ticker, see every trade on a chart next to the S&P 500 and buy-and-hold, and optionally let it trade an Alpaca **paper** account.

```
daily candles -> Uptrend {"up"} ─┐
                                 ├─► Dip {"buy"} ─► Trade(take-profit, stop-loss) ─► order
hourly candles ──────────────────┘
```

---

# 1. Quick start (built-in strategies)

```bash
source .vnv/bin/activate                 # or call .vnv/bin/dagtrade directly
dagtrade list                            # strategies x tickers already set up
dagtrade run sma_cross/SPY               # backtest one (data downloads and caches automatically)
dagtrade run --all --check               # everything, plus the lookahead test
dagtrade dash                            # http://127.0.0.1:8765: leaderboard, charts, trades
```

**Your first strategy takes two files.** A built-in signal plus a `Trade`:

`strategies/my_idea/strategy.py`
```python
from dagtrade import Graph, Trade
from dagtrade.nodes import RsiOversold

DEFAULTS = dict(buy_below=30, take_profit=0.08, stop_loss=0.04)

def build(buy_below, take_profit, stop_loss):
    return Graph(Trade(RsiOversold(buy_below=buy_below), take_profit=take_profit, stop_loss=stop_loss))
```
`strategies/my_idea/AAPL.toml` (this strategy on one ticker; add more files for more tickers)
```toml
start = "2018-01-01"
```
```bash
dagtrade run my_idea/AAPL                # backtest
dagtrade sweep my_idea/AAPL buy_below=20,25,30,35     # try parameters (ranked in-sample, holdout shown beside it)
dagtrade new another_idea --ticker TSLA  # scaffold a fresh strategy from a template
```

**Built-in nodes** (`from dagtrade.nodes import ...`, all take keyword parameters):

| Node | What it does |
|---|---|
| `SmaCross(fast=20, slow=50)` | buy when the fast average crosses above the slow one, exit when it crosses back |
| `RsiOversold(n=14, buy_below=30, exit_above=55)` | buy oversold, exit on recovery |
| `DipBuy(drop=0.03)` | buy a bounce after a `drop` from the recent high; if an upstream regime node says "not up", it stays out |
| `BreakoutBuy()` | buy when the close beats the highest high of the candles before it |
| `TrendRegime(fast=20, slow=100, band=0.01)` | labels the market `"up"` / `"down"` / `"range"` (feed it into another node) |

Chain them: `Trade(DipBuy(TrendRegime(), drop=0.04), take_profit=0.12, stop_loss=0.06)` means "only buy dips while the trend regime is up". See `strategies/` for ready-made examples (`sma_cross`, `regime_dip`, `wick_reversal`, `window_breakout`, `trend_pullback`, `buy_and_hold`).

**What the `Trade` does.** `take_profit` and `stop_loss` are required (fractions of the entry price: 0.10 = +10%); everything else defaults ($1000 per order, market entry). In a backtest, a candle whose high or low touches your take-profit or stop-loss ends the trade, filled at that price (at the open if the candle gapped through it; the stop wins if one candle touches both).

**Reading a result without fooling yourself**
1. Compare to buy & hold *and* SPY on the same dates. Beating cash is not an achievement.
2. The last 20-25% of the period is reported separately as a **holdout**. Great in-sample and bad holdout means overfitting.
3. **Beat-random %**: the same exposure at random times. Under ~90% you can't tell skill from luck.
4. Under ~30 trades, nothing is statistically meaningful.
5. The more parameter sets you sweep, the more likely the best one is luck.

Glossary: *lookahead* (using information not yet known: the #1 way backtests lie; `dagtrade check` hunts for it), *overfitting* (tuning to the past's noise), *slippage* (you rarely get the exact price), *Sharpe* (return per unit of volatility; >1 decent), *max drawdown* (worst peak-to-trough loss), *beta/alpha* (how much of your result is just the market vs extra), *ATR* (typical candle size).

---

# 2. Subclassing and custom logic

## The model
- **`Node`**: candles in, structured data out. `run(candles, upstream)` is called every time a candle of the node's interval closes.
- **`Range`**: which candles a node looks at.
- **`Trade`**: the last node; turns a signal into an order.

```python
from dagtrade import Graph, Node, Range, Trade

class Uptrend(Node):
    history = Range(start="-200d", interval="d")          # the last 200 days of DAILY candles
    def run(self, candles, upstream):                      # candles: open/high/low/close/volume, newest last
        return {"up": candles.close.iloc[-1] > candles.close.mean()}      # any structured output

class Dip(Node):
    history = Range(start="-10d", interval="h")           # hourly candles; this node runs every hour
    def __init__(self, trend, drop, **kw):
        super().__init__(trend, drop=drop, **kw)           # keyword params become attributes (self.drop)
    def run(self, candles, upstream):
        dip = candles.close.iloc[-1] < (1 - self.drop) * candles.close.max()
        return {"buy": upstream["uptrend"]["up"] and dip}  # sees Uptrend's latest output, keyed by node name

strategy = Graph(Trade(Dip(Uptrend(), drop=0.03), take_profit=0.10, stop_loss=0.05))
```

- **`run(candles, upstream)`**: return a dict (or anything picklable), or `None` for "nothing to say". It is not called until every upstream node has produced its first output. Nodes are stateless: "waiting" means being re-evaluated on every candle, gated by what upstream said. If a node must remember something, make its `Range` long enough to see it.
- **Waiting for upstream.** Before a node looks at any candles it checks its upstream: it is skipped (output `None`) until every upstream node has produced an output, and then until `ready(upstream)` is true. Override it to wait for a success criterion, e.g. `def ready(self, upstream): return upstream["trend_waiter"]["ok"]`; `run` is then only called on candles where the waiter succeeded, and nodes further down wait too.
- **Mixing timeframes is safe.** An hourly node receives the daily node's output only after that daily candle has closed. A node can never see a candle that has not closed yet.
- **Several upstream nodes** are keyed by name (default: the class name in snake_case, e.g. `Uptrend` -> `"uptrend"`; pass `name="x"` to rename). Names must be distinct.
- **Helpers** for use inside `run`: `from dagtrade.indicators import sma, ema, rsi, atr`.
- **`dagtrade check`** re-runs the strategy with the future hidden at several points; if any node's past outputs change, that node is named. Run it on anything new.

## Range
`Range(start=None, end="now", interval="d")`

| `start` / `end` | meaning |
|---|---|
| `"2024-01-01"`, `"2024-03-01-09-30"` (`yyyy-mm-dd-hh-mm`) | an absolute moment (UTC). An absolute `start` is a fixed anchor, so the window grows. |
| `"-30d"`, `"-12h"`, `"-45m"`, `"-4w"` | that long before the moment the node is deciding: the window slides. |
| `"now"` (default `end`) / `None` start | the deciding moment / as far back as data is loaded |

`interval`: `"m" "5m" "15m" "30m" "h" "4h" "d" "w"`.

## Optional building blocks (nothing in the core depends on them)
`RegimeNode` and `SignalNode` are five-line subclasses of `Node`; copy them freely:
```python
from dagtrade.nodes import RegimeNode, SignalNode

class Calm(RegimeNode):                                    # output {"regime": <label>}
    history = Range(start="-60d", interval="d")
    def classify(self, candles, upstream):
        return "calm" if candles.close.pct_change().std() < 0.01 else "wild"

class BigDrop(SignalNode):                                 # output {"buy": bool}, or return your own dict
    history = Range(start="-20d", interval="d")
    def signal(self, candles, upstream):
        return upstream["calm"]["regime"] == "calm" and candles.close.iloc[-1] < 0.95 * candles.high.max()
```
For anything else (an LLM classifier, news, outside data) subclass `Node` and override `compute(ctx, *upstream)` to take over completely: you get the whole aligned history and `ctx.live` (True when driving the live runner). Results are cached on disk, so slow nodes only run once per input; mind lookahead: only use information available at each candle's close.

## Trade: richer instructions
`Trade(signal, take_profit, stop_loss, notional=1000, trail=False, max_hold=None)`

The signal node's dict can override any of it for that one order:

| key | meaning |
|---|---|
| `"buy": True` / `"sell": True` | open a long / short (a fresh signal only, and only when flat) |
| `"exit": True` | close the position now |
| `"price": 100` | limit entry: fills only if the next candle trades through it, otherwise it expires |
| `"usd": 750` or `"qty": 5` | order size (default `notional`) |
| `"take_profit_price": 110`, `"stop_loss_price": 95` | absolute exit prices (or `"take_profit"` / `"stop_loss"` as fractions) |
| `"trail": True`, `"max_hold": 10` | trailing stop / close after N candles |

The simplest hand-written trade: `{"buy": True, "price": 100, "take_profit_price": 110, "stop_loss_price": 95}`. Inconsistent levels (a long whose stop is above the price) raise an error; unknown keys are ignored. `Trade` never talks to a broker: it states the order, and the backtest simulator or the live Alpaca runner carries it out. To support a new *kind* of instruction (scaling in/out, say), extend `Trade` (`graph/node.py`), the simulator kernel (`sim/broker.py`) and the live planner (`live/reconcile.py`) together.

## What the backtester assumes
Each order is `$notional / price` shares. A decision at a candle's close fills at the **next** open (or at your limit price). 1 bp commission and 2 bps slippage per side (`[sim]` in the TOML; starting cash defaults to the order size so results compare with buying the stock with the same dollars). Stops fill at the stop (or the open if it gapped through); a candle touching both TP and SL counts as the stop; after a stop-out the strategy stays flat until its signal changes. Shorting has no borrow fees. Prices are split/dividend adjusted.

## Per-strategy files
```
strategies/<name>/strategy.py     build(**params) -> Graph, plus DEFAULTS and DESCRIPTION
strategies/<name>/<TICKER>.toml   start/end, base_tf, [params], [sim] costs, optional [live]
```
One ticker per strategy; as many strategy x ticker pairs as you like. Every run is saved under `runs/` and shows up in the dashboard (candlestick chart with entries/exits/TP-SL lines, each node's output under it, equity vs SPY and buy-and-hold, drawdown, scorecard, trade table, DAG diagram). `dagtrade report <run_id>` exports one run as a single HTML file.

---

# 3. What you need to use it

**To backtest on daily candles: nothing.** Dependencies are already in `.vnv`; daily data comes from Yahoo Finance with no key.

**For intraday candles (minutes/hours) over long periods: a free Alpaca account's API keys.** Yahoo only serves ~7 days of 1-minute, ~60 days of 5-30 minute, and ~2 years of hourly history.

1. Sign up at https://alpaca.markets (free; no deposit is needed for paper trading).
2. In the dashboard switch to **Paper Trading** and generate an **API key ID and secret**.
3. `cp .env.example .env` and fill in:
   ```
   ALPACA_KEY=your-paper-key-id
   ALPACA_SECRET=your-paper-secret
   ALPACA_FEED=sip      # backtests: consolidated data (falls back to iex if your plan refuses it)
   ```
   (`.env` is git-ignored. Never commit keys.)

**That is all.** There is **no OAuth and no access token to fetch**: Alpaca's Trading and Market Data APIs authenticate with just that key ID + secret (sent as the `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers; the Python SDK does it for you). OAuth / the Connect API is for third-party apps that act on *other people's* accounts. The same paper keys cover both the paper trading API (`paper-api.alpaca.markets`) and market data (`data.alpaca.markets`). Paper and live accounts have separate keys; **dagtrade only ever uses paper** (the client is created with `paper=True`, and no real-money path exists).

## Live paper trading (optional)
The DAG decides, the backtester tests it, and live mode sends the same decisions to your Alpaca paper account.

```
candle closes -> run the DAG on fresh candles -> Trade says "new order now"
              -> ONE order (market, or limit if the signal gave a price) carrying its own stop-loss / take-profit
                 (Alpaca bracket order) -> journal + dashboard
```
1. Put your paper keys in `.env` (above).
2. Opt a strategy in, in its TOML (nothing trades unless `enabled = true`):
   ```toml
   [live]
   enabled = true
   # feed = "iex"       # live data feed: iex (real-time, free) or sip (15-min delayed on the free plan)
   ```
3. `dagtrade live check`: verifies the keys and prints what each live strategy would do right now (sends nothing).
4. `dagtrade live once --dry-run`: one full scan that only **logs** the orders it would send.
5. `dagtrade serve`: dashboard **and** the always-on scanner (http://127.0.0.1:8765/#/live). Or headless: `dagtrade live run`.

**Always on.** It polls every ~20 s while the market is open (every minute when closed), acts only when a new candle has closed, survives restarts (state in `live/`), and refuses to run twice. To keep it alive across reboots on macOS use `launchd` (`ProgramArguments` = `[".../.vnv/bin/dagtrade", "serve"]`, `WorkingDirectory` = this repo, `KeepAlive` = true). Daily strategies decide after the daily candle is final (~8 pm ET) and queue for the next open.

**What it does** (`src/dagtrade/live/reconcile.py`, ~60 lines):
- *Entry*: the Trade placed a new order on the candle that just closed and you are flat -> one order for `notional / price` whole shares with take-profit and stop-loss attached; Alpaca manages the exits. A limit entry that does not fill stays working until Alpaca's day rules cancel it, slightly longer than the one candle a backtest allows.
- *Exit*: the strategy asks to leave (`exit` signal / `max_hold`) while you hold a position -> cancel the exit legs, then close at market.
- Order ids are deterministic (`dt.<strategy>-<ticker>.<kind>.<candle>`), so the same decision is never sent twice. One strategy per symbol per account; don't hand-trade that symbol.
- **If an order fails**: a transient error (timeout, 429/5xx) is retried immediately; if that fails the whole DAG is re-run on fresh candles and the order re-planned in the same cycle; still failing: pause 10 s and repeat from scratch (an unsent entry is dropped once a newer candle makes it obsolete). A permanent rejection (403 buying power, 422) is not retried (60 s pause).
- *Safety*: paper-only; per-strategy opt-in; stale decisions (>72 h) never open positions; `dagtrade live pause` (or the dashboard's *Pause trading*) stops all orders; `dagtrade live flatten [x/AAPL]` cancels orders and closes positions, then pauses.

**Live vs backtest.** Live uses Alpaca's free **IEX** feed by default (real-time but one venue, so prices/volume differ slightly from backtest data). Paper fills are simulated against the live quote, sometimes partial, with no dividends or borrow fees. The live chart (`#/run/live_...`) re-runs the backtest on fresh candles whenever one closes and overlays your **real paper fills** as violet squares so you can compare live with the simulation.

## Command reference
```
dagtrade list | run <x/AAPL>... [--all] [--check] | check <x/AAPL> | sweep <x/AAPL> name=v1,v2 ...
dagtrade new <name> [--ticker T] | data fetch AAPL --tf 5m | dash | report <run_id>
dagtrade live check|once|run|status|pause|resume|flatten [--dry-run]   |   dagtrade serve [--dry-run] [--no-live]
pytest                                                                  # the test suite
```

## Layout
```
src/dagtrade/{data,graph,nodes,sim,analytics,store,viz,live}   engine (live = Alpaca paper runner)
strategies/<name>/{strategy.py,TICKER.toml}                     your strategies
data/ runs/ .cache/ live/                                       generated (git-ignored)
```
Not (yet) included: real-money trading, multi-asset portfolios, streaming (it polls candles), fractional shares live.
