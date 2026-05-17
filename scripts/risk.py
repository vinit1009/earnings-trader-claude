"""Hard risk caps. The agent cannot place orders that violate these.

The cloud version is stateless — risk checks query the broker (Alpaca) for
the current account snapshot every call. Source of truth for positions and
P&L lives at the broker, not in a local DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from broker import Broker
from ladder import LimitOrder, total_notional


MAX_PER_POSITION_USD: float = 500.0
MAX_CONCURRENT_POSITIONS: int = 3
MAX_DAILY_LOSS_USD: float = 200.0
MAX_PRICE_DEVIATION_PCT: float = 0.15


@dataclass(frozen=True)
class AccountSnapshot:
    """In-memory view derived from the broker for risk-checking."""
    open_positions: dict[str, float]      # symbol -> notional exposure
    pnl_today: float                       # realized + unrealized

    @property
    def open_count(self) -> int:
        return len(self.open_positions)

    def exposure(self, symbol: str) -> float:
        return self.open_positions.get(symbol, 0.0)

    @classmethod
    def from_broker(cls, broker: Broker) -> "AccountSnapshot":
        account = broker.get_account()
        positions = broker.list_positions()
        return cls(
            open_positions={
                p.symbol.upper(): abs(p.market_value) for p in positions
            },
            pnl_today=account.pnl_today(),
        )


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> "RiskDecision":
        return cls(allowed=True, reason="")

    @classmethod
    def deny(cls, reason: str) -> "RiskDecision":
        return cls(allowed=False, reason=reason)


def check_orders(
    orders: Sequence[LimitOrder],
    account: AccountSnapshot,
    current_price: float | None = None,
) -> RiskDecision:
    """Validate a set of ladder orders against all risk caps."""
    if not orders:
        return RiskDecision.deny("empty order list")

    symbols = {o.symbol for o in orders}
    if len(symbols) != 1:
        return RiskDecision.deny(f"orders span multiple symbols: {symbols}")
    symbol = next(iter(symbols))

    sides = {o.side for o in orders}
    if len(sides) != 1:
        return RiskDecision.deny(f"orders mix sides: {sides}")
    side = next(iter(sides))

    proposed_notional = total_notional(orders)
    if side == "buy":
        new_exposure = account.exposure(symbol) + proposed_notional
        if new_exposure > MAX_PER_POSITION_USD:
            return RiskDecision.deny(
                f"would push {symbol} exposure to ${new_exposure:.2f}, "
                f"cap ${MAX_PER_POSITION_USD:.2f}"
            )
        if symbol not in account.open_positions:
            if account.open_count + 1 > MAX_CONCURRENT_POSITIONS:
                return RiskDecision.deny(
                    f"would open {symbol} as position #{account.open_count + 1}, "
                    f"cap {MAX_CONCURRENT_POSITIONS}"
                )

    if account.pnl_today <= -MAX_DAILY_LOSS_USD and side == "buy":
        return RiskDecision.deny(
            f"P&L today ${account.pnl_today:.2f} at/below "
            f"-${MAX_DAILY_LOSS_USD:.2f}; new entries blocked (exits still allowed)"
        )

    if current_price is not None and current_price > 0:
        for o in orders:
            deviation = abs(o.price - current_price) / current_price
            if deviation > MAX_PRICE_DEVIATION_PCT:
                return RiskDecision.deny(
                    f"order at ${o.price:.2f} is {deviation*100:.1f}% from "
                    f"current ${current_price:.2f} (cap {MAX_PRICE_DEVIATION_PCT*100:.0f}%)"
                )

    return RiskDecision.ok()
