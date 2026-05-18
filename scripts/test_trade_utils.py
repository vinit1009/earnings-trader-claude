"""Unit tests for trade.py helpers that don't require live APIs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import datetime as _dt

from trade import _trading_days_between


def test_trading_days_same_day():
    d = _dt.date(2026, 5, 11)  # Monday
    assert _trading_days_between(d, d) == 0


def test_trading_days_one_week():
    # Mon → next Mon = 5 trading days
    start = _dt.date(2026, 5, 11)  # Monday
    end = _dt.date(2026, 5, 18)   # Next Monday
    assert _trading_days_between(start, end) == 5


def test_trading_days_skips_weekend():
    # Friday → Monday = 1 trading day (Sat/Sun skipped)
    start = _dt.date(2026, 5, 15)  # Friday
    end = _dt.date(2026, 5, 18)   # Monday
    assert _trading_days_between(start, end) == 1


def test_trading_days_over_weekend():
    # Thursday → next Tuesday = 3 trading days (Fri + Mon + Tue)
    start = _dt.date(2026, 5, 14)  # Thursday
    end = _dt.date(2026, 5, 19)   # Tuesday
    assert _trading_days_between(start, end) == 3


def test_force_flatten_stale_no_positions_flag():
    """If no --position args, emits the 'no dates supplied' message."""
    import argparse
    import io
    import json

    # Patch broker to return empty positions
    import unittest.mock as mock
    with mock.patch("trade.load_dotenv"), \
         mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        # Build args manually
        args = argparse.Namespace(position=None, max_age_days=5, dry_run=False)
        # Patch broker inside trade module
        mock_broker = mock.MagicMock()
        mock_broker.list_positions.return_value = []
        with mock.patch("broker.load_broker", return_value=mock_broker):
            from trade import cmd_force_flatten_stale
            result = cmd_force_flatten_stale(args)
        out = json.loads(mock_out.getvalue())
    assert result == 0
    assert out["stale_positions"] == []


def test_stale_detection():
    """Positions older than max_age_days are correctly flagged."""
    import argparse
    import io
    import json
    import unittest.mock as mock

    today = _dt.date.today()
    # Position opened 8 trading days ago (definitely stale at threshold=5)
    opened = today - _dt.timedelta(days=12)  # 12 calendar days ≈ 8 trading days

    mock_pos = mock.MagicMock()
    mock_pos.symbol = "AAPL"
    mock_pos.qty = 10.0
    mock_pos.avg_entry_price = 150.0
    mock_pos.market_value = 1600.0
    mock_pos.unrealized_pl = 100.0

    mock_broker = mock.MagicMock()
    mock_broker.list_positions.return_value = [mock_pos]

    args = argparse.Namespace(
        position=[f"AAPL:{opened.isoformat()}"],
        max_age_days=5,
        dry_run=False,
    )

    with mock.patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        with mock.patch("broker.load_broker", return_value=mock_broker):
            from trade import cmd_force_flatten_stale
            result = cmd_force_flatten_stale(args)
        out = json.loads(mock_out.getvalue())

    assert result == 0
    assert len(out["stale_positions"]) == 1
    assert out["stale_positions"][0]["symbol"] == "AAPL"
    assert out["stale_positions"][0]["age_trading_days"] > 5
