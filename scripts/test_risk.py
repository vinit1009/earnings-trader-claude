"""Unit tests for risk.py."""

import pytest

from ladder import LimitOrder, build_ladder
from risk import (
    MAX_CONCURRENT_POSITIONS,
    MAX_DAILY_LOSS_USD,
    MAX_PER_POSITION_USD,
    AccountSnapshot,
    check_orders,
)


def flat_account(**kwargs) -> AccountSnapshot:
    return AccountSnapshot(
        open_positions=kwargs.get("open_positions", {}),
        pnl_today=kwargs.get("pnl_today", kwargs.get("realized_pnl_today", 0.0)),
    )


def test_happy_path_under_caps():
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=20.0, total_shares=20, rungs=10
    )
    decision = check_orders(orders, flat_account(), current_price=20.0)
    assert decision.allowed, decision.reason


def test_blocks_when_position_exceeds_cap():
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=100.0, total_shares=100, rungs=5
    )
    decision = check_orders(orders, flat_account(), current_price=100.0)
    assert not decision.allowed
    assert "cap" in decision.reason.lower()


def test_blocks_when_adding_to_position_exceeds_cap():
    """Already $400 in AAPL, trying to add $200 — should block at $500 cap."""
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=20.0, total_shares=10, rungs=5
    )
    account = flat_account(open_positions={"AAPL": 400.0})
    decision = check_orders(orders, account, current_price=20.0)
    assert not decision.allowed


def test_blocks_when_concurrent_positions_exceed_cap():
    orders = build_ladder(
        symbol="NEW", side="buy", target_price=10.0, total_shares=10, rungs=5
    )
    account = flat_account(
        open_positions={"AAPL": 100.0, "TSLA": 100.0, "NVDA": 100.0}
    )
    assert account.open_count == MAX_CONCURRENT_POSITIONS
    decision = check_orders(orders, account, current_price=10.0)
    assert not decision.allowed
    assert "position" in decision.reason.lower()


def test_allows_adding_to_existing_position_even_at_max_concurrent():
    """If at max positions, can still add to one we already own."""
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=10.0, total_shares=5, rungs=5
    )
    account = flat_account(
        open_positions={"AAPL": 100.0, "TSLA": 100.0, "NVDA": 100.0}
    )
    decision = check_orders(orders, account, current_price=10.0)
    assert decision.allowed, decision.reason


def test_blocks_buys_after_daily_loss_cap():
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=10.0, total_shares=10, rungs=5
    )
    account = flat_account(pnl_today=-MAX_DAILY_LOSS_USD - 50)
    decision = check_orders(orders, account, current_price=10.0)
    assert not decision.allowed
    assert "p&l" in decision.reason.lower()


def test_allows_sells_after_daily_loss_cap():
    """Exit orders still allowed once daily loss hit (we want to flatten)."""
    orders = build_ladder(
        symbol="AAPL",
        side="sell",
        target_price=10.0,
        total_shares=10,
        rungs=5,
        down_band=0.05,
        up_band=0.10,
    )
    account = flat_account(
        open_positions={"AAPL": 200.0}, pnl_today=-300.0
    )
    decision = check_orders(orders, account, current_price=10.0)
    assert decision.allowed, decision.reason


def test_blocks_price_deviation():
    orders = [LimitOrder(symbol="X", side="buy", qty=1, price=100.0)]
    decision = check_orders(orders, flat_account(), current_price=50.0)
    assert not decision.allowed
    assert "deviation" in decision.reason.lower() or "%" in decision.reason


def test_empty_orders_denied():
    decision = check_orders([], flat_account(), current_price=10.0)
    assert not decision.allowed


def test_multi_symbol_denied():
    orders = [
        LimitOrder(symbol="AAPL", side="buy", qty=1, price=10.0),
        LimitOrder(symbol="TSLA", side="buy", qty=1, price=10.0),
    ]
    decision = check_orders(orders, flat_account(), current_price=10.0)
    assert not decision.allowed
    assert "multiple symbols" in decision.reason.lower()


def test_no_current_price_skips_deviation_check():
    """When current_price is None, deviation check is skipped (still other caps run)."""
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=20.0, total_shares=10, rungs=5
    )
    decision = check_orders(orders, flat_account(), current_price=None)
    assert decision.allowed, decision.reason


def test_caps_match_plan():
    """Conservative caps from the approved plan."""
    assert MAX_PER_POSITION_USD == 500.0
    assert MAX_CONCURRENT_POSITIONS == 3
    assert MAX_DAILY_LOSS_USD == 200.0
