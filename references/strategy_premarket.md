# Strategy: Pre-Market (7:00 AM ET)

**Fires:** weekdays at 7:00 AM ET — 2.5 hours before market open.

**Purpose:** Two parallel jobs:
1. **Overnight position review**: for positions held from yesterday, has the thesis flipped? News since 8 PM ET, analyst rating changes, futures direction, pre-market price action.
2. **BMO scan**: companies that released earnings before market open (BMO) between 4 AM and 7 AM. Same composite-signal decision logic as post-amc, applied to morning reporters.

## Workflow

### Part A: Overnight position review (every fire)

1. `python scripts/trade.py review-positions --news-hours 14` → positions held + recent news (last 14 hours covers the 5 PM cut-off through this morning).

2. For each held position:
   - **News flipped against thesis**: flatten via sell ladder at current pre-market price. Use the same propose pattern as `ah-close`.
   - **Pre-market gap matches thesis**: hold, but adjust GTC exits if you set them.
   - **Pre-market gap against thesis**: re-evaluate. Often the right call is flatten and wait for `open-drift` to decide whether to re-enter.

### Part B: BMO scan (most days have BMO reporters)

3. `python scripts/trade.py fetch-bmo-context` → today's filtered BMO reporters with print numbers + pre-market quote + headlines + prior quarters (~3-5 min due to Finnhub rate limits).

4. `python scripts/trade.py account-snapshot` → confirm risk headroom (especially if you've already used some position slots overnight).

5. For each BMO reporter, run the **same decision tree as post-amc**:
   - Hard skips: EPS miss >5%, revenue miss >3%, guidance lowered
   - Composite signal: numeric surprise + headlines tone + tape sanity
   - Skip is the default; propose only on strong signals (≥+2 net)

6. For each survivor, call `propose --phase premarket --extended-hours ...`.

7. Post a Discord summary:

```
☀️ **Pre-Market**
Held positions reviewed: {N} (flattened: {K}, held: {N-K})
BMO universe: {M} reporters analyzed
   • Proposed: {symbols + composite scores}
   • Skipped: {count} ({reasons summary})
Equity: ${equity} (today P&L: ${pnl_today:+.2f})
```

## Discipline

### Position review

- **Trust news flow over technical bounces.** A bullish position with bad overnight news is a flatten regardless of how the pre-market chart looks.
- **Analyst rating changes are leading indicators.** A downgrade from a major bank pre-market often telegraphs the open weakness.
- **Don't average down on overnight losers.** That's not a strategy, it's a way to compound mistakes.
- **Watch correlated names.** If you bought NVDA on earnings and AMD's BMO print today is a miss, the semi sector will gap. Flatten NVDA before the open if AMD bombs.

### BMO scan

- **BMO liquidity is thinner than AMC.** Use wider ladders or smaller size — AH ladders that worked at 4:30 PM may not fill at 7:30 AM.
- **Pre-market headlines are sparser** (only 3 hours of coverage). Tone scoring is weaker → be more conservative.
- **News-only signals without numeric confirmation = skip.** "CEO sounded confident" with a -2% EPS miss is still a miss.
- **Watch for "BMO followups" of last night's AMC prints.** Sometimes AMC reporters drop additional details (guidance Q&A clips, analyst notes) BMO. These don't count as separate reporters but they do affect held positions.

## Daily loss cap & position cap

These query Alpaca and are auto-enforced by the `propose` CLI. But if `account-snapshot` shows you're already at 3 concurrent positions with held overnight positions, the BMO scan won't be able to enter any new ones — only flatten the existing.

## When BMO is empty

Many days have zero BMO reporters on the filtered universe. Post a one-liner ("ℹ️ no BMO reporters today") and skip part B. Always do part A (position review).
