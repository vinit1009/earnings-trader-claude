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
MAX_PER_SECTOR_POSITIONS: int = 1

# Trading-mode tripwire thresholds (today's P&L in USD).
MODE_CAUTIOUS_THRESHOLD: float = -100.0
MODE_DEFENSIVE_THRESHOLD: float = -150.0
MODE_EXIT_ONLY_THRESHOLD: float = -180.0
MODE_HALT_THRESHOLD: float = -200.0


def get_trading_mode(pnl_today: float) -> dict:
    """Classify today's posture based on running P&L.

    Modes (descending P&L):
        - normal: standard playbook
        - cautious: only tier S/A new opens; no opens in last hour
        - defensive: no new opens; flatten positions down >5%
        - exit-only: flatten all losers; no new orders except sells
        - halt: cancel everything, no orders allowed
    """
    if pnl_today <= MODE_HALT_THRESHOLD:
        mode = "halt"
        rule = "cancel every open order; no orders of any kind allowed"
    elif pnl_today <= MODE_EXIT_ONLY_THRESHOLD:
        mode = "exit_only"
        rule = "flatten all positions with negative P&L; no new orders except sells"
    elif pnl_today <= MODE_DEFENSIVE_THRESHOLD:
        mode = "defensive"
        rule = "no new opens; flatten any position down >5% immediately"
    elif pnl_today <= MODE_CAUTIOUS_THRESHOLD:
        mode = "cautious"
        rule = "only tier S/A new opens; no new opens in the last hour of session"
    else:
        mode = "normal"
        rule = "standard playbook"
    return {"mode": mode, "rule": rule, "pnl_today": round(pnl_today, 2)}


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
    *,
    industries: dict[str, str] | None = None,
    allow_sector_double: bool = False,
) -> RiskDecision:
    """Validate a set of ladder orders against all risk caps.

    `industries` is an optional symbol->finnhubIndustry map covering at least the new
    symbol and every currently-open position. When provided, sector-correlation cap is
    enforced. `allow_sector_double` bypasses that check (manual override only).
    """
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

        if industries and not allow_sector_double:
            new_industry = (industries.get(symbol) or "").strip().lower()
            if new_industry and symbol not in account.open_positions:
                for other_sym in account.open_positions:
                    other_industry = (industries.get(other_sym) or "").strip().lower()
                    if other_industry and other_industry == new_industry:
                        return RiskDecision.deny(
                            f"sector cap: {symbol} (industry={new_industry!r}) would be "
                            f"position #{1 + sum(1 for s in account.open_positions if (industries.get(s) or '').strip().lower() == new_industry)} "
                            f"in this industry; existing holder {other_sym}. "
                            f"Override with --allow-sector-double."
                        )

    mode_info = get_trading_mode(account.pnl_today)
    mode = mode_info["mode"]
    if side == "buy" and mode in ("defensive", "exit_only", "halt"):
        return RiskDecision.deny(
            f"trading mode={mode!r} (P&L today ${account.pnl_today:.2f}); "
            f"new buys blocked. Rule: {mode_info['rule']}"
        )
    # Sells (exits) are always allowed in any mode — you must be able to flatten
    # positions regardless of daily loss. Blocking exits would compound losses.

    if current_price is not None and current_price > 0:
        for o in orders:
            deviation = abs(o.price - current_price) / current_price
            if deviation > MAX_PRICE_DEVIATION_PCT:
                return RiskDecision.deny(
                    f"order at ${o.price:.2f} is {deviation*100:.1f}% from "
                    f"current ${current_price:.2f} (cap {MAX_PRICE_DEVIATION_PCT*100:.0f}%)"
                )

    return RiskDecision.ok()
