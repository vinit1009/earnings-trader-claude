"""Broker interface. Concrete implementations: broker_alpaca, broker_robinhood.

Strategies depend on Broker (abstract) only — swap implementations via env
var BROKER=alpaca|robinhood without touching strategy code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable

from ladder import LimitOrder


@dataclass(frozen=True)
class AccountInfo:
    cash: float
    equity: float
    buying_power: float
    last_equity: float = 0.0  # equity at previous trading day's close

    def pnl_today(self) -> float:
        """Today's total P&L (realized + unrealized)."""
        if not self.last_equity:
            return 0.0
        return self.equity - self.last_equity


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class OrderStatus:
    order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    price: float
    status: str
    extended_hours: bool


class BrokerError(Exception):
    pass


class Broker(ABC):
    name: str

    @abstractmethod
    def get_account(self) -> AccountInfo: ...

    @abstractmethod
    def get_position(self, symbol: str) -> Position | None: ...

    @abstractmethod
    def list_positions(self) -> list[Position]: ...

    @abstractmethod
    def list_open_orders(self) -> list[OrderStatus]: ...

    @abstractmethod
    def place_limit(self, order: LimitOrder) -> OrderStatus: ...

    @abstractmethod
    def cancel(self, order_id: str) -> None: ...

    def place_ladder(self, orders: Iterable[LimitOrder]) -> list[OrderStatus]:
        """Place every order in a ladder. Stops on first error.

        Caller is responsible for risk-checking the full ladder BEFORE
        invoking this method (see risk.check_orders).
        """
        placed: list[OrderStatus] = []
        for o in orders:
            try:
                placed.append(self.place_limit(o))
            except BrokerError as exc:
                for status in placed:
                    try:
                        self.cancel(status.order_id)
                    except BrokerError:
                        pass
                raise BrokerError(
                    f"ladder failed at rung price=${o.price:.2f} qty={o.qty}: {exc}"
                ) from exc
        return placed


def load_broker(name: str | None = None) -> Broker:
    """Load broker. Currently Alpaca-only; Robinhood deferred."""
    import os

    name = name or os.environ.get("BROKER", "alpaca").lower()
    if name == "alpaca":
        from broker_alpaca import AlpacaBroker

        return AlpacaBroker()
    raise ValueError(f"unknown broker: {name!r} (only 'alpaca' supported)")
