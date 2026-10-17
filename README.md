# dagtrade

Build a trading strategy as a small **state machine** of nodes (each looks at candles and decides: not yet / success / failure) ending in a **Trade** that places the order. Backtest it on any ticker, see every trade on a chart against the S&P 500 and buy-and-hold, and run it live on paper: our own local engine or an Alpaca **paper** account.

```
opening_range ──ok──► gap_break ──ok──► retest_engulf ──ok──► Trade ──► order
     ▲                    ▲   └──fail───┐      │                 │
     │                    └─fail / max_wait────┘                 │
     └───────────────────────── done (back to the start) ────────┘
```

## 1. Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .

dagtrade list                  # strategies already included
dagtrade run stochastic/SPY    # backtest one (data downloads automatically, no keys needed for daily candles)
dagtrade dash                  # open http://127.0.0.1:8765 to see the chart, trades and metrics
```

## 2. General structure: strategies and tickers

```
strategies/
  stochastic/
    strategy.py     <- the logic (nodes wired together). One per strategy.
    SPY.toml        <- this strategy on SPY (daily candles)
    SPY@5m.toml     <- the same strategy on SPY's 5-minute candles, with its own settings (optional)
```

- **`strategy.py`** defines *what the strategy does*: it builds the state machine and returns it. It does not fix a timeframe: the same strategy runs on daily, hourly or 5-minute candles.
- **`<TICKER>.toml`** defines *where and how it runs*: the ticker (taken from the file name), the dates, the candle interval, and optional parameter overrides. It contains no logic. `<TICKER>@<interval>.toml` is used when you run that interval (e.g. a tighter stop loss on 5m candles).

**To run an existing strategy on a new ticker**: add one file, `strategies/stochastic/TSLA.toml`:
```toml
start = "2018-01-01"            # the only required line
```
then `dagtrade run stochastic/TSLA`. Nothing else changes.

**To create a new strategy**: `dagtrade new my_idea --ticker AAPL` creates `strategy.py` and `AAPL.toml` from a template. Edit `strategy.py`, then `dagtrade run my_idea/AAPL`.

Everything you can put in a `.toml` (all optional except `start`):
```toml
start = "2018-01-01"     # backtest start
end = "2024-12-31"       # default: today
base_tf = "1h"           # candle interval the strategy runs on (default: daily; set by the file name for SPY@1h.toml)
flat_by_close = true     # day trading: no new entries in the last 30 min, out before 16:00 ET (intraday candles only)
size = 0.75              # each order = 75% of the account (reinvests profits); backtests then start with $10,000
start_at = "gap_break"   # begin the first pass at this state instead of the start state
benchmark = "SPY"        # what to compare against
holdout = 0.2            # last 20% of the period is reported separately (overfitting check)

[params]                 # overrides for the DEFAULTS in strategy.py
fast = 10

[inputs]                 # extra values every state sees as ctx.inputs (next to "cash")
risk = 0.01

[sim]                    # trading costs in basis points
commission_bps = 1.0
slippage_bps = 2.0

[live]                   # leave out to never trade live
enabled = true
```

## 3. Strategies are state machines

A strategy is a few **states** (Nodes) connected by **edges**. Only one state is active at a time. Every time a candle of its interval closes, the active state looks at its candles and answers:

| It returns | Meaning |
|---|---|
| `None` | not yet: stay, ask again next candle |
| `True` or a dict | success: move along its `ok` edge (the dict is passed on to every later state) |
| `False` | failure: follow its `fail` edge |
| `Go("name", ...)` | a named outcome, for branching (`edges={node: {"name": other_node}}`) |

A state that keeps saying `None` longer than its **`max_wait`** fails with the outcome `max_wait` (so old setups go stale). Defaults: the start state waits forever, every other state waits at most 20 candles of its own interval; set `max_wait = 5`, `"2h"`, `"3d"` or `None` (forever) per state. Edges you don't write: `ok` and `fail` go back to the start, `max_wait` goes wherever `fail` goes. The **`Trade`** state places the order its predecessor described, holds the position until take-profit / stop-loss / its `exit` check / `max_hold` / the close, then follows `ok` (by default: back to the start).

```python
from dagtrade import FSA, Node, Range, Trade

class OpeningRange(Node):                       # start state: waits until today's first 5m candle has closed
    history = Range(start="-1d", interval="5m")
    def run(self, candles, ctx):
        ...
        return {"high": hi, "low": lo}          # success -> gap_break, which reads it as ctx["opening_range"]

class GapBreak(Node): ...                       # waits for a breakout that leaves a gap
class RetestEngulf(Node):                       # waits for the retest, then returns the order
    max_wait = 60                               # 60 candles without it: give up
    ...                                         # return {"sell": True, "stop_loss_price": s, "take_profit_price": t}

def build():
    orb, gap, eng = OpeningRange(), GapBreak(), RetestEngulf()
    return FSA(start=orb, edges={
        orb: dict(ok=gap),
        gap: dict(ok=eng),                                        # fail (day over) -> back to the start
        eng: dict(ok=Trade(take_profit=0.05, stop_loss=0.02),     # TP/SL required; the signal can give exact prices
                  fail=gap, max_wait=gap),                        # missed it: look for the next gap
    })
