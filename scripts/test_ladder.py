"""Unit tests for ladder.py."""

import pytest

from ladder import (
    LimitOrder,
    average_fill_price,
    build_ladder,
    total_notional,
)


def test_default_aapl_25sh_at_20():
    """Plan example: 25sh AAPL @ $20, 10 rungs, 10%/5% bands, 5:1 weight."""
    orders = build_ladder(
        symbol="AAPL",
        side="buy",
        target_price=20.0,
        total_shares=25,
        down_band=0.10,
        up_band=0.05,
        rungs=10,
        weight_ratio=5.0,
    )
    assert sum(o.qty for o in orders) == 25
    assert all(o.symbol == "AAPL" for o in orders)
    assert all(o.side == "buy" for o in orders)
    assert min(o.price for o in orders) == 18.0
    assert max(o.price for o in orders) == 21.0
    # avg fill price should be below target since buy ladder is weighted low
    assert average_fill_price(orders) < 20.0
    # ordered by price ascending
    prices = [o.price for o in orders]
    assert prices == sorted(prices)


def test_buy_ladder_weights_favor_low_prices():
    """Lowest-price rung should have at least 2x the shares of the highest."""
    orders = build_ladder(
        symbol="TSLA",
        side="buy",
        target_price=100.0,
        total_shares=20,
        rungs=5,
        weight_ratio=5.0,
    )
    assert orders[0].qty >= 2 * orders[-1].qty


def test_sell_ladder_weights_favor_high_prices():
    """Highest-price rung should have more shares than lowest on sell side."""
    orders = build_ladder(
        symbol="NVDA",
        side="sell",
        target_price=500.0,
        total_shares=20,
        rungs=5,
        weight_ratio=5.0,
    )
    assert orders[-1].qty >= 2 * orders[0].qty
    assert all(o.side == "sell" for o in orders)


def test_rungs_one_returns_single_order():
    orders = build_ladder(
        symbol="MSFT",
        side="buy",
        target_price=300.0,
        total_shares=10,
        rungs=1,
    )
    assert len(orders) == 1
    assert orders[0].qty == 10
    assert orders[0].price == 300.0


def test_total_shares_always_preserved():
    """Across many parameter combos, sum(qty) must equal total_shares."""
    cases = [
        dict(target_price=20.0, total_shares=25, rungs=10),
        dict(target_price=100.0, total_shares=7, rungs=5),
        dict(target_price=50.0, total_shares=100, rungs=20),
        dict(target_price=1.50, total_shares=1000, rungs=10),
        dict(target_price=180.0, total_shares=1, rungs=10),
        dict(target_price=180.0, total_shares=3, rungs=10),
    ]
    for case in cases:
        orders = build_ladder(symbol="X", side="buy", **case)
        assert sum(o.qty for o in orders) == case["total_shares"], case


def test_total_shares_preserved_sell_side():
    cases = [
        dict(target_price=20.0, total_shares=25, rungs=10),
        dict(target_price=100.0, total_shares=7, rungs=5),
    ]
    for case in cases:
        orders = build_ladder(symbol="X", side="sell", **case)
        assert sum(o.qty for o in orders) == case["total_shares"], case


def test_extended_hours_flag_propagates():
    orders = build_ladder(
        symbol="AMZN",
        side="buy",
        target_price=150.0,
        total_shares=10,
        rungs=5,
        extended_hours=True,
        tif="ext",
    )
    assert all(o.extended_hours for o in orders)
    assert all(o.tif == "ext" for o in orders)


def test_zero_band_collapses_to_target():
    orders = build_ladder(
        symbol="X",
        side="buy",
        target_price=50.0,
        total_shares=10,
        down_band=0.0,
        up_band=0.0,
        rungs=5,
    )
    # All rungs at $50 (within rounding)
    assert all(o.price == 50.0 for o in orders)


def test_average_fill_price_zero_for_empty_list():
    assert average_fill_price([]) == 0.0


def test_total_notional_matches_manual():
    orders = [
        LimitOrder(symbol="X", side="buy", qty=10, price=18.0),
        LimitOrder(symbol="X", side="buy", qty=5, price=20.0),
    ]
    assert total_notional(orders) == pytest.approx(280.0)


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        build_ladder(symbol="X", side="buy", target_price=20, total_shares=0)
    with pytest.raises(ValueError):
        build_ladder(symbol="X", side="buy", target_price=20, total_shares=10, rungs=0)
    with pytest.raises(ValueError):
        build_ladder(symbol="X", side="buy", target_price=-5, total_shares=10)
    with pytest.raises(ValueError):
        build_ladder(symbol="X", side="hold", target_price=20, total_shares=10)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_ladder(symbol="X", side="buy", target_price=20, total_shares=10, weight_ratio=0)


def test_avg_buy_fill_below_target_with_default_weights():
    """Across a range, buy ladder average must be <= target (weighted low)."""
    for target in (5.0, 20.0, 100.0, 500.0):
        orders = build_ladder(
            symbol="X",
            side="buy",
            target_price=target,
            total_shares=50,
            rungs=10,
            weight_ratio=5.0,
        )
        assert average_fill_price(orders) <= target, target


def test_avg_sell_fill_above_target_with_appropriate_bands():
    """Sell ladders should pass bands favoring upside (mirror of buy defaults)."""
    for target in (5.0, 20.0, 100.0, 500.0):
        orders = build_ladder(
            symbol="X",
            side="sell",
            target_price=target,
            total_shares=50,
            rungs=10,
            weight_ratio=5.0,
            down_band=0.05,
            up_band=0.10,
        )
        assert average_fill_price(orders) >= target, target


def test_sell_avg_above_band_midpoint_with_default_bands():
    """Even with buy-style defaults, sell weighting pulls avg above band midpoint."""
    target = 100.0
    orders = build_ladder(
        symbol="X",
        side="sell",
        target_price=target,
        total_shares=50,
        rungs=10,
        weight_ratio=5.0,
    )
    midpoint = (min(o.price for o in orders) + max(o.price for o in orders)) / 2
    assert average_fill_price(orders) > midpoint
