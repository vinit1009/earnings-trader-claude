# Strategy: After-Hours Close (7:55 PM ET)

**Fires:** weekdays at 7:55 PM ET — 5 minutes before the after-hours session ends.

**Purpose:** Decide what to do with positions opened in the post-AMC session (4:01 PM ET) before they roll into overnight risk. For each open position: hold overnight, flatten via extended-hours limit, or convert to a GTC sell order that fires at next open.

**This phase is the safety net** for the post-AMC routine. Without it, positions sit unmanaged from 8 PM ET to 9:30 AM the next morning.

## Notion state (read at start, write at end)

See `references/notion_state.md` for the schema. For this phase:

**Read at start:**
- `Positions` DB — every open position with its provenance. Critically: filter by `Opened By Phase = post-amc` AND `Opened Date = today (ET)` to identify positions you're responsible for tonight. Pre-existing carryovers are not your job — premarket handles those tomorrow.
- `Handoffs` page, section `post-amc → ah-close` — notes from post-amc (e.g. "META guidance clip at 6:30 PM"). After reading, **clear this section**.

**Write at end:**
- `Positions` DB — for each position you touched: update Current P&L %, Last Touched By=ah-close, Last Touched At=now. If you flattened, **archive/delete the row**.
- `Daily Log` DB — append one row (Run Title=`YYYY-MM-DD ah-close`, Phase=ah-close, Status, Trades Proposed=sell ladders posted, Trades Approved, Trades Filled, Summary=Discord post)
- `Handoffs` page, section `ah-close → premarket` — replace with ≤5 bullets for what premarket should watch (e.g. "Held META through guidance Q&A — thesis still intact, watch for overnight downgrades")

## Workflow

1. `python scripts/trade.py account-snapshot` → confirm current equity, P&L today, headroom.
2. `python scripts/trade.py review-positions --news-hours 4` → JSON of every open position with current AH price, P&L since entry, recent news headlines (last 4 hours of post-print coverage).
3. `python scripts/trade.py list-orders` → confirm no orphaned open buy orders. Cancel any that didn't fill (the prints are stale by now).

4. For **each open position** taken today, decide:

   ### Decision tree

   - **News confirms thesis + price action matches**: hold overnight. Don't flatten.
   - **News flipped against the thesis** (e.g., bought on "raised guidance" but post-print headlines reveal a SEC investigation, an executive departure, a competitor warning): flatten. Use `propose --side sell` with a tight ladder near current AH price.
   - **Price moved violently in your favor** (>+15% from entry): take partial profits. Sell 50% via limit ladder at current AH price.
   - **Price moved against you in AH** (down >5% from entry): re-evaluate. If news supports thesis, hold and accept the move. If news is neutral/negative, flatten or set a GTC stop near current price.
   - **Position is illiquid in AH** (no fills available, spread > 2% of price): leave it. Convert decision to `open-drift` (9:25 AM ET next day).

5. For each flatten/partial decision, call:

   ```
   python scripts/trade.py propose --symbol X --side sell \
       --target P --shares N \
       --rationale "..." \
       --extended-hours --phase ah-close \
       --down-band 0.03 --up-band 0.05 --rungs 5
   ```

   Notes:
   - **Sell ladders are tighter than buy ladders** (0.03 / 0.05 bands vs 0.10 / 0.05) — you want fills, not perfection on the exit.
   - **Fewer rungs** (5) — AH liquidity is thin, large ladders mostly don't fill.
   - **Discord approval still required.** Same flow.

6. Cancel any remaining unfilled buy orders from the post-AMC session:

   ```
   python scripts/trade.py list-orders
   # for each open buy order whose ladder filled partially or not at all:
   python scripts/trade.py cancel --order-id ...
   ```

7. Post a Discord summary:

```
🌙 **AH Close**
Reviewed {N} positions:
• {SYM}: held (thesis intact, +X% vs entry)
• {SYM}: flattened (news flipped — guidance miss in Q&A)
• {SYM}: partial 50% sold (gapped +18%, took some off)

Open orders canceled: {count}
Equity: ${equity} (today P&L: ${pnl_today:+.2f})
```

## Discipline

- **Flatten when in doubt.** Earnings volatility resets overnight. If you're not sure, the overnight gap risk is asymmetric — you can re-enter at open if the thesis still looks good.
- **Don't average down on a losing AH position.** That's the next phase's call (premarket / open-drift) with fresh data, not yours.
- **Respect AH liquidity.** Sell-side limits at extreme prices won't fill. Aim near current AH price ±3%.
- **Honor the daily loss cap.** If today's P&L is at or below -$200, flatten aggressively. The system is in defensive mode.

## When to skip

- **No open positions:** post "ℹ️ no positions to review" and exit. Quiet days happen.
- **All positions still in their entry ladders (not filled yet):** the AH ladder rungs may still be working. Leave them. Cancel any rungs at unreachable prices.

## What's tracked

Everything is observable via Alpaca's order/position API and Discord history. No local state needed.
