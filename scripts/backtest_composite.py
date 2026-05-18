"""Composite scoring backtest & tier calibration.

Runs the graduated EPS scoring model against 15 tickers × last 4 quarters of
real Finnhub/Alpaca data, then prints a calibration report.

Usage:
    cd /path/to/earnings-trader
    python3 scripts/backtest_composite.py

Requires: FINNHUB_KEY, ALPACA_KEY_ID, ALPACA_SECRET in environment.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

# Allow running from repo root or scripts/ dir
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

# Auto-load .env — try repo root first, then parent directory
def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in [
        Path(_here).parent / ".env",          # earnings-trader/.env
        Path(_here).parent.parent / ".env",   # robinhood/.env
        Path.home() / ".env",                  # ~/.env
    ]:
        if candidate.exists():
            load_dotenv(candidate)
            print(f"Loaded env from {candidate}")
            return

_load_env()

_MISSING_KEYS = [k for k in ("FINNHUB_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET") if not os.environ.get(k)]
if _MISSING_KEYS:
    print("=" * 70)
    print("ERROR: Missing required environment variables:")
    for k in _MISSING_KEYS:
        print(f"  {k}")
    print()
    print("Create a .env file at:")
    print(f"  {Path(_here).parent / '.env'}")
    print()
    print("Template (copy from .env.example):")
    print("  FINNHUB_KEY=your_key_here")
    print("  ALPACA_KEY_ID=your_key_here")
    print("  ALPACA_SECRET=your_secret_here")
    print()
    print("Or set them in your shell before running:")
    print("  export FINNHUB_KEY=xxx ALPACA_KEY_ID=yyy ALPACA_SECRET=zzz")
    print("  python3 scripts/backtest_composite.py")
    print("=" * 70)
    sys.exit(1)

from earnings import (
    _post_print_abs_move_pct,
    compute_beat_consistency,
    compute_implied_move_proxy,
    get_announcement_dates,
    get_recent_earnings,
)

TICKERS = [
    # Mega-cap — expected sandbagging
    "NVDA", "META", "MSFT", "AAPL", "GOOGL",
    # Large-cap tech / semis
    "AMZN", "NFLX", "AVGO", "AMD", "PANW",
    # Mid-cap / high-vol / mixed signals
    "INTC", "CRM", "SNOW", "UBER", "PLTR",
]


def graduated_eps_points(surprise_pct: float | None) -> int:
    if surprise_pct is None or surprise_pct <= 2.0:
        return 0
    if surprise_pct <= 7.0:
        return 1
    if surprise_pct <= 15.0:
        return 2
    return 3


def map_to_tier(composite: float) -> str:
    if composite >= 5.5:
        return "S"
    if composite >= 4.0:
        return "A"
    if composite >= 3.0:
        return "C"
    return "skip"


def find_announcement_date(
    period_end: _dt.date, ann_dates: list[_dt.date]
) -> _dt.date:
    for d in ann_dates:
        if period_end <= d <= period_end + _dt.timedelta(days=90):
            return d
    return period_end + _dt.timedelta(days=45)


def run_backtest() -> None:
    rows: list[dict] = []

    for symbol in TICKERS:
        print(f"  fetching {symbol}...", flush=True)
        try:
            history = get_recent_earnings(symbol, quarters=4)
            if not history:
                print(f"    {symbol}: no history returned")
                continue

            bc = compute_beat_consistency(history)
            ann_dates = get_announcement_dates(symbol)
            implied_proxy = compute_implied_move_proxy(symbol, history)

            for q in history:
                if not q.date:
                    continue
                try:
                    period_end = _dt.date.fromisoformat(q.date)
                except ValueError:
                    continue

                ann_date = find_announcement_date(period_end, ann_dates)
                eps_surprise = q.eps_surprise_pct()
                eps_pts = graduated_eps_points(eps_surprise)

                sandbagging_adj = 1 if bc["sandbagging_flag"] else 0
                adjusted_eps_pts = max(0, eps_pts - sandbagging_adj)

                # Qualitative floor: +3 if beat (>2%), else 0
                # Simulates guidance raised + tape + confident tone for a genuine beat
                qual_floor = 3 if (eps_surprise is not None and eps_surprise > 2.0) else 0

                # Beat rate bonus
                beat_bonus = (
                    0.5
                    if (bc["beat_rate_4q"] == 1.0 and not bc["sandbagging_flag"])
                    else 0.0
                )

                composite = adjusted_eps_pts + qual_floor + beat_bonus

                tier = map_to_tier(composite)

                actual_move = _post_print_abs_move_pct(symbol, ann_date)
                note = "ok"
                if actual_move is None:
                    note = "no_bar_data"

                ratio = None
                if actual_move is not None and implied_proxy and implied_proxy > 0:
                    ratio = round(actual_move / implied_proxy, 2)

                rows.append(
                    {
                        "symbol": symbol,
                        "ann_date": ann_date.isoformat(),
                        "eps_surprise": eps_surprise,
                        "eps_pts_raw": eps_pts,
                        "sandbagging_adj": sandbagging_adj,
                        "adj_eps_pts": adjusted_eps_pts,
                        "qual_floor": qual_floor,
                        "beat_bonus": beat_bonus,
                        "composite": composite,
                        "tier": tier,
                        "actual_1d_abs_pct": actual_move,
                        "implied_proxy_pct": implied_proxy,
                        "ah_move_ratio": ratio,
                        "sandbagging_flag": bc["sandbagging_flag"],
                        "avg_eps_surprise_4q": bc["avg_eps_surprise_4q"],
                        "beat_rate_4q": bc["beat_rate_4q"],
                        "note": note,
                    }
                )
        except Exception as e:
            print(f"    {symbol}: ERROR — {e}")
            continue

    if not rows:
        print("No data collected.")
        return

    _print_table(rows)
    _print_calibration(rows)


def _fmt(v, fmt=".1f", suffix="") -> str:
    if v is None:
        return "  n/a "
    return f"{v:{fmt}}{suffix}"


def _print_table(rows: list[dict]) -> None:
    print()
    print("=" * 110)
    print("BACKTEST RESULTS — Composite Scoring Calibration")
    print("=" * 110)
    hdr = (
        f"{'TICKER':<6} {'ANN_DATE':<12} {'EPS%':>7} {'EPS_PTS':>7} "
        f"{'SB_ADJ':>6} {'ADJ_EPS':>7} {'QUAL':>5} {'BONUS':>5} "
        f"{'COMP':>5} {'TIER':>4} {'ACTUAL_1D':>9} {'IMPLIED':>7} {'RATIO':>5} NOTE"
    )
    print(hdr)
    print("-" * 110)

    prev_sym = None
    for r in rows:
        if r["symbol"] != prev_sym and prev_sym is not None:
            print()
        prev_sym = r["symbol"]

        eps_str = _fmt(r["eps_surprise"], "+.1f", "%") if r["eps_surprise"] is not None else "   n/a"
        actual_str = _fmt(r["actual_1d_abs_pct"], "+.1f", "%") if r["actual_1d_abs_pct"] is not None else "   n/a"
        implied_str = _fmt(r["implied_proxy_pct"], ".1f", "%") if r["implied_proxy_pct"] is not None else "  n/a"
        ratio_str = _fmt(r["ah_move_ratio"], ".2f") if r["ah_move_ratio"] is not None else " n/a"
        sb_flag = "Y" if r["sandbagging_flag"] else "N"

        print(
            f"{r['symbol']:<6} {r['ann_date']:<12} {eps_str:>7} "
            f"{r['eps_pts_raw']:>7} {r['sandbagging_adj']:>6}({sb_flag}) "
            f"{r['adj_eps_pts']:>7} {r['qual_floor']:>5} {r['beat_bonus']:>5.1f} "
            f"{r['composite']:>5.1f} {r['tier']:>4} {actual_str:>9} "
            f"{implied_str:>7} {ratio_str:>5}  {r['note']}"
        )

    print()


def _print_calibration(rows: list[dict]) -> None:
    print("=" * 110)
    print("CALIBRATION ANALYSIS")
    print("=" * 110)

    # Filter to rows with actual data
    valid = [r for r in rows if r["actual_1d_abs_pct"] is not None]
    if not valid:
        print("No rows with bar data available for calibration.")
        return

    # By tier
    print("\n--- Mean actual 1-day abs move by tier (higher is better for the bull thesis) ---")
    for tier in ["S", "A", "C", "skip"]:
        tier_rows = [r for r in valid if r["tier"] == tier]
        if not tier_rows:
            print(f"  Tier {tier}: no data")
            continue
        mean_move = sum(r["actual_1d_abs_pct"] for r in tier_rows) / len(tier_rows)
        composites = [r["composite"] for r in tier_rows]
        mean_comp = sum(composites) / len(composites)
        print(
            f"  Tier {tier:4s}: n={len(tier_rows):2d}  mean_actual={mean_move:+.1f}%  "
            f"mean_composite={mean_comp:.1f}"
        )

    # By EPS surprise bucket
    print("\n--- Mean actual 1-day abs move by EPS surprise bucket ---")
    buckets = [
        ("miss/flat (≤2%)", lambda r: r["eps_surprise"] is None or r["eps_surprise"] <= 2.0),
        ("weak beat (2-7%)", lambda r: r["eps_surprise"] is not None and 2.0 < r["eps_surprise"] <= 7.0),
        ("strong beat (7-15%)", lambda r: r["eps_surprise"] is not None and 7.0 < r["eps_surprise"] <= 15.0),
        ("blowout (>15%)", lambda r: r["eps_surprise"] is not None and r["eps_surprise"] > 15.0),
    ]
    for label, fn in buckets:
        bucket_rows = [r for r in valid if fn(r)]
        if not bucket_rows:
            print(f"  {label}: no data")
            continue
        mean_move = sum(r["actual_1d_abs_pct"] for r in bucket_rows) / len(bucket_rows)
        print(f"  {label:<28}: n={len(bucket_rows):2d}  mean_actual={mean_move:+.1f}%")

    # Sandbagging comparison
    print("\n--- Sandbagging vs non-sandbagging (same EPS tier: strong beat 7-15%) ---")
    strong_beat = [
        r for r in valid
        if r["eps_surprise"] is not None and 7.0 < r["eps_surprise"] <= 15.0
    ]
    sb_rows = [r for r in strong_beat if r["sandbagging_flag"]]
    non_sb_rows = [r for r in strong_beat if not r["sandbagging_flag"]]
    if sb_rows:
        mean_sb = sum(r["actual_1d_abs_pct"] for r in sb_rows) / len(sb_rows)
        print(f"  Sandbagging names  : n={len(sb_rows):2d}  mean_actual={mean_sb:+.1f}%")
    else:
        print("  Sandbagging names  : no data in 7-15% bucket")
    if non_sb_rows:
        mean_non = sum(r["actual_1d_abs_pct"] for r in non_sb_rows) / len(non_sb_rows)
        print(f"  Non-sandbagging    : n={len(non_sb_rows):2d}  mean_actual={mean_non:+.1f}%")
    else:
        print("  Non-sandbagging    : no data in 7-15% bucket")

    # Composite distribution
    print("\n--- Composite score distribution across all 15 tickers ---")
    composites = [r["composite"] for r in rows]
    from collections import Counter
    counts = Counter(str(int(c)) if c == int(c) else str(c) for c in composites)
    min_c = min(composites)
    max_c = max(composites)
    mean_c = sum(composites) / len(composites)
    print(f"  min={min_c:.1f}  max={max_c:.1f}  mean={mean_c:.1f}")
    score_groups = {}
    for r in rows:
        bucket = "≥8 (S)" if r["composite"] >= 8 else ("6-7 (A)" if r["composite"] >= 6 else ("3-5 (C)" if r["composite"] >= 3 else "<3 (skip)"))
        score_groups.setdefault(bucket, []).append(r["composite"])
    for label in ["≥8 (S)", "6-7 (A)", "3-5 (C)", "<3 (skip)"]:
        grp = score_groups.get(label, [])
        print(f"  {label:<12}: n={len(grp):2d} quarters  ({len(grp)/len(rows)*100:.0f}%)")

    # Verdict
    print("\n--- CALIBRATION VERDICT ---")
    tier_s = [r for r in valid if r["tier"] == "S"]
    tier_c = [r for r in valid if r["tier"] == "C"]

    if tier_s and tier_c:
        mean_s = sum(r["actual_1d_abs_pct"] for r in tier_s) / len(tier_s)
        mean_c = sum(r["actual_1d_abs_pct"] for r in tier_c) / len(tier_c)
        if mean_s > mean_c * 1.2:
            verdict = "GOOD — Tier S produces materially stronger actual moves than Tier C. Thresholds validated."
        elif mean_s > mean_c:
            verdict = "MARGINAL — Tier S slightly better than C but not definitively. Consider lowering S to ≥7."
        else:
            verdict = "RECALIBRATE — Tier S not outperforming Tier C. Lower S threshold to ≥7, A to 5-6, C to 3-4."
        print(f"  Tier S mean actual: {mean_s:.1f}%  vs  Tier C mean actual: {mean_c:.1f}%")
        print(f"  → {verdict}")
    elif not tier_s:
        print("  WARNING: No Tier S quarters found — S threshold of ≥8 may be too high.")
        print("  Consider lowering to ≥7.")
        # Check what the max composite is
        max_comp = max(r["composite"] for r in rows)
        print(f"  Highest composite seen in this universe: {max_comp:.1f}")
    else:
        print("  Not enough data for verdict.")

    # Per-ticker summary
    print("\n--- Per-ticker summary ---")
    syms = list(dict.fromkeys(r["symbol"] for r in rows))
    for sym in syms:
        sym_rows = [r for r in rows if r["symbol"] == sym]
        bc_flag = sym_rows[0]["sandbagging_flag"] if sym_rows else None
        avg_surp = sym_rows[0]["avg_eps_surprise_4q"] if sym_rows else None
        beat_rate = sym_rows[0]["beat_rate_4q"] if sym_rows else None
        composites_sym = [r["composite"] for r in sym_rows]
        tiers_sym = [r["tier"] for r in sym_rows]
        sb_str = "SANDBAGGING" if bc_flag else "normal"
        print(
            f"  {sym:<6} avg_eps_surp={_fmt(avg_surp,'+.1f','%'):>8}  "
            f"beat_rate={_fmt(beat_rate,'.2f'):>5}  {sb_str:<11}  "
            f"composites={[f'{c:.1f}' for c in composites_sym]}  tiers={tiers_sym}"
        )

    print()
    print("=" * 110)


if __name__ == "__main__":
    print("Earnings Composite Scoring Backtest")
    print(f"Tickers: {', '.join(TICKERS)}")
    print(f"Date: {_dt.date.today().isoformat()}")
    print()
    print("Fetching data (may take 1-2 min due to Finnhub rate limits)...")
    print()
    run_backtest()
