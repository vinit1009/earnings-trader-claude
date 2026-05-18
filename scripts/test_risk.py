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


def test_sector_cap_blocks_second_position_in_same_industry():
    """Two tickers in the same Finnhub industry should be blocked."""
    orders = build_ladder(
        symbol="AMD", side="buy", target_price=10.0, total_shares=10, rungs=5
    )
    account = flat_account(open_positions={"NVDA": 200.0})
    industries = {"NVDA": "Semiconductors", "AMD": "Semiconductors"}
    decision = check_orders(orders, account, current_price=10.0, industries=industries)
    assert not decision.allowed
    assert "sector" in decision.reason.lower()


def test_sector_cap_allow_different_industries():
    """Different industries are not blocked by the sector cap."""
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=10.0, total_shares=10, rungs=5
    )
    account = flat_account(open_positions={"NVDA": 200.0})
    industries = {"NVDA": "Semiconductors", "AAPL": "Technology"}
    decision = check_orders(orders, account, current_price=10.0, industries=industries)
    assert decision.allowed, decision.reason


def test_sector_cap_override_flag():
    """--allow-sector-double bypasses the sector cap."""
    orders = build_ladder(
        symbol="AMD", side="buy", target_price=10.0, total_shares=10, rungs=5
    )
    account = flat_account(open_positions={"NVDA": 200.0})
    industries = {"NVDA": "Semiconductors", "AMD": "Semiconductors"}
    decision = check_orders(
        orders, account, current_price=10.0, industries=industries, allow_sector_double=True
    )
    assert decision.allowed, decision.reason


def test_trading_modes_progression():
    """get_trading_mode returns the right mode at each P&L threshold."""
    from risk import get_trading_mode
    assert get_trading_mode(0.0)["mode"] == "normal"
    assert get_trading_mode(-50.0)["mode"] == "normal"
    assert get_trading_mode(-100.0)["mode"] == "cautious"
    assert get_trading_mode(-130.0)["mode"] == "cautious"
    assert get_trading_mode(-150.0)["mode"] == "defensive"
    assert get_trading_mode(-165.0)["mode"] == "defensive"
    assert get_trading_mode(-180.0)["mode"] == "exit_only"
    assert get_trading_mode(-195.0)["mode"] == "exit_only"
    assert get_trading_mode(-200.0)["mode"] == "halt"
    assert get_trading_mode(-999.0)["mode"] == "halt"


def test_halt_mode_still_blocks_buys():
    """In halt mode, buy orders must be rejected."""
    orders = build_ladder(
        symbol="AAPL", side="buy", target_price=10.0, total_shares=5, rungs=3
    )
    account = flat_account(pnl_today=-250.0)
    decision = check_orders(orders, account, current_price=10.0)
    assert not decision.allowed
    assert "halt" in decision.reason.lower()


def test_defensive_mode_blocks_buys_but_not_sells():
    """In defensive mode, sells (exits) are still allowed."""
    orders_sell = build_ladder(
        symbol="AAPL", side="sell", target_price=10.0, total_shares=5, rungs=3,
        down_band=0.05, up_band=0.10,
    )
    account = flat_account(open_positions={"AAPL": 200.0}, pnl_today=-155.0)
    decision = check_orders(orders_sell, account, current_price=10.0)
    assert decision.allowed, decision.reason
