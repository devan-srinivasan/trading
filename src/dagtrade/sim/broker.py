"""Bar-based broker simulator.

Rules (all deliberately conservative / realistic):
  * A decision made at the CLOSE of candle i executes at the OPEN of candle i+1 (never at the price you just saw).
    Trading is enabled from `trade_from`: the first fill can be at that candle's open, decided at the previous close.
  * An order is `side x usd`: shares = usd / fill price (fractional shares are fine in the simulation).
  * A market entry fills at the next open; a LIMIT entry fills only if that candle trades through the limit price
    (at the open if it gapped through), otherwise it expires.
  * Slippage is applied against you on market fills; commission is bps of traded notional.
  * Stops/takes set by the decision at candle i are live during candle i+1 onward, checked against its high/low.
    Gaps through a stop fill at the open (worse than the stop). If a candle touches both, the STOP wins.
  * After a stop/take exit the strategy stays flat until its target changes (no instant re-entry loop).
  * Anything still open on the last candle is closed at its close so the ledger is complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numba as nb
import numpy as np
import pandas as pd

R_SIGNAL, R_STOP, R_TAKE, R_END = 0, 1, 2, 3
REASONS = {R_SIGNAL: "signal", R_STOP: "stop", R_TAKE: "take", R_END: "end"}


@dataclass
class SimConfig:
    capital: float = 1000.0            # starting cash (the runner sets it to the order size unless you override)
    commission_bps: float = 1.0        # per side, of notional
    slippage_bps: float = 2.0          # per market fill, against you
    allow_short: bool = True


@nb.njit(cache=True)
def _simulate(o, h, l, c, target, usd, stop, take, limit, trade_from, capital, comm, slip, allow_short):
    n = len(o)
    equity = np.empty(n)
    shares_end = np.empty(n)
    cap_fills = 3 * n + 4
    f_bar = np.empty(cap_fills, np.int64)
    f_qty = np.empty(cap_fills)
    f_px = np.empty(cap_fills)
    f_reason = np.empty(cap_fills, np.int64)
    f_comm = np.empty(cap_fills)
    nf = 0
    cash = capital
    sh = 0.0
    lock = False         # True after a stop/take exit, until the target changes
    lock_side = 0.0

    for j in range(n):
        # ---- 1. execute the decision made at the close of candle j-1, at this candle's open
        if j >= 1 and j >= trade_from:
            t = target[j - 1]
            side = 0.0
            if t == t:
                side = 1.0 if t > 0 else (-1.0 if t < 0 else 0.0)
            if side < 0 and not allow_short:
                side = 0.0
            if lock and side != lock_side:
                lock = False
            cur = 1.0 if sh > 0 else (-1.0 if sh < 0 else 0.0)
            if not lock and side != cur:
                if sh != 0.0:                                   # close what we hold
                    q = -sh
                    px = o[j] * (1.0 + slip) if q > 0 else o[j] * (1.0 - slip)
                    cm = abs(q) * px * comm
                    cash -= q * px + cm
                    f_bar[nf] = j; f_qty[nf] = q; f_px[nf] = px; f_reason[nf] = R_SIGNAL; f_comm[nf] = cm
                    nf += 1
                    sh = 0.0
                if side != 0.0 and o[j] > 0:                    # open the new position
                    lim = limit[j - 1]
                    is_limit = lim == lim
                    base = o[j]
                    filled = True
                    if is_limit:                                # limit entry: only if this candle trades through it
                        if side > 0:
                            filled = l[j] <= lim
                            base = min(o[j], lim)
                        else:
                            filled = h[j] >= lim
                            base = max(o[j], lim)
                    if filled:
                        u = usd[j - 1]
                        if u != u or u <= 0:
                            u = capital
                        q = side * u / base
                        px = base if is_limit else (base * (1.0 + slip) if q > 0 else base * (1.0 - slip))
                        cm = abs(q) * px * comm
                        cash -= q * px + cm
                        sh += q
                        f_bar[nf] = j; f_qty[nf] = q; f_px[nf] = px; f_reason[nf] = R_SIGNAL; f_comm[nf] = cm
                        nf += 1
        # ---- 2. intrabar stop / take using levels decided at the close of candle j-1
        if sh != 0.0 and j >= 1 and j >= trade_from:
            s = stop[j - 1]
            t = take[j - 1]
            exit_px = np.nan
            reason = -1
            if sh > 0:
                if s == s and l[j] <= s:
                    exit_px = min(o[j], s) * (1.0 - slip)
                    reason = R_STOP
                elif t == t and h[j] >= t:
                    exit_px = max(o[j], t)
                    reason = R_TAKE
            else:
                if s == s and h[j] >= s:
                    exit_px = max(o[j], s) * (1.0 + slip)
                    reason = R_STOP
                elif t == t and l[j] <= t:
                    exit_px = min(o[j], t)
                    reason = R_TAKE
            if reason >= 0:
                q = -sh
                cm = abs(q) * exit_px * comm
                cash -= q * exit_px + cm
                f_bar[nf] = j; f_qty[nf] = q; f_px[nf] = exit_px; f_reason[nf] = reason; f_comm[nf] = cm
                nf += 1
                lock_side = 1.0 if sh > 0 else -1.0
                sh = 0.0
                lock = True
        # ---- 3. liquidate on the final candle
        if j == n - 1 and sh != 0.0:
            q = -sh
            px = c[j] * (1.0 + slip) if q > 0 else c[j] * (1.0 - slip)
            cm = abs(q) * px * comm
            cash -= q * px + cm
            f_bar[nf] = j; f_qty[nf] = q; f_px[nf] = px; f_reason[nf] = R_END; f_comm[nf] = cm
            nf += 1
            sh = 0.0
        equity[j] = cash + sh * c[j]
        shares_end[j] = sh
    return equity, shares_end, f_bar[:nf], f_qty[:nf], f_px[:nf], f_reason[:nf], f_comm[:nf]


@dataclass
class SimResult:
    equity: pd.Series                # mark-to-market at each bar's close (index = bar open time)
    shares: pd.Series                # signed shares held at each bar's close
    exposure: pd.Series              # position value / equity at each bar's close
    fills: pd.DataFrame
    trades: pd.DataFrame
    config: SimConfig = field(default_factory=SimConfig)


def simulate(bars: pd.DataFrame, intents: pd.DataFrame, config: SimConfig | None = None,
             trade_from: pd.Timestamp | None = None) -> SimResult:
    """bars: base candles (open-time index, OHLC). intents: Trade output on the same rows (target/usd/stop/take)."""
    cfg = config or SimConfig()
    assert len(bars) == len(intents), "bars and intents must align row-for-row"
    idx = bars.index
    tf_idx = 0 if trade_from is None else int(idx.searchsorted(pd.Timestamp(trade_from)))
    eq, sh, fb, fq, fp, fr, fc = _simulate(
        bars["open"].to_numpy(float), bars["high"].to_numpy(float), bars["low"].to_numpy(float),
        bars["close"].to_numpy(float), intents["target"].to_numpy(float), intents["usd"].to_numpy(float),
        intents["stop"].to_numpy(float), intents["take"].to_numpy(float),
        intents["limit"].to_numpy(float) if "limit" in intents else np.full(len(bars), np.nan), tf_idx, cfg.capital,
        cfg.commission_bps / 1e4, cfg.slippage_bps / 1e4, cfg.allow_short)
    equity = pd.Series(eq, index=idx, name="equity")
    shares = pd.Series(sh, index=idx, name="shares")
    exposure = (shares * bars["close"] / equity).rename("exposure")
    fills = pd.DataFrame({"bar": fb, "time": idx[fb] if len(fb) else pd.DatetimeIndex([], tz="UTC"), "qty": fq,
                          "price": fp, "reason": [REASONS[int(r)] for r in fr], "commission": fc})
    return SimResult(equity, shares, exposure, fills, build_trades(bars, fills), cfg)


def build_trades(bars: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    """Round trips (flat -> flat) from fills, with net PnL, return, MAE/MFE."""
    cols = ["entry_time", "exit_time", "side", "qty", "entry_price", "exit_price", "pnl", "ret", "bars_held",
            "exit_reason", "mae", "mfe"]
    if fills.empty:
        return pd.DataFrame(columns=cols)
    rows, pos = [], 0.0
    cur = None
    for f in fills.itertuples():
        if cur is None:
            cur = dict(entry_bar=f.bar, side=1.0 if f.qty > 0 else -1.0, in_q=0.0, in_cost=0.0, out_q=0.0,
                       out_val=0.0, comm=0.0, peak=0.0)
        same_dir = (f.qty > 0) == (cur["side"] > 0)
        if same_dir:
            cur["in_q"] += abs(f.qty)
            cur["in_cost"] += abs(f.qty) * f.price
        else:
            cur["out_q"] += abs(f.qty)
            cur["out_val"] += abs(f.qty) * f.price
        cur["comm"] += f.commission
        cur["peak"] = max(cur["peak"], cur["in_q"])
        pos += f.qty
        if abs(pos) < 1e-9:
            ep = cur["in_cost"] / cur["in_q"]
            xp = cur["out_val"] / cur["out_q"]
            pnl = cur["side"] * (cur["out_val"] - cur["in_cost"]) - cur["comm"]
            seg = bars.iloc[cur["entry_bar"]: f.bar + 1]
            if cur["side"] > 0:
                mae, mfe = seg["low"].min() / ep - 1, seg["high"].max() / ep - 1
            else:
                mae, mfe = 1 - seg["high"].max() / ep, 1 - seg["low"].min() / ep
            rows.append(dict(entry_time=bars.index[cur["entry_bar"]], exit_time=bars.index[f.bar],
                             side="long" if cur["side"] > 0 else "short", qty=cur["peak"], entry_price=ep,
                             exit_price=xp, pnl=pnl, ret=pnl / cur["in_cost"], bars_held=f.bar - cur["entry_bar"],
                             exit_reason=f.reason, mae=mae, mfe=mfe))
            cur, pos = None, 0.0
    return pd.DataFrame(rows, columns=cols)
