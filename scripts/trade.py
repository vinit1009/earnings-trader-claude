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


def _check_macro_events() -> dict:
    """Cross-check today + tomorrow against references/macro_calendar.yaml.

    Returns a dict listing any matching event categories. Empty lists mean no
    blackout in effect.
    """
    import yaml

    calendar_path = REPO_ROOT / "references" / "macro_calendar.yaml"
    today = _dt.date.today()
    tomorrow = today + _dt.timedelta(days=1)
    result = {
        "today": today.isoformat(),
        "tomorrow": tomorrow.isoformat(),
        "today_events": [],
        "tomorrow_events": [],
        "market_closed_today": False,
        "market_closed_tomorrow": False,
    }
    if not calendar_path.exists():
        return result
    try:
        with open(calendar_path) as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return result
    year_block = cfg.get(today.year) or {}
    for category, dates in year_block.items():
        if not isinstance(dates, list):
            continue
        if today.isoformat() in dates:
            result["today_events"].append(category)
            if category == "market_closed":
                result["market_closed_today"] = True
        if tomorrow.isoformat() in dates:
            result["tomorrow_events"].append(category)
            if category == "market_closed":
                result["market_closed_tomorrow"] = True
    return result


# ---------------------------------------------------------------------------
# Subcommand: fetch-amc-context
# ---------------------------------------------------------------------------


def _vol_to_ladder_bands(vol: float | None) -> tuple[float, float]:
    """Map realized vol to (down_band, up_band) ladder parameters.

    Low-vol names react less to earnings noise; widen down_band only as necessary.
    """
    if vol is None or 20.0 <= vol < 45.0:
        return (0.08, 0.02)  # default
    if vol < 20.0:
        return (0.05, 0.02)  # low-vol: tighter entry band
    if vol < 70.0:
        return (0.12, 0.03)  # elevated vol: wider entry band
    return (0.15, 0.05)     # very high vol (PLTR, RIVN, MSTR)


