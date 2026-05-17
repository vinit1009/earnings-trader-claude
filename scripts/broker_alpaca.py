"""Alpaca implementation of the Broker interface.

Uses alpaca-py. Reads ALPACA_KEY_ID, ALPACA_SECRET, ALPACA_BASE_URL from env.
"""

from __future__ import annotations

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus as AlpacaStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, StopLimitOrderRequest
from alpaca.common.exceptions import APIError

from broker import AccountInfo, Broker, BrokerError, OrderStatus, Position
from ladder import LimitOrder


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(
        self,
        *,
        key_id: str | None = None,
        secret: str | None = None,
        base_url: str | None = None,
    ):
        key_id = key_id or os.environ.get("ALPACA_KEY_ID")
        secret = secret or os.environ.get("ALPACA_SECRET")
        base_url = base_url or os.environ.get(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        )
        if not key_id or not secret:
            raise BrokerError("ALPACA_KEY_ID / ALPACA_SECRET not set")

        paper = "paper" in base_url
        self._client = TradingClient(api_key=key_id, secret_key=secret, paper=paper)
        self._paper = paper

    def get_account(self) -> AccountInfo:
        try:
            a = self._client.get_account()
        except APIError as e:
            raise BrokerError(f"alpaca get_account failed: {e}") from e
        return AccountInfo(
            cash=float(a.cash),
            equity=float(a.equity),
            buying_power=float(a.buying_power),
            last_equity=float(a.last_equity or 0),
        )

    def list_positions(self) -> list[Position]:
        try:
            raw = self._client.get_all_positions()
        except APIError as e:
            raise BrokerError(f"alpaca list_positions failed: {e}") from e
        return [
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
            )
            for p in raw
        ]

    def get_position(self, symbol: str) -> Position | None:
        try:
            p = self._client.get_open_position(symbol.upper())
        except APIError as e:
            msg = str(e).lower()
            if "position does not exist" in msg or "404" in msg:
                return None
            raise BrokerError(f"alpaca get_position({symbol}) failed: {e}") from e
        return Position(
            symbol=p.symbol,
            qty=float(p.qty),
            avg_entry_price=float(p.avg_entry_price),
            market_value=float(p.market_value),
            unrealized_pl=float(p.unrealized_pl),
        )

    def list_open_orders(self) -> list[OrderStatus]:
        try:
            req = GetOrdersRequest(status="open")
            raw = self._client.get_orders(filter=req)
        except APIError as e:
            raise BrokerError(f"alpaca list_open_orders failed: {e}") from e
        return [self._to_status(o) for o in raw]

    def place_limit(self, order: LimitOrder) -> OrderStatus:
        side_enum = OrderSide.BUY if order.side == "buy" else OrderSide.SELL

        tif_map = {
            "day": TimeInForce.DAY,
            "gtc": TimeInForce.GTC,
            "ioc": TimeInForce.IOC,
            "fok": TimeInForce.FOK,
            "opg": TimeInForce.OPG,
            "cls": TimeInForce.CLS,
        }
        tif_enum = tif_map.get(order.tif.lower(), TimeInForce.DAY)

        req = LimitOrderRequest(
            symbol=order.symbol,
            qty=order.qty,
            side=side_enum,
            time_in_force=tif_enum,
            limit_price=order.price,
            extended_hours=order.extended_hours,
        )
        try:
            placed = self._client.submit_order(order_data=req)
        except APIError as e:
            raise BrokerError(
                f"alpaca submit_order {order.side} {order.qty} {order.symbol} @ "
                f"${order.price:.2f}: {e}"
            ) from e
        return self._to_status(placed)

    def place_stop_limit(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        *,
        tif: str = "gtc",
        extended_hours: bool = False,
    ) -> OrderStatus:
        if limit_price is None:
            limit_price = round(stop_price * 0.99, 2)
        tif_map = {
            "day": TimeInForce.DAY,
            "gtc": TimeInForce.GTC,
        }
        tif_enum = tif_map.get(tif.lower(), TimeInForce.GTC)
        req = StopLimitOrderRequest(
            symbol=symbol.upper(),
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=tif_enum,
            stop_price=stop_price,
            limit_price=limit_price,
            extended_hours=extended_hours,
        )
        try:
            placed = self._client.submit_order(order_data=req)
        except APIError as e:
            raise BrokerError(
                f"alpaca place_stop_limit {qty} {symbol} stop=${stop_price:.2f}: {e}"
            ) from e
        return self._to_status(placed)

    def cancel(self, order_id: str) -> None:
        try:
            self._client.cancel_order_by_id(order_id)
        except APIError as e:
            raise BrokerError(f"alpaca cancel({order_id}) failed: {e}") from e

    @staticmethod
    def _to_status(o) -> OrderStatus:
        status = o.status
        status_str = status.value if hasattr(status, "value") else str(status)
        return OrderStatus(
            order_id=str(o.id),
            symbol=o.symbol,
            side=o.side.value if hasattr(o.side, "value") else str(o.side),
            qty=float(o.qty),
            filled_qty=float(o.filled_qty or 0),
            price=float(o.limit_price) if o.limit_price else 0.0,
            status=status_str,
            extended_hours=bool(o.extended_hours),
        )
