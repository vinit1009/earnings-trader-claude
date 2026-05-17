"""Earnings-trader CLI.

Primitives that Claude (running via /schedule) calls to gather context, propose
trades, and manage orders. Claude is the brain; this is the body.

Subcommands:
    fetch-amc-context [--symbol X]   Emit JSON: today's AMC reporters + their
                                      print numbers + quotes + headlines + prior
                                      quarters. With --symbol, fetch the most
                                      recent historical print for that ticker
                                      (test mode).
    account-snapshot                  Emit JSON: cash, equity, buying power,
                                      open positions, today's realized P&L,
                                      risk headroom.
    list-orders                       Emit JSON: currently-open orders on the
                                      broker.
    propose --symbol X --target P     Build a laddered limit order, risk-check,
            --shares N                post to Discord with --rationale, await
            --rationale "..."         ✅, place on approval. JSON result.
            [--down-band 0.10]
            [--up-band 0.05]
            [--rungs 10]
            [--side buy|sell]
            [--extended-hours]
            [--tif day|gtc]
            [--timeout-s 90]
    cancel --order-id ID              Cancel one order.
    cancel-all                        Cancel all open orders.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    # Load .env files in priority order. In cloud (no files present), os.environ
    # already has what we need from the routine prompt.
    candidates = [
        REPO_ROOT / ".env.local",
        REPO_ROOT / ".env",
        REPO_ROOT.parent / ".env.local",  # workspace fallback (local dev)
    ]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _emit(data) -> None:
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Subcommand: fetch-amc-context
# ---------------------------------------------------------------------------


def cmd_fetch_amc_context(args) -> int:
    from earnings import (
        get_company_metrics,
        get_company_news,
        get_quote,
        get_recent_earnings,
        get_upcoming_earnings,
    )
    import yaml

    watchlist_path = REPO_ROOT / "watchlist.yaml"
    cfg = {}
    if watchlist_path.exists():
        with open(watchlist_path) as f:
            cfg = yaml.safe_load(f) or {}
    filters = cfg.get("filters") or {}

    min_market_cap = float(filters.get("min_market_cap_usd", 2_000_000_000))
    min_avg_volume = float(filters.get("min_avg_volume", 1_000_000))
    min_price = float(filters.get("min_price", 5.0))
    allowed_countries = set(filters.get("allowed_countries") or [])
    allowed_exchanges = set(
        (x or "").upper() for x in (filters.get("allowed_exchanges") or [])
    )
    common_stock_only = bool(filters.get("common_stock_only", True))

    if args.symbol:
        # Test mode: skip universe filtering, use historical prints.
        events = []
        for sym in args.symbol:
            hist = get_recent_earnings(sym, quarters=1)
            if hist and hist[0].eps_actual is not None:
                events.append(hist[0])
        rejected = []
    else:
        # Prod mode: pull the full US AMC universe for the target date, then filter.
        import datetime as _dt
        target_date = (
            _dt.date.fromisoformat(args.for_date) if args.for_date else _dt.date.today()
        )
        all_events = get_upcoming_earnings(
            days=1, watchlist=None, from_date=target_date
        )
        amc_events = [
            e
            for e in all_events
            if e.is_amc
            and e.eps_actual is not None
            and e.date == target_date.isoformat()
        ]
        logging.info(
            "fetch-amc-context: %d AMC reporters today before filtering",
            len(amc_events),
        )

        events = []
        rejected = []
        for e in amc_events:
            m = get_company_metrics(e.symbol)
            if m is None:
                rejected.append({"symbol": e.symbol, "reason": "no metrics data"})
                continue
            ok, reason = m.passes_filter(
                min_market_cap_usd=min_market_cap,
                min_avg_volume=min_avg_volume,
                min_price=min_price,
                allowed_countries=allowed_countries or None,
                allowed_exchanges=allowed_exchanges or None,
                common_stock_only=common_stock_only,
            )
            if not ok:
                rejected.append({"symbol": e.symbol, "reason": reason})
                continue
            events.append((e, m))
            logging.info(
                "  ✓ %s  cap=$%.1fB  vol=%.1fM  px=$%.2f",
                e.symbol,
                (m.market_cap_usd or 0) / 1e9,
                (m.avg_volume_10d or 0) / 1e6,
                m.current_price or 0,
            )

    if not events:
        _emit(
            {
                "reporters": [],
                "filtered_out": rejected,
                "note": "no AMC reporters passed filters today",
            }
        )
        return 0

    # Normalize: events may be List[EarningsEvent] (test mode) or List[(event, metrics)] (prod)
    out = []
    for item in events:
        if isinstance(item, tuple):
            e, m = item
        else:
            e, m = item, None

        q = get_quote(e.symbol) if m is None else None  # in prod, we already fetched a quote inside metrics
        if q is None and m is not None and m.current_price is not None:
            # Re-fetch fresh quote (the one inside get_company_metrics may be stale by a few sec)
            q = get_quote(e.symbol)

        news = get_company_news(e.symbol, hours=1) or []
        history = get_recent_earnings(e.symbol, quarters=4)

        out.append(
            {
                "symbol": e.symbol,
                "date": e.date,
                "hour": e.hour,
                "metrics": (
                    None
                    if m is None
                    else {
                        "market_cap_usd": m.market_cap_usd,
                        "avg_volume_10d": m.avg_volume_10d,
                        "exchange": m.exchange,
                        "country": m.country,
                        "industry": m.finnhub_industry,
                    }
                ),
                "print": {
                    "eps_estimate": e.eps_estimate,
                    "eps_actual": e.eps_actual,
                    "eps_surprise_pct": e.eps_surprise_pct(),
                    "revenue_estimate": e.revenue_estimate,
                    "revenue_actual": e.revenue_actual,
                },
                "quote": (
                    None
                    if q is None
                    else {
                        "current": q.current,
                        "previous_close": q.previous_close,
                        "pct_change": q.pct_change(),
                        "high": q.high,
                        "low": q.low,
                    }
                ),
                "headlines": [
                    {
                        "headline": n.headline,
                        "source": n.source,
                        "published_at": n.published_at.isoformat(),
                        "summary": n.summary[:240] if n.summary else "",
                    }
                    for n in news[:20]
                ],
                "prior_quarters": [
                    {
                        "date": h.date,
                        "eps_estimate": h.eps_estimate,
                        "eps_actual": h.eps_actual,
                        "eps_surprise_pct": h.eps_surprise_pct(),
                    }
                    for h in history
                ],
            }
        )

    _emit(
        {
            "reporters": out,
            "count": len(out),
            "filtered_out_count": len(rejected),
            "filters_applied": {
                "min_market_cap_usd": min_market_cap,
                "min_avg_volume": min_avg_volume,
                "min_price": min_price,
                "common_stock_only": common_stock_only,
            },
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: account-snapshot
# ---------------------------------------------------------------------------


def cmd_account_snapshot(args) -> int:
    from broker import load_broker
    from risk import (
        AccountSnapshot,
        MAX_CONCURRENT_POSITIONS,
        MAX_DAILY_LOSS_USD,
        MAX_PER_POSITION_USD,
    )

    broker = load_broker()
    acct = broker.get_account()
    snap = AccountSnapshot.from_broker(broker)
    positions = broker.list_positions()

    _emit(
        {
            "broker": broker.name,
            "account": {
                "cash": acct.cash,
                "equity": acct.equity,
                "buying_power": acct.buying_power,
                "last_equity": acct.last_equity,
                "pnl_today": acct.pnl_today(),
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_entry_price": p.avg_entry_price,
                    "market_value": p.market_value,
                    "unrealized_pl": p.unrealized_pl,
                }
                for p in positions
            ],
            "position_count": len(positions),
            "risk_caps": {
                "max_per_position_usd": MAX_PER_POSITION_USD,
                "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
                "max_daily_loss_usd": MAX_DAILY_LOSS_USD,
            },
            "headroom": {
                "remaining_position_slots": max(
                    0, MAX_CONCURRENT_POSITIONS - snap.open_count
                ),
                "remaining_daily_loss_budget": MAX_DAILY_LOSS_USD
                + min(0.0, snap.pnl_today),
            },
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: list-orders
# ---------------------------------------------------------------------------


def cmd_list_orders(args) -> int:
    from broker import load_broker

    broker = load_broker()
    orders = broker.list_open_orders()
    _emit(
        {
            "open_orders": [
                {
                    "order_id": o.order_id,
                    "symbol": o.symbol,
                    "side": o.side,
                    "qty": o.qty,
                    "filled_qty": o.filled_qty,
                    "price": o.price,
                    "status": o.status,
                    "extended_hours": o.extended_hours,
                }
                for o in orders
            ],
            "count": len(orders),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: propose
# ---------------------------------------------------------------------------


async def _propose_async(args) -> int:
    from broker import BrokerError, load_broker
    from discord_bot import ApprovalBot
    from earnings import get_quote
    from ladder import average_fill_price, build_ladder
    from risk import AccountSnapshot, check_orders

    broker = load_broker()

    orders = build_ladder(
        symbol=args.symbol,
        side=args.side,
        target_price=args.target,
        total_shares=args.shares,
        rungs=args.rungs,
        down_band=args.down_band,
        up_band=args.up_band,
        weight_ratio=args.weight_ratio,
        extended_hours=args.extended_hours,
        tif=args.tif,
    )

    quote = get_quote(args.symbol)
    current_price = quote.current if quote else None
    snapshot = AccountSnapshot.from_broker(broker)
    risk = check_orders(orders, snapshot, current_price=current_price)
    if not risk.allowed:
        _emit(
            {
                "approved": False,
                "placed": False,
                "reason": f"risk: {risk.reason}",
                "ladder_preview": [
                    {"price": o.price, "qty": o.qty} for o in orders
                ],
            }
        )
        return 1

    ladder_id = f"lad-{uuid.uuid4().hex[:8]}"

    if args.dry_run:
        avg = average_fill_price(orders)
        _emit(
            {
                "dry_run": True,
                "ladder_id": ladder_id,
                "avg_fill_if_all_filled": round(avg, 2),
                "total_notional": round(sum(o.price * o.qty for o in orders), 2),
                "rungs": [{"price": o.price, "qty": o.qty} for o in orders],
            }
        )
        return 0

    async with ApprovalBot() as bot:
        decision = await bot.request_approval(
            symbol=args.symbol,
            side=args.side,
            orders=orders,
            context=args.rationale,
            phase=args.phase or "manual",
            timeout_s=args.timeout_s,
        )

        if not decision.approved:
            _emit(
                {
                    "approved": False,
                    "placed": False,
                    "reason": decision.reason,
                    "ladder_id": ladder_id,
                }
            )
            return 0

        try:
            placed = broker.place_ladder(orders)
        except BrokerError as e:
            await bot.post_error(args.phase or "manual", f"{args.symbol}: {e}")
            _emit(
                {
                    "approved": True,
                    "placed": False,
                    "reason": f"broker error: {e}",
                    "ladder_id": ladder_id,
                }
            )
            return 1

        await bot.post_message(
            f"✅ **{args.symbol}** placed {len(placed)} ladder orders "
            f"(ladder_id=`{ladder_id}`)"
        )

        _emit(
            {
                "approved": True,
                "placed": True,
                "ladder_id": ladder_id,
                "orders": [
                    {
                        "order_id": s.order_id,
                        "price": s.price,
                        "qty": s.qty,
                        "status": s.status,
                    }
                    for s in placed
                ],
            }
        )
        return 0


def cmd_propose(args) -> int:
    return asyncio.run(_propose_async(args))


# ---------------------------------------------------------------------------
# Subcommand: cancel / cancel-all
# ---------------------------------------------------------------------------


def cmd_cancel(args) -> int:
    from broker import BrokerError, load_broker

    broker = load_broker()
    try:
        broker.cancel(args.order_id)
    except BrokerError as e:
        _emit({"cancelled": False, "order_id": args.order_id, "error": str(e)})
        return 1
    _emit({"cancelled": True, "order_id": args.order_id})
    return 0


def cmd_cancel_all(args) -> int:
    from broker import BrokerError, load_broker

    broker = load_broker()
    open_orders = broker.list_open_orders()
    cancelled = []
    errors = []
    for o in open_orders:
        try:
            broker.cancel(o.order_id)
            cancelled.append(o.order_id)
        except BrokerError as e:
            errors.append({"order_id": o.order_id, "error": str(e)})
    _emit({"cancelled": cancelled, "errors": errors})
    return 0 if not errors else 1


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="earnings-trader")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser(
        "fetch-amc-context",
        help="Emit JSON context (print + quote + headlines + prior quarters) for today's AMC reporters or the specified test symbols",
    )
    p_fetch.add_argument(
        "--symbol",
        action="append",
        help="Test mode: fetch most recent historical print for this ticker. Repeatable.",
    )
    p_fetch.add_argument(
        "--for-date",
        help="Test mode: use earnings calendar from a specific YYYY-MM-DD (default: today).",
    )
    p_fetch.set_defaults(fn=cmd_fetch_amc_context)

    p_acct = sub.add_parser(
        "account-snapshot",
        help="Emit JSON: broker account + ledger + risk headroom",
    )
    p_acct.set_defaults(fn=cmd_account_snapshot)

    p_list = sub.add_parser("list-orders", help="Emit JSON: open broker orders")
    p_list.set_defaults(fn=cmd_list_orders)

    p_prop = sub.add_parser(
        "propose",
        help="Build a laddered limit order, risk-check, Discord ✅/❌, place on approval",
    )
    p_prop.add_argument("--symbol", required=True)
    p_prop.add_argument("--target", required=True, type=float, help="Anchor price for the ladder")
    p_prop.add_argument("--shares", required=True, type=int)
    p_prop.add_argument(
        "--rationale",
        required=True,
        help="One- or two-sentence justification shown in Discord embed",
    )
    p_prop.add_argument("--side", choices=["buy", "sell"], default="buy")
    p_prop.add_argument("--down-band", type=float, default=0.10)
    p_prop.add_argument("--up-band", type=float, default=0.05)
    p_prop.add_argument("--rungs", type=int, default=10)
    p_prop.add_argument("--weight-ratio", type=float, default=5.0)
    p_prop.add_argument("--extended-hours", action="store_true")
    p_prop.add_argument("--tif", default="day", choices=["day", "gtc", "ioc", "fok", "opg", "cls"])
    p_prop.add_argument(
        "--phase",
        help="Phase tag for logs (e.g. post-amc, premarket, manual). Default 'manual'.",
    )
    p_prop.add_argument(
        "--timeout-s",
        type=int,
        default=90,
        help="Seconds to wait for Discord approval reaction",
    )
    p_prop.add_argument(
        "--dry-run",
        action="store_true",
        help="Build ladder + risk-check but don't post to Discord or place orders",
    )
    p_prop.set_defaults(fn=cmd_propose)

    p_cancel = sub.add_parser("cancel", help="Cancel one open order")
    p_cancel.add_argument("--order-id", required=True)
    p_cancel.set_defaults(fn=cmd_cancel)

    p_cancel_all = sub.add_parser("cancel-all", help="Cancel every open order")
    p_cancel_all.set_defaults(fn=cmd_cancel_all)

    args = parser.parse_args(argv)
    _load_env()
    _configure_logging(args.verbose)
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
