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


def _fetch_earnings_context(
    args,
    *,
    window: str,                  # "amc" or "bmo"
    require_actuals: bool,        # post-print mode vs preview mode
    news_hours: int,              # 0 = skip news fetch (preview); else hours of news to pull
) -> int:
    from earnings import (
        compute_implied_move_proxy,
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
        def _hour_match(e):
            return (window == "amc" and e.is_amc) or (window == "bmo" and e.is_bmo)

        window_events = [
            e
            for e in all_events
            if _hour_match(e)
            and (not require_actuals or e.eps_actual is not None)
            and e.date == target_date.isoformat()
        ]
        logging.info(
            "fetch %s: %d reporters today before filtering (require_actuals=%s)",
            window, len(window_events), require_actuals,
        )

        events = []
        rejected = []
        for e in window_events:
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
                "note": f"no {window.upper()} reporters passed filters today",
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

        news = (get_company_news(e.symbol, hours=news_hours) or []) if news_hours > 0 else []
        history = get_recent_earnings(e.symbol, quarters=4)

        implied_move_pct = compute_implied_move_proxy(e.symbol, history)
        current_move_pct = q.pct_change() if q else None
        if implied_move_pct and current_move_pct is not None and implied_move_pct > 0:
            ah_move_ratio = abs(current_move_pct) / implied_move_pct
        else:
            ah_move_ratio = None

        if ah_move_ratio is None:
            move_classification = "unknown"
        elif ah_move_ratio < 0.5:
            move_classification = "pead_candidate"
        elif ah_move_ratio < 1.0:
            move_classification = "neutral"
        elif ah_move_ratio < 1.5:
            move_classification = "partial_take_candidate"
        else:
            move_classification = "fade_candidate_skip"

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
                "implied_move": {
                    "proxy_pct": implied_move_pct,
                    "current_move_pct": current_move_pct,
                    "ah_move_ratio": ah_move_ratio,
                    "classification": move_classification,
                },
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


# Thin wrappers for each window/mode.
def cmd_fetch_amc_context(args) -> int:
    return _fetch_earnings_context(
        args, window="amc", require_actuals=True, news_hours=1
    )


def cmd_fetch_bmo_context(args) -> int:
    return _fetch_earnings_context(
        args, window="bmo", require_actuals=True, news_hours=12
    )


def cmd_fetch_earnings_preview(args) -> int:
    """Pre-print mode: list reporters who are EXPECTED to release in `--window`
    today, with estimates + prior quarter history. No actuals required, no
    news fetch. Used by amc-brief (3:50 PM ET) and premarket (7:00 AM ET)."""
    return _fetch_earnings_context(
        args, window=args.window, require_actuals=False, news_hours=0
    )


# ---------------------------------------------------------------------------
# Subcommand: review-positions
# ---------------------------------------------------------------------------


def cmd_review_positions(args) -> int:
    from broker import load_broker
    from earnings import get_company_news, get_quote

    broker = load_broker()
    positions = broker.list_positions()
    open_orders = broker.list_open_orders()

    out = []
    for p in positions:
        sym = p.symbol.upper()
        q = get_quote(sym)
        pct_change_since_entry = (
            (p.avg_entry_price - q.current) / q.current * 100
            if q is not None and p.avg_entry_price > 0
            else None
        )
        news = (
            get_company_news(sym, hours=args.news_hours) or []
            if args.news_hours > 0
            else []
        )
        ords = [
            {
                "order_id": o.order_id,
                "side": o.side,
                "qty": o.qty,
                "filled_qty": o.filled_qty,
                "price": o.price,
                "status": o.status,
                "extended_hours": o.extended_hours,
            }
            for o in open_orders
            if o.symbol.upper() == sym
        ]
        out.append(
            {
                "symbol": sym,
                "qty": p.qty,
                "avg_entry_price": p.avg_entry_price,
                "market_value": p.market_value,
                "unrealized_pl": p.unrealized_pl,
                "current_price": q.current if q else None,
                "pct_change_today": q.pct_change() if q else None,
                "pct_vs_entry": (
                    (q.current - p.avg_entry_price) / p.avg_entry_price * 100
                    if q and p.avg_entry_price > 0
                    else None
                ),
                "open_orders_for_symbol": ords,
                "headlines": [
                    {
                        "headline": n.headline,
                        "source": n.source,
                        "published_at": n.published_at.isoformat(),
                        "summary": n.summary[:240] if n.summary else "",
                    }
                    for n in news[:15]
                ],
            }
        )

    _emit({"positions": out, "count": len(out)})
    return 0


# ---------------------------------------------------------------------------
# Subcommand: daily-summary
# ---------------------------------------------------------------------------


def cmd_daily_summary(args) -> int:
    """End-of-day rollup. Pulls today's fills + current positions + P&L.
    Used by the daily-recap routine to post a Discord summary."""
    import datetime as _dt
    from broker import load_broker, BrokerError

    broker = load_broker()
    acct = broker.get_account()
    positions = broker.list_positions()

    today_iso = _dt.date.today().isoformat()
    try:
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status="closed", after=today_iso)
        raw_orders = broker._client.get_orders(filter=req)
    except Exception as e:
        raw_orders = []
        logging.warning("could not pull today's closed orders: %s", e)

    fills = []
    for o in raw_orders:
        if not o.filled_at:
            continue
        fills.append(
            {
                "order_id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side).split(".")[-1].lower(),
                "qty": float(o.filled_qty or 0),
                "price": float(o.filled_avg_price or 0),
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            }
        )

    by_symbol: dict[str, dict] = {}
    for f in fills:
        s = by_symbol.setdefault(
            f["symbol"], {"symbol": f["symbol"], "buys": 0.0, "sells": 0.0, "buy_qty": 0.0, "sell_qty": 0.0}
        )
        if f["side"] == "buy":
            s["buys"] += f["price"] * f["qty"]
            s["buy_qty"] += f["qty"]
        else:
            s["sells"] += f["price"] * f["qty"]
            s["sell_qty"] += f["qty"]

    _emit(
        {
            "date": today_iso,
            "account": {
                "cash": acct.cash,
                "equity": acct.equity,
                "last_equity": acct.last_equity,
                "pnl_today": acct.pnl_today(),
            },
            "fills_today_count": len(fills),
            "by_symbol": list(by_symbol.values()),
            "open_positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_entry_price": p.avg_entry_price,
                    "market_value": p.market_value,
                    "unrealized_pl": p.unrealized_pl,
                }
                for p in positions
            ],
            "open_position_count": len(positions),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: account-snapshot