def _fetch_earnings_context(
    args,
    *,
    window: str,                  # "amc" or "bmo"
    require_actuals: bool,        # post-print mode vs preview mode
    news_hours: int,              # 0 = skip news fetch (preview); else hours of news to pull
) -> int:
    from earnings import (
        compute_beat_consistency,
        compute_implied_move_proxy,
        get_ah_volume_today,
        get_company_metrics,
        get_company_news,
        get_pre_earnings_drift,
        get_quote,
        get_realized_vol,
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

    min_market_cap = float(filters.get("min_market_cap_usd", 1_000_000_000))
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

        exclude_syms: set[str] = set()
        if getattr(args, "exclude", None):
            exclude_syms = {s.strip().upper() for s in args.exclude.split(",") if s.strip()}

        window_events = [
            e
            for e in all_events
            if _hour_match(e)
            and (not require_actuals or e.eps_actual is not None)
            and e.date == target_date.isoformat()
            and e.symbol not in exclude_syms
        ]
        logging.info(
            "fetch %s: %d reporters today before filtering (require_actuals=%s, excluded=%d)",
            window, len(window_events), require_actuals, len(exclude_syms),
        )

        # Compute pending: expected reporters that have no actuals yet and weren't excluded.
        # Emitted in output so the caller can decide whether to poll again.
        all_window = [
            e for e in all_events
            if _hour_match(e) and e.date == target_date.isoformat()
        ]
        pending_symbols = [
            e.symbol for e in all_window
            if e.eps_actual is None and e.symbol not in exclude_syms
        ]

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

    # In test mode (--symbol), pending is not meaningful.
    if args.symbol:
        pending_symbols = []

    if not events:
        _emit(
            {
                "reporters": [],
                "filtered_out": rejected,
                "pending_symbols": pending_symbols,
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
        beat_consistency = compute_beat_consistency(history)
        realized_vol_30d = get_realized_vol(e.symbol, days=30)
        pre_earnings_drift_5d = get_pre_earnings_drift(e.symbol)
        ladder_down, ladder_up = _vol_to_ladder_bands(realized_vol_30d)
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

        # Flag reporters whose actuals were filed before the expected window.
        # AMC reporters with actuals at 9 AM or BMO reporters already in Finnhub the
        # night before likely pre-announced — the surprise is already priced in.
        import datetime as _dt2
        now_et_hour = (_dt2.datetime.utcnow().hour - 4) % 24  # rough ET hour
        pre_announced = (
            e.eps_actual is not None
            and (
                (e.hour == "amc" and now_et_hour < 15)   # actuals before 3 PM on AMC day
                or (e.hour == "bmo" and now_et_hour >= 16)  # actuals filed the evening before
            )
        )

        out.append(
            {
                "symbol": e.symbol,
                "date": e.date,
                "hour": e.hour,
                "pre_announced": pre_announced,
                "metrics": (
                    None
                    if m is None
                    else {
                        "market_cap_usd": m.market_cap_usd,
                        "avg_volume_10d": m.avg_volume_10d,
                        "exchange": m.exchange,
                        "country": m.country,
                        "industry": m.finnhub_industry,
                        "short_ratio": m.short_ratio,
                    }
                ),
                "print": {
                    "eps_estimate": e.eps_estimate,
                    "eps_actual": e.eps_actual,
                    "eps_surprise_pct": e.eps_surprise_pct(),
                    "revenue_estimate": e.revenue_estimate,
                    "revenue_actual": e.revenue_actual,
                    "revenue_surprise_pct": e.revenue_surprise_pct(),
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
                "beat_consistency": beat_consistency,
                "implied_move": {
                    "proxy_pct": implied_move_pct,
                    "current_move_pct": current_move_pct,
                    "ah_move_ratio": ah_move_ratio,
                    "classification": move_classification,
                },
                "realized_vol_30d_pct": realized_vol_30d,
                "pre_earnings_5d_drift_pct": pre_earnings_drift_5d,
                "ah_volume_today": get_ah_volume_today(e.symbol),
                "suggested_ladder": {
                    "down_band": ladder_down,
                    "up_band": ladder_up,
                },
            }
        )

    # Sector crowding: flag industries with 2+ reporters tonight
    import collections as _collections
    _industry_map: dict[str, list[str]] = _collections.defaultdict(list)
    for r in out:
        ind = (r.get("metrics") or {}).get("industry") or "unknown"
        _industry_map[ind].append(r["symbol"])
    sector_crowding = {ind: syms for ind, syms in _industry_map.items() if len(syms) >= 2}

    _emit(
        {
            "reporters": out,
            "count": len(out),
            "sector_crowding": sector_crowding,
            "pending_symbols": pending_symbols,
            "pending_count": len(pending_symbols),
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
    import datetime as _dt
    from broker import load_broker
    from earnings import get_analyst_signals, get_company_news, get_quote

    broker = load_broker()
    positions = broker.list_positions()
    open_orders = broker.list_open_orders()

    analyst_since = _dt.date.today() - _dt.timedelta(days=max(1, args.news_hours // 24))

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
        analyst = get_analyst_signals(sym, analyst_since)
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
                "analyst_signals": analyst,
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
        get_trading_mode,
    )

    broker = load_broker()
    acct = broker.get_account()
    snap = AccountSnapshot.from_broker(broker)
    positions = broker.list_positions()
    regime = get_market_regime()
    macro = _check_macro_events()
    trading_mode = get_trading_mode(acct.pnl_today())

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
            "macro_events": macro,
            "trading_mode": trading_mode,
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

        # Re-fetch price after approval — guard against stale orders.
        # The user may have taken 1–5 minutes to react; price can move materially.
        if args.side == "buy" and current_price and current_price > 0:
            fresh_quote = get_quote(args.symbol)
            if fresh_quote:
                drift_pct = (fresh_quote.current - current_price) / current_price * 100
                implied_for_stale = getattr(args, "implied_move_pct", None) or 0.0

                if drift_pct < -7.0:
                    # Hard abort: price cratered since proposal — thesis may have broken
                    msg = (
                        f"⚠️ **{args.symbol}** approval received but price dropped "
                        f"{drift_pct:+.1f}% since proposal "
                        f"(${current_price:.2f} → ${fresh_quote.current:.2f}). "
                        f"Orders NOT placed — re-run `propose` with fresh data."
                    )
                    await bot.post_message(msg)
                    _emit(
                        {
                            "approved": True,
                            "placed": False,
                            "reason": f"stale_price: dropped {drift_pct:+.1f}%",
                            "ladder_id": ladder_id,
                        }
                    )
                    return 0

                if drift_pct > 5.0 and implied_for_stale > 0:
                    # Hard abort if stock surged past fade_candidate threshold
                    new_ah_pct = abs(fresh_quote.pct_change())
                    new_ratio = new_ah_pct / implied_for_stale
                    if new_ratio > 1.5:
                        msg = (
                            f"⚠️ **{args.symbol}** is now a fade_candidate "
                            f"(ratio {new_ratio:.2f}) after {drift_pct:+.1f}% surge "
                            f"since proposal. Orders NOT placed."
                        )
                        await bot.post_message(msg)
                        _emit(
                            {
                                "approved": True,
                                "placed": False,
                                "reason": f"stale_price: fade_candidate ratio={new_ratio:.2f}",
                                "ladder_id": ladder_id,
                            }
                        )
                        return 0

                if abs(drift_pct) > 3.0:
                    # Soft warn — moderate drift, proceed but flag it
                    await bot.post_message(
                        f"ℹ️ **{args.symbol}** drifted {drift_pct:+.1f}% since proposal "
                        f"(${current_price:.2f} → ${fresh_quote.current:.2f}). Placing anyway."
                    )

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
# Subcommand: fetch-press-release  (SEC EDGAR 8-K Item 2.02 text)
# ---------------------------------------------------------------------------


def cmd_fetch_press_release(args) -> int:
    from edgar import fetch_latest_earnings_8k

    on_or_after = None
    if args.since:
        try:
            on_or_after = _dt.date.fromisoformat(args.since)
        except ValueError:
            _emit({"error": f"invalid --since {args.since!r} (use YYYY-MM-DD)"})
            return 1

    release = fetch_latest_earnings_8k(
        args.symbol, on_or_after=on_or_after, max_chars=args.max_chars
    )
    if release is None:
        _emit({"found": False, "symbol": args.symbol.upper()})
        return 0
    _emit(
        {
            "found": True,
            "symbol": release.symbol,
            "cik": release.cik,
            "accession_number": release.accession_number,
            "filing_date": release.filing_date,
            "filed_today": release.filed_today,
            "is_call_transcript": release.is_call_transcript,
            "primary_doc_url": release.primary_doc_url,
            "item_codes": release.item_codes,
            "guidance_direction": release.guidance_direction,
            "secondary_offering_detected": release.secondary_offering_detected,
            "char_count": len(release.text),
            "text": release.text,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: fetch-call-transcript  (EDGAR 8-K Item 7.01 — conference call)
# ---------------------------------------------------------------------------


def cmd_fetch_call_transcript(args) -> int:
    """Fetch the conference call transcript from EDGAR (8-K Item 7.01).

    Many companies file their earnings call transcript as a Regulation FD disclosure
    (Item 7.01) within 1-3 hours of the call ending. The ah-close routine can call this
    before making the 25% staged entry decision.
    """
    from edgar import fetch_latest_earnings_8k

    on_or_after = None
    if args.since:
        try:
            on_or_after = _dt.date.fromisoformat(args.since)
        except ValueError:
            _emit({"error": f"invalid --since {args.since!r} (use YYYY-MM-DD)"})
            return 1

    release = fetch_latest_earnings_8k(
        args.symbol, item_code="7.01", on_or_after=on_or_after, max_chars=args.max_chars
    )
    if release is None:
        _emit({"found": False, "symbol": args.symbol.upper()})
        return 0
    _emit(
        {
            "found": True,
            "symbol": release.symbol,
            "cik": release.cik,
            "accession_number": release.accession_number,
            "filing_date": release.filing_date,
            "filed_today": release.filed_today,
            "is_call_transcript": release.is_call_transcript,
            "primary_doc_url": release.primary_doc_url,
            "item_codes": release.item_codes,
            "char_count": len(release.text),
            "text": release.text,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: rolling-stats  (used by daily-recap to detect strategy drift)
# ---------------------------------------------------------------------------


def cmd_rolling_stats(args) -> int:
    """Compute win rate / expectancy / fill rate from the last N days of fills."""
    from broker import load_broker

    broker = load_broker()
    today = _dt.date.today()
    cutoff_date = today - _dt.timedelta(days=args.days * 2)
    try:
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status="closed", after=cutoff_date.isoformat(), limit=500)
        raw_orders = broker._client.get_orders(filter=req)
    except Exception as e:
        _emit({"error": f"order pull failed: {e}", "days": args.days})
        return 1

    fills_by_symbol: dict[str, list[dict]] = {}
    submitted_by_symbol: dict[str, int] = {}
    for o in raw_orders:
        sym = o.symbol
        submitted_by_symbol[sym] = submitted_by_symbol.get(sym, 0) + 1
        if not o.filled_at:
            continue
        fills_by_symbol.setdefault(sym, []).append(
            {
                "side": str(o.side).split(".")[-1].lower(),
                "qty": float(o.filled_qty or 0),
                "price": float(o.filled_avg_price or 0),
                "filled_at": o.filled_at,
            }
        )

    roundtrips: list[dict] = []
    for sym, fills in fills_by_symbol.items():
        fills.sort(key=lambda f: f["filled_at"])
        buy_q: list[dict] = []
        for f in fills:
            if f["side"] == "buy":
                buy_q.append(f)
                continue
            qty_to_close = f["qty"]
            while qty_to_close > 0 and buy_q:
                buy = buy_q[0]
                take = min(qty_to_close, buy["qty"])
                pnl = (f["price"] - buy["price"]) * take
                roundtrips.append(
                    {
                        "symbol": sym,
                        "opened_at": buy["filled_at"].isoformat() if buy["filled_at"] else None,
                        "closed_at": f["filled_at"].isoformat() if f["filled_at"] else None,
                        "qty": take,
                        "buy_price": buy["price"],
                        "sell_price": f["price"],
                        "pnl_usd": round(pnl, 2),
                        "pnl_pct": round((f["price"] - buy["price"]) / buy["price"] * 100, 2),
                    }
                )
                qty_to_close -= take
                buy["qty"] -= take
                if buy["qty"] <= 1e-6:
                    buy_q.pop(0)

    window_start = today - _dt.timedelta(days=args.days)
    in_window = [
        r for r in roundtrips
        if r["closed_at"] and r["closed_at"][:10] >= window_start.isoformat()
    ]

    wins = [r for r in in_window if r["pnl_usd"] > 0]
    losses = [r for r in in_window if r["pnl_usd"] < 0]
    total = len(in_window)
    win_rate = len(wins) / total * 100 if total else None
    avg_win = sum(r["pnl_usd"] for r in wins) / len(wins) if wins else 0.0
    avg_loss = sum(r["pnl_usd"] for r in losses) / len(losses) if losses else 0.0
    expectancy = (
        (len(wins) / total) * avg_win + (len(losses) / total) * avg_loss
        if total else None
    )

    submitted_total = sum(submitted_by_symbol.values())
    filled_total = sum(len(v) for v in fills_by_symbol.values())
    fill_rate = filled_total / submitted_total * 100 if submitted_total else None

    _emit(
        {
            "window_days": args.days,
            "window_start": window_start.isoformat(),
            "window_end": today.isoformat(),
            "roundtrip_count": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "expectancy_per_trade_usd": round(expectancy, 2) if expectancy is not None else None,
            "ladder_fill_rate_pct": round(fill_rate, 1) if fill_rate is not None else None,
            "recent_roundtrips": in_window[-10:],
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: force-flatten-stale
# Flatten any position held longer than max_age_days trading days.
# PEAD effect decays after ~5 sessions; anything older is an unplanned directional bet.
# ---------------------------------------------------------------------------


def _trading_days_between(start: _dt.date, end: _dt.date) -> int:
    """Count Mon–Fri days between start (exclusive) and end (inclusive).

    Does not subtract US holidays (close enough for our stale-age check — a
    holiday-heavy week would over-count by at most 1–2 days, which errs on the
    side of holding slightly longer, not cutting too early).
    """
    count = 0
    d = start + _dt.timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        d += _dt.timedelta(days=1)
    return count


def cmd_force_flatten_stale(args) -> int:
    """Check each open Alpaca position against Notion 'Opened Date' passed via --position.

    The calling Claude agent queries the Notion Positions DB via MCP, then passes
    each held symbol and its opened date as `--position SYMBOL:YYYY-MM-DD` args.
    Any position held longer than --max-age-days trading days is flagged as stale.

    The command emits a JSON list of stale positions.  The agent loop then calls
    `propose --side sell` for each flagged symbol.

    Example:
        python scripts/trade.py force-flatten-stale \\
            --position AAPL:2026-05-10 \\
            --position NVDA:2026-05-09 \\
            --max-age-days 5
    """
    from broker import load_broker

    broker = load_broker()
    positions = broker.list_positions()
    today = _dt.date.today()

    # Parse --position SYMBOL:YYYY-MM-DD pairs supplied by the calling agent
    notion_opened: dict[str, str] = {}
    for entry in (args.position or []):
        if ":" not in entry:
            logging.warning("force-flatten-stale: skipping bad --position %r (expected SYMBOL:YYYY-MM-DD)", entry)
            continue
        sym, date_str = entry.split(":", 1)
        notion_opened[sym.upper()] = date_str.strip()

    if not positions:
        _emit({"stale_positions": [], "note": "no open positions"})
        return 0

    if not notion_opened:
        _emit(
            {
                "stale_positions": [],
                "skipped_no_date": [p.symbol.upper() for p in positions],
                "note": (
                    "No --position args supplied. Pass opened dates from the Notion Positions DB: "
                    "--position SYMBOL:YYYY-MM-DD for each held position."
                ),
            }
        )
        return 0

    stale = []
    skipped_no_date = []

    for pos in positions:
        sym = pos.symbol.upper()
        opened_iso = notion_opened.get(sym)
        if not opened_iso:
            skipped_no_date.append(sym)
            continue
        try:
            opened_date = _dt.date.fromisoformat(opened_iso)
        except ValueError:
            logging.warning("force-flatten-stale: bad date %r for %s", opened_iso, sym)
            skipped_no_date.append(sym)
            continue
        age_days = _trading_days_between(opened_date, today)
        if age_days > args.max_age_days:
            stale.append(
                {
                    "symbol": sym,
                    "opened_date": opened_iso,
                    "age_trading_days": age_days,
                    "qty": pos.qty,
                    "avg_entry_price": pos.avg_entry_price,
                    "market_value": pos.market_value,
                    "unrealized_pl": pos.unrealized_pl,
                }
            )

    if skipped_no_date:
        logging.warning(
            "force-flatten-stale: no Notion Opened Date supplied for %s — skipping staleness check.",
            skipped_no_date,
        )

    if not stale:
        _emit(
            {
                "stale_positions": [],
                "skipped_no_date": skipped_no_date,
                "note": f"no positions older than {args.max_age_days} trading days",
            }
        )
        return 0

    # Emit so the calling agent can propose sell ladders for each stale position.
    _emit(
        {
            "stale_positions": stale,
            "skipped_no_date": skipped_no_date,
            "max_age_days": args.max_age_days,
            "action_required": (
                "For each symbol below, call: propose --side sell --symbol X "
                "--target <current_price> --shares <qty> --rationale 'stale: held N days'"
                if not args.dry_run
                else "dry_run=true — no action taken"
            ),
        }
    )
    return 0


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
    p_fetch.add_argument(
        "--exclude",
        default="",
        metavar="SYM1,SYM2",
        help="Comma-separated symbols to skip. Used in polling loops to avoid re-processing already-handled reporters.",
    )
    p_fetch.set_defaults(fn=cmd_fetch_amc_context)

    p_fetch_bmo = sub.add_parser(
        "fetch-bmo-context",
        help="Emit JSON context for today's BMO (before-market-open) reporters",
    )
    p_fetch_bmo.add_argument("--symbol", action="append")
    p_fetch_bmo.add_argument("--for-date")
    p_fetch_bmo.add_argument(
        "--exclude",
        default="",
        metavar="SYM1,SYM2",
        help="Comma-separated symbols to skip (already processed in a prior poll cycle).",
    )
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
        default=300,
        help="Seconds to wait for Discord approval reaction (default 300 = 5 min; use 60 for sell ladders where speed matters)",
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
        help="Historical implied-move proxy for this ticker (used to size the hard stop). 0 = use 5-pct floor.",
    )
    p_prop.set_defaults(fn=cmd_propose)

    p_press = sub.add_parser(
        "fetch-press-release",
        help="Fetch the latest 8-K Item 2.02 (earnings release) from SEC EDGAR",
    )
    p_press.add_argument("--symbol", required=True)
    p_press.add_argument(
        "--since",
        help="Only return filings on/after this date (YYYY-MM-DD). Default: 2 days ago.",
    )
    p_press.add_argument("--max-chars", type=int, default=15000)
    p_press.set_defaults(fn=cmd_fetch_press_release)

    p_transcript = sub.add_parser(
        "fetch-call-transcript",
        help="Fetch the earnings call transcript from EDGAR (8-K Item 7.01, Regulation FD)",
    )
    p_transcript.add_argument("--symbol", required=True)
    p_transcript.add_argument(
        "--since",
        help="Only return filings on/after this date (YYYY-MM-DD). Default: 2 days ago.",
    )
    p_transcript.add_argument("--max-chars", type=int, default=15000)
    p_transcript.set_defaults(fn=cmd_fetch_call_transcript)

    p_stats = sub.add_parser(
        "rolling-stats",
        help="Compute win rate / expectancy / fill rate over the last N days for feedback loop",
    )
    p_stats.add_argument("--days", type=int, default=5)
    p_stats.set_defaults(fn=cmd_rolling_stats)

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

    p_stale = sub.add_parser(
        "force-flatten-stale",
        help="Flag positions held longer than max-age-days trading days. Pass opened dates from Notion via --position SYMBOL:YYYY-MM-DD.",
    )
    p_stale.add_argument(
        "--position",
        action="append",
        metavar="SYMBOL:YYYY-MM-DD",
        help="Opened date for a held position. Repeat for each symbol. Example: --position AAPL:2026-05-10",
    )
    p_stale.add_argument(
        "--max-age-days",
        type=int,
        default=5,
        help="Trading days before a position is considered stale (default 5)",
    )
    p_stale.add_argument(
        "--dry-run",
        action="store_true",
        help="List stale positions without emitting sell action",
    )
    p_stale.set_defaults(fn=cmd_force_flatten_stale)

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
