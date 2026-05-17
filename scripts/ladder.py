"""Build laddered limit orders.

Given a target price, total share count, and price bands, produce N limit orders
distributed across [target*(1-down_band), target*(1+up_band)] with descending
linear weighting (more shares at favorable prices, fewer at unfavorable).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class LimitOrder:
    symbol: str
    side: Side
    qty: int
    price: float
    tif: str = "day"
    extended_hours: bool = False

    def notional(self) -> float:
        return self.qty * self.price


def build_ladder(
    symbol: str,
    side: Side,
    target_price: float,
    total_shares: int,
    *,
    down_band: float = 0.10,
    up_band: float = 0.05,
    rungs: int = 10,
    weight_ratio: float = 5.0,
    extended_hours: bool = False,
    tif: str = "day",
) -> list[LimitOrder]:
    """Build a ladder of limit orders.

    For `buy` side: heavier weight at lower prices (favors getting in cheap).
    For `sell` side: heavier weight at higher prices (favors selling rich).

    Args:
        symbol: ticker
        side: 'buy' or 'sell'
        target_price: anchor price
        total_shares: total quantity to distribute across rungs
        down_band: fraction below target for lowest rung (0.10 = 10% below)
        up_band: fraction above target for highest rung
        rungs: number of price points (>=1). Recommended 5-20.
        weight_ratio: heaviest:lightest share weight (e.g. 5.0 = 5x more at favorable end)
        extended_hours: flag for pre/post-market sessions
        tif: time-in-force ('day', 'gtc', 'ext')

    Returns:
        list of LimitOrder, sorted by price ascending.
    """
    if total_shares < 1:
        raise ValueError(f"total_shares must be >=1, got {total_shares}")
    if rungs < 1:
        raise ValueError(f"rungs must be >=1, got {rungs}")
    if target_price <= 0:
        raise ValueError(f"target_price must be positive, got {target_price}")
    if down_band < 0 or up_band < 0:
        raise ValueError("bands must be non-negative")
    if weight_ratio <= 0:
        raise ValueError(f"weight_ratio must be positive, got {weight_ratio}")
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side}")

    if rungs == 1:
        return [
            LimitOrder(
                symbol=symbol,
                side=side,
                qty=total_shares,
                price=round(target_price, 2),
                tif=tif,
                extended_hours=extended_hours,
            )
        ]

    lower = target_price * (1 - down_band)
    upper = target_price * (1 + up_band)
    step = (upper - lower) / (rungs - 1)
    prices = [round(lower + i * step, 2) for i in range(rungs)]

    # weights: descending linear for buy (heaviest at lowest price),
    # ascending linear for sell (heaviest at highest price).
    # weight[i] for buy: weight_ratio at i=0, 1.0 at i=rungs-1, linear.
    if side == "buy":
        weights = [
            weight_ratio - (weight_ratio - 1.0) * (i / (rungs - 1))
            for i in range(rungs)
        ]
    else:
        weights = [
            1.0 + (weight_ratio - 1.0) * (i / (rungs - 1)) for i in range(rungs)
        ]

    total_weight = sum(weights)
    raw_qtys = [total_shares * w / total_weight for w in weights]
    qtys = [int(round(q)) for q in raw_qtys]

    drift = total_shares - sum(qtys)
    if drift != 0:
        if side == "buy":
            qtys[0] += drift
        else:
            qtys[-1] += drift

    orders: list[LimitOrder] = []
    for price, qty in zip(prices, qtys):
        if qty < 1:
            continue
        orders.append(
            LimitOrder(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                tif=tif,
                extended_hours=extended_hours,
            )
        )

    placed = sum(o.qty for o in orders)
    if placed != total_shares and orders:
        residual = total_shares - placed
        idx = 0 if side == "buy" else -1
        orders[idx] = LimitOrder(
            symbol=symbol,
            side=side,
            qty=orders[idx].qty + residual,
            price=orders[idx].price,
            tif=tif,
            extended_hours=extended_hours,
        )

    return orders


def average_fill_price(orders: list[LimitOrder]) -> float:
    """Weighted average price across orders (assumes all fill)."""
    total_qty = sum(o.qty for o in orders)
    if total_qty == 0:
        return 0.0
    return sum(o.qty * o.price for o in orders) / total_qty


def total_notional(orders: list[LimitOrder]) -> float:
    return sum(o.notional() for o in orders)