# ---------------------------------------------------------------------------


def cmd_account_snapshot(args) -> int:
    from broker import load_broker
    from earnings import get_market_regime
    from risk import (
        AccountSnapshot,
        MAX_CONCURRENT_POSITIONS,
        MAX_DAILY_LOSS_USD,
        MAX_PER_POSITION_USD,
        MAX_PER_SECTOR_POSITIONS,
    )

    broker = load_broker()
    acct = broker.get_account()
    snap = AccountSnapshot.from_broker(broker)
    positions = broker.list_positions()
    regime = get_market_regime()

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
            "market_regime": regime,
            "risk_caps": {
                "max_per_position_usd": MAX_PER_POSITION_USD,
                "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
                "max_daily_loss_usd": MAX_DAILY_LOSS_USD,
                "max_per_sector_positions": MAX_PER_SECTOR_POSITIONS,
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
    from earnings import get_company_metrics, get_quote
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

    industries: dict[str, str] = {}
    if args.side == "buy":
        all_syms = {args.symbol.upper()} | set(snapshot.open_positions.keys())
        for sym in all_syms:
            m = get_company_metrics(sym)
            if m and m.finnhub_industry:
                industries[sym] = m.finnhub_industry

    risk = check_orders(
        orders,
        snapshot,
        current_price=current_price,
        industries=industries,
        allow_sector_double=getattr(args, "allow_sector_double", False),
    )
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

        recommended_hard_stop = None
        if args.side == "buy":
            implied = getattr(args, "implied_move_pct", None) or 0.0
            stop_pct = max(0.05, (implied / 100.0) * 0.75) if implied > 0 else 0.05
            avg_fill_est = average_fill_price(orders)
            recommended_hard_stop = round(avg_fill_est * (1 - stop_pct), 2)

        _emit(
            {
                "approved": True,
                "placed": True,
                "ladder_id": ladder_id,
                "recommended_hard_stop": recommended_hard_stop,
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
# Subcommand: place-stop  (hard-stop enforcement, called by ah-close/premarket/open-drift)
# ---------------------------------------------------------------------------


def cmd_place_stop(args) -> int:
    from broker import BrokerError, load_broker

    broker = load_broker()
    pos = broker.get_position(args.symbol)
    if pos is None:
        _emit({"placed": False, "symbol": args.symbol, "reason": "no open position"})
        return 1
    qty = args.qty if args.qty else pos.qty
    if qty <= 0:
        _emit({"placed": False, "symbol": args.symbol, "reason": "qty <= 0"})
        return 1
    try:
        status = broker.place_stop_limit(
            symbol=args.symbol,
            qty=qty,
            stop_price=args.stop_price,
            limit_price=args.limit_price,
            tif="gtc",
            extended_hours=args.extended_hours,
        )
    except BrokerError as e:
        _emit({"placed": False, "symbol": args.symbol, "error": str(e)})
        return 1
    _emit(
        {
            "placed": True,
            "order_id": status.order_id,
            "symbol": status.symbol,
            "qty": status.qty,
            "stop_price": args.stop_price,
            "limit_price": args.limit_price or round(args.stop_price * 0.99, 2),
            "tif": "gtc",
        }
    )
    return 0


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

    p_fetch_bmo = sub.add_parser(
        "fetch-bmo-context",
        help="Emit JSON context for today's BMO (before-market-open) reporters",
    )
    p_fetch_bmo.add_argument("--symbol", action="append")
    p_fetch_bmo.add_argument("--for-date")
    p_fetch_bmo.set_defaults(fn=cmd_fetch_bmo_context)

    p_prev = sub.add_parser(
        "fetch-earnings-preview",
        help="Pre-print mode: list EXPECTED reporters (estimates only, no actuals required) with prior quarter history. For amc-brief / premarket planning.",
    )
    p_prev.add_argument("--window", choices=["amc", "bmo"], required=True)
    p_prev.add_argument("--symbol", action="append")
    p_prev.add_argument("--for-date")
    p_prev.set_defaults(fn=cmd_fetch_earnings_preview)

    p_review = sub.add_parser(
        "review-positions",
        help="JSON: open positions with current price + P&L + recent news per ticker. For ah-close / premarket / open-drift.",
    )
    p_review.add_argument(
        "--news-hours",
        type=int,
        default=4,
        help="Hours of news to pull per ticker (default 4)",
    )
    p_review.set_defaults(fn=cmd_review_positions)

    p_summary = sub.add_parser(
        "daily-summary",
        help="JSON: today's fills, by-symbol netted P&L, current positions. For daily-recap.",
    )
    p_summary.set_defaults(fn=cmd_daily_summary)

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
    p_prop.add_argument(
        "--allow-sector-double",
        action="store_true",
        help="Override the sector correlation cap (allow a second position in the same industry)",
    )
    p_prop.add_argument(
        "--implied-move-pct",
        type=float,
        default=0.0,
        help="Historical implied-move proxy for this ticker (used to size the hard stop). 0 = use 5% floor.",
    )
    p_prop.set_defaults(fn=cmd_propose)

    p_stop = sub.add_parser(
        "place-stop",
        help="Place a GTC stop-limit SELL to enforce a hard stop on an existing position",
    )
    p_stop.add_argument("--symbol", required=True)
    p_stop.add_argument("--stop-price", required=True, type=float)
    p_stop.add_argument("--limit-price", type=float, help="Limit price (default: stop * 0.99)")
    p_stop.add_argument("--qty", type=float, help="Shares (default: full position)")
    p_stop.add_argument("--extended-hours", action="store_true")
    p_stop.set_defaults(fn=cmd_place_stop)

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
