# Strategy: Post-AMC Earnings Reaction

Invoked by a /schedule routine at **16:01 America/New_York** on weekdays. You (Claude) are the brain; `trade.py` is the body — it fetches data, builds ladders, posts to Discord for approval, and places orders. Your job is to read the data, apply judgment, and decide what to propose.

## Notion state (read at start, write at end)

See `references/notion_state.md` for the schema. For this phase:

**Read at start:**
- `Positions` DB — current open positions (so you don't double-add to AAPL if you already hold it)
- `Observations` page — strategy-tuning patterns from prior days/weeks (look at the most recent week section)
- `Handoffs` page, section `daily-recap → next-day post-amc` — overnight notes from yesterday's recap, if any. Clear that section after reading.

**Write at end:**
- `Positions` DB — for each new position you opened, upsert a row (Symbol, Opened By Phase=post-amc, Opened Date=today ET, Composite Score, Original Thesis, Target Price, Stop Plan, Ladder Rungs Filled=0 initially, Avg Entry Price from Alpaca, Last Touched By=post-amc, Last Touched At=now UTC)
- `Daily Log` DB — append one row (Run Title=`YYYY-MM-DD post-amc`, Phase=post-amc, Status=ok|partial|error, Trades Proposed, Trades Approved, Trades Filled, Summary=Discord post text, Errors=any)
- `Handoffs` page, section `post-amc → ah-close` — replace contents with ≤5 bullets for what ah-close should watch (e.g. "META guidance Q&A clip drops ~6:30 PM — re-read tone before deciding")

## Workflow

1. Call `python scripts/trade.py fetch-amc-context` → JSON of **every** US-listed AMC reporter today that passes the universe filter (market cap ≥$2B, avg volume ≥1M, price ≥$5, US common stock — no hand-picked ticker list), with print numbers, current quote, recent headlines, and prior quarters' surprise history. Expect 5–25 names on a busy day, 0 on a quiet one.
2. Call `account-snapshot` → confirm current positions and risk headroom (you can't propose anything if caps are hit).
3. For **each** reporter, analyze and decide: skip, or propose a buy ladder. Follow the decision rules below — they are constraints, not suggestions.
4. For each proposal, call `trade.py propose ...` with a one- to two-sentence `--rationale`. The Discord embed shows your rationale to the user, who taps ✅ or ❌.
5. After all reporters are processed, summarize what you did (skipped X, proposed Y, placed Z) and exit.

## Decision rules — hard skips (never propose)

These are **non-negotiable**. If any of these are true, skip the ticker and do not propose:

- **EPS missed estimate by more than 5%** (eps_surprise_pct < -5)
- **Revenue missed estimate by more than 3%** (when revenue numbers are present)
- **Guidance explicitly lowered** in the press release / headlines
- **Stock halted** or no quote available
- **Already-open position in this ticker would exceed the $500 per-position cap** (check account-snapshot first)
- **You're at 3 concurrent positions and don't already own this one** (the broker will reject anyway, but don't propose to begin with)
- **Today's realized P&L is at or below -$200** (daily loss cap — `propose` will block buys, no exits to manage)

## Decision rules — composite signal

For the survivors of the hard skips, build a composite from these inputs:

**Bullish points (count each that applies):**

- EPS surprise > +2%
- Revenue surprise > +1% (only count when revenue numbers are present)
- Headlines describe "raised guidance", "raised outlook", "beat", "record" — without hedging
- CEO/CFO language in headlines/quotes is confident: concrete numbers, raised outlook, references to specific growth drivers
- The current after-hours tape is positive (`quote.pct_change > 0`) and the headlines align with the tape direction

**Bearish points:**

- EPS surprise < -2%
- Revenue surprise < -1%
- Headlines use hedging language: "headwinds", "navigating", "weather", "challenging", "near-term pressure"
- Defensive Q&A characterizations
- Tape is red >2% while you have an otherwise-bullish read (tape disagreement is a red flag)

**Decision:**

- `bullish - bearish ≥ 4` → **strong bullish** — propose with full size ($400 notional)
- `bullish - bearish` is 2 or 3 → **moderate bullish** — propose with reduced size ($250 notional)
- `bullish - bearish` < 2 → **weak signal** — skip
- `bullish - bearish ≤ -2` → **bearish** — skip (MVP doesn't open shorts)

## Sizing & ladder shape

Default ladder for a buy proposal:

- `target` = `quote.current * 0.98` (enter 2% below the AH tape — don't chase)
- `shares` = `floor(position_usd / target)`
- `down-band` = 0.08 (8% below target → lowest rung)
- `up-band` = 0.02 (2% above target → highest rung)
- `rungs` = 10
- `weight-ratio` = 5.0 (heaviest weight on the lowest rungs)
- `extended-hours` = true (so the order is eligible for AH execution)
- `tif` = day

**Per-ticker overrides** in `watchlist.yaml` (e.g. TSLA 15%/8% bands, NVDA 12%/7%) should override the defaults — read the YAML and use those values.

**Fade mode**: if the tape is already extended *in our favor* (>+8% after-hours), reduce position size by 50% AND widen the down-band by 1.5x (an overshoot is more likely to mean-revert than to keep running). Mention "fade mode" in the rationale.

## Rationale format

The `--rationale` string is displayed in the Discord embed. Keep it under 240 chars. Format:

```
EPS X.XX vs est Y.YY (+Z%). [revenue line if present]. AH price $X (+/-Y%).
Read: <one short sentence> — direction=<bullish/neutral>, guidance=<raised/maintained/unknown>, tone=<confident/neutral/hedgy>.
```

Example for a hypothetical NVDA strong beat:

```
EPS 2.10 vs est 1.79 (+17%). Revenue $48B vs est $43B (+12%). AH +6.5%.
Read: clean beat + raised guidance + confident commentary on data center growth. Bullish 6/0.
```

## Risk caps (auto-enforced by `propose`)

You do **not** need to enforce these manually — `propose` will block any order that violates them. But knowing them helps you avoid wasting calls:

- Per-ticker exposure ≤ **$500**
- Concurrent open positions ≤ **3**
- Daily realized P&L floor ≥ **-$200**
- Each rung price within **±15%** of the current bid/ask (typo guard)

Check `account-snapshot` at the start to see remaining headroom — if you're at 3 positions, prefer to add to an existing one rather than skipping outright.

## Common pitfalls

- **Don't propose just because a print beat.** A 3.6% beat with no guidance signal isn't enough — that's a routine "in-line" print. Skip it.
- **Don't trust the tape alone.** If AH price is +5% but every headline is bearish, the tape is wrong and will reverse overnight. Read the news.
- **Don't chase.** Target should be *below* the current tape (2% by default). If you enter at the tape and it dips, you're underwater immediately.
- **Watch for "double earnings" days.** If 4+ companies report at 4:00 PM ET, you cannot propose all of them — you only have 3 position slots. Rank by signal strength, propose the top 1-2.

## When to skip the whole phase

If `fetch-amc-context` returns zero reporters, exit cleanly. Zero is fine — it means no US company reporting after-market today passed the liquidity/size filter. Don't try to relax the filter on the fly to find trades; the filter exists to keep us out of illiquid AH names. A quiet day is a quiet day.
