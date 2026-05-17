# Strategy: Opening Bell + Drift (9:25 AM ET)

**Fires:** weekdays at 9:25 AM ET — 5 minutes before market open.

**Purpose:** Decide opening-bell strategy for positions and any final BMO reporter adjustments. The two main plays:
1. **Fade the gap**: if a position gapped beyond the implied move, the open often mean-reverts in the first 10-30 min.
2. **Ride the drift**: if the post-print/overnight reaction is *underreacting* vs implied move, PEAD says the trend continues for hours-to-days.

This is the last chance to set the entry/exit for the day before the regular session starts.

## Workflow

1. `python scripts/trade.py account-snapshot` → headroom check.
2. `python scripts/trade.py review-positions --news-hours 2` → freshest pre-market state per position.
3. `python scripts/trade.py list-orders` → any pre-market orders from premarket phase that haven't filled? Decide cancel-and-re-issue.

4. For each held position, classify:

| Pre-market move vs entry | Pre-market move vs implied | Action |
|---|---|---|
| Positive | Less than implied | **PEAD ride** — leave the position. Set GTC sell at +1.5x your entry move target. |
| Positive | Equal to implied | **Take partial** — sell 50% at current pre-market price via tight ladder. |
| Positive | Beyond implied | **Fade your own move** — sell full position at current pre-market price (likely to mean-revert in first 30 min). |
| Negative | Less than -implied | **Re-evaluate.** Cancel any GTC stops. Wait for first 5 min of regular session to confirm direction. |
| Negative | Beyond -implied | **Flatten at open** — submit market-on-open sell. The downside is likely overshooting but the stop logic protects against further drop. |
| Flat | — | Convert to GTC limit ladders for both sides around target. Let the regular session decide. |

5. For each decision, propose via the CLI. Order types vary:
   - **Tight limit ladders** for take-partial / fade decisions: use `--side sell --extended-hours --down-band 0.02 --up-band 0.03 --rungs 5`.
   - **MOO (market-on-open)** for flatten-at-open: not directly supported by our ladder; use a single-rung limit at current pre-market price - 0.5% (`--rungs 1`).
   - **GTC ladders** for "set targets and let it ride": `--tif gtc --extended-hours false --down-band 0.05 --up-band 0.10`.

6. For BMO reporters proposed in `premarket` phase but not yet entered: re-check thesis if pre-market price moved a lot in the meantime. Cancel and re-propose at fresh target if so.

7. Post a Discord summary:

```
🔔 **Opening Bell**
Positions managed: {N}
   • {SYM}: PEAD ride (gap +5% < implied 8%)
   • {SYM}: partial 50% sold (gap +12% = implied 12%)
   • {SYM}: faded (gap +20% > implied 15%, expecting mean-revert)
   • {SYM}: flatten-at-open (gap -18% > -implied 10%, stops out)

Equity: ${equity} (today P&L: ${pnl_today:+.2f})
Open orders heading into regular session: {count}
```

## Discipline

- **Don't trade against your premarket plan unless news changed.** The 9:25 decision should mostly be reaffirming what premarket already decided.
- **Fades are higher-conviction than rides** for this system. PEAD is a known statistical effect but it's hourly-to-daily; the opening fade window is 10-30 min and often clear from the tape.
- **Beware the "premium" opening minute.** First 30-90 seconds often gap-fill from afterhours. Don't enter MOO blindly into volatility — limit orders only.
- **Daily loss cap check.** If P&L today is at or below -$150 (75% of cap), be defensive: prefer flattens over fresh entries.

## Special case: no held positions, no BMO scans pending

If `review-positions` returns empty AND no pending orders from earlier phases, post one-liner ("ℹ️ no opening positions to manage") and exit. The regular session is on its own until the next AMC brief at 3:50 PM.

## Estimating implied move

Finnhub free tier doesn't expose options-implied move directly. Workarounds:
- **Historical proxy**: average absolute move of the last 4 earnings reactions (visible via `prior_quarters` in fetch-amc-context output). Decent first approximation.
- **Manual override in `watchlist.yaml`**: per-ticker `implied_move_pct` field. Set manually if you have option data from another source. The CLI doesn't currently read this, but the strategy doc can — for now Claude can use the prior-quarters average as the implied move proxy.

If unsure, **default to PEAD ride** (do less) rather than fade (do more) — entry costs and slippage favor passivity.
