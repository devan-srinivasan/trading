"""Profit and loss from a list of fills (works for the local engine and for Alpaca fills alike)."""
from __future__ import annotations


def position_pnl(fills: list[dict], last_price: float | None) -> dict:
    """fills: [{"side": "buy"|"sell", "qty", "price", "fee"?}] in time order. Average-cost accounting."""
    qty = avg = realized = fees = 0.0
    trades = wins = 0
    for f in fills:
        q = f["qty"] if f["side"] == "buy" else -f["qty"]
        fees += f.get("fee", 0.0) or 0.0
        if qty == 0 or (qty > 0) == (q > 0):                       # opening / adding
            avg = (avg * abs(qty) + f["price"] * abs(q)) / (abs(qty) + abs(q))
            qty += q
        else:                                                      # reducing / closing (maybe flipping)
            close = min(abs(q), abs(qty))
            pnl = close * (f["price"] - avg) * (1 if qty > 0 else -1)
            realized += pnl
            trades += 1
            wins += pnl > 0
            qty += q
            if abs(qty) < 1e-9:
                qty, avg = 0.0, 0.0
            elif (qty > 0) == (q > 0):                             # flipped: the remainder opened at this price
                avg = f["price"]
    unreal = qty * (last_price - avg) if qty and last_price else 0.0
    return {"position": qty, "avg_price": avg or None, "realized": round(realized - fees, 2),
            "unrealized": round(unreal, 2), "fees": round(fees, 2), "closed_trades": trades, "wins": wins}