```
(The full version is `strategies/orb_fvg/`.) A plain sequence is `FSA.chain(a, b, c, Trade(...))`, with `fail={c: b}` for back edges.

**What a state sees**: `candles` (its `Range`, newest last; it can never see a candle that has not closed), `ctx["state_name"]` (outputs of earlier states this pass), `ctx.inputs` (starting cash plus the TOML's `[inputs]` table), `ctx.account` (cash, equity, position: read-only), `ctx.now`, `ctx.entered_at`, `ctx.waited`, `ctx.last_entry`.

**The order** (the dict the state before `Trade` returns): `"buy"` / `"sell"`, `"price"` (limit entry), `"usd"` / `"qty"` / `"pct"` (size; `"pct": 0.5` = half the account), `"take_profit"` / `"stop_loss"` (fractions) or `"take_profit_price"` / `"stop_loss_price"`, `"trail"`, `"max_hold"`. `Trade(size=0.75)` trades 75% of the account each time (profits reinvested). The broker side (backtest, local paper or Alpaca) turns `%` into dollars when the order is placed and never spends more cash than the account has.

**Indicator strategies** use the optional helper `dagtrade.nodes.Indicator`: write `check(candles) -> {"buy": ..., "exit": ...}` once; as a state it waits for `buy`, and `.as_exit()` is the same rule as the Trade's exit check:
```python
sig = GoldenCross(fast=50, slow=200)
return FSA.chain(sig, Trade(take_profit=1.0, stop_loss=0.10, exit=sig.as_exit()))
```

**Range**: `candles=200` (a count) or start/end (`"2024-01-01"`, `"2024-03-01-09-30"`, relative `"-30d"` / `"-12h"`). Leave out `interval` and the state runs on the interval you pick for the run, so the strategy works on any timeframe; set it (`m 5m 15m h 4h d w`) only when a state needs one specific timeframe.

**Start midway**: a backtest (Advanced options, or `start_at = "state"` in the TOML) or a live DAG (`Restart at` on its page, `dagtrade live restart ID --start-at STATE`) can begin at any state; give the outputs it reads (`needs = ("opening_range",)`) as JSON.

## 4. Commands

**Backtest**
```bash
dagtrade run my_idea/AAPL              # one strategy on one ticker (my_idea/AAPL@5m for its 5m setup)
dagtrade run --all                     # every strategy/ticker pair
dagtrade run my_idea/AAPL --check      # also replay with the future hidden: nothing may change
dagtrade sweep my_idea/AAPL drop=0.02,0.03,0.04    # try parameters
dagtrade dash                          # dashboard: charts, trades, metrics
dagtrade report <run_id>               # one run as a single shareable HTML file
```

**Live paper trading**

Run `dagtrade serve` and open the **Live** tab. Add a DAG (strategy, ticker, interval) and it starts trading on the next closed candle. Where its orders go is one switch, **Send orders to Alpaca** (on the DAG's page and in the live table):
- **Off** (default, local paper): our own engine trades on live Alpaca candles with its own **$10,000 account per DAG**. Fills follow the backtest rules (next candle's open; take-profit / stop-loss against each candle's high and low). Nothing is sent to Alpaca.
- **On** (Alpaca paper): the local engine for that DAG stops (closing any local position) and its orders go to your Alpaca paper account, with take-profit and stop-loss attached.

Each live DAG gets a page with its state machine (the active state, how long it has waited, what earlier states found), every transition in its activity log, its live chart, its trades and its orders, and a **Restart at [state]** control. Its state is saved after every candle, so restarting the server resumes where it was. Pages refresh every 10 s (or press Refresh). Stop / Start / Remove per DAG.

**Every number comes from orders.** A backtest and a live DAG run the same state machine and both produce a list of fills (time, side, qty, price, fee); the backtest replays history candle by candle through the same paper broker the local engine uses. Returns, Sharpe, drawdown, win rate, alpha vs SPY, and the equity curve against buy-and-hold are all computed from those fills alone (`analytics/ledger.py`). It doesn't matter whether they came from the simulator, the local engine or Alpaca. On a live page you choose the window (today, 1 week, 1 month, since start).

Setup for live data and Alpaca mode:
1. Make free Alpaca paper keys (alpaca.markets, Paper Trading, API keys). No OAuth or token: just a key ID and secret.
2. `cp .env.example .env` and fill in `ALPACA_KEY=` (the key ID) and `ALPACA_SECRET=` (the secret). Alpaca's own names `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` work too. (`.env` is git-ignored.)
```bash
dagtrade serve                                   # dashboard + always-on scanner (or: dagtrade dash, then "Start scanner")
dagtrade live add stochastic/SPY --interval 5m  # the same from the terminal (--flat-by-close, --mode local|alpaca)
dagtrade live list | stop ID | start ID | remove ID | mode ID alpaca
dagtrade live check                              # where each live DAG's state machine is (sends nothing)
dagtrade live restart ID --start-at gap_break    # restart a live DAG's state machine at a state
dagtrade live pause | resume                     # stop / restart sending orders for every DAG
dagtrade live flatten                            # stop everything; Alpaca DAGs are closed at Alpaca
```
Only the paper account is ever used. One Alpaca-mode DAG per ticker (local DAGs have separate accounts, so they can share a ticker).

**Intraday data**: daily candles need no keys. For minute/hour candles over long periods, the Alpaca keys above are used for historical data too.

**Tests**: `pytest`
