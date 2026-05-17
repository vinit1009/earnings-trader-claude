# Strategy: Daily Recap (4:05 PM ET)

**Fires:** weekdays at 4:05 PM ET — 5 minutes after the regular market closes (and 4 minutes after the post-amc routine starts).

**Purpose:** End-of-day rollup. Today's trades, today's P&L, today's hit-rate, observations for tomorrow.

**Does NOT trade.** Pure reporting.

## Notion state (read at start, write at end)

See `references/notion_state.md` for the schema. For this phase:

**Read at start:**
- `Daily Log` DB — filter by Date=today, read every row (one per phase that ran). This is your source of truth for "what proposals/approvals/fills happened today" — far more reliable than scraping Discord.
- `Positions` DB — current open positions for the "open positions (unrealized)" section of the recap.
- `Handoffs` page, section `open-drift → daily-recap` — notes from open-drift on what to highlight. After reading, **clear this section**.

**Write at end:**
- `Daily Log` DB — append your own row (Run Title=`YYYY-MM-DD daily-recap`, Phase=daily-recap, Status, Trades Proposed=0, Realized P&L Today from `daily-summary` CLI, Summary=Discord recap)
- `Observations` page — if you spotted a pattern worth flagging (3 consecutive fades, ladder fill <30%, etc.), append a bullet under the current week's `## Week of YYYY-MM-DD` section. Create the section if it doesn't exist.
- `Handoffs` page, section `daily-recap → next-day post-amc` — replace with ≤5 bullets for tomorrow's post-amc routine (e.g. "Yesterday's NVDA still drifting +1.2% — PEAD active, prefer hold over fade on similar setups tomorrow")

**Concurrency note**: this phase fires at 16:05, only 4 minutes after post-amc starts at 16:01. Post-amc may still be running. That's OK — read the Daily Log as a snapshot; anything missing from it will appear in ah-close's row tonight or tomorrow's recap.

## Workflow

1. `python scripts/trade.py daily-summary` → JSON with today's fills, by-symbol netting, current positions, account state.

2. Compute / extract:
   - **Trades placed today**: count of fills × 2 (each ladder rung is a fill)
   - **Symbols traded today**: distinct symbols in by_symbol output
   - **Realized P&L today** (per symbol): `sells - buys` for symbols where buy_qty == sell_qty (closed round-trips)
   - **Unrealized P&L** (per open position): `unrealized_pl` from open_positions
   - **Total P&L today**: `pnl_today` from account block (realized + unrealized)
   - **Hit rate**: of closed round-trips, what % were positive?

3. Cross-reference with Discord history (you can `Read` past messages in #earnings since 4 PM yesterday) to identify:
   - Which proposals were approved vs rejected
   - Which were placed but didn't fill (rungs that sit out of the money)
   - Any system errors posted

4. Post a single Discord embed (or formatted message) summarizing the day:

```
📊 **Daily Recap — {date}**

Account:
• Equity: ${equity}  (${pnl_today:+.2f} vs yesterday)
• Cash: ${cash}  Positions held: {open_position_count}

Trades today:
• Filled: {fill_count} rungs across {symbol_count} symbols
• Symbols: {symbols comma-separated}

Round-trips closed today:
• {SYM}: bought {qty} @ avg ${buy_avg}, sold @ avg ${sell_avg} → ${pnl:+.2f} ({pct:+.2f}%)
• {SYM}: ...

Open positions (unrealized):
• {SYM}: {qty} @ ${entry}, now ${current}, ${unrealized_pl:+.2f}

Observations for tomorrow:
{one or two sentences — patterns you noticed in today's prints, names to watch in tomorrow's pre-market, sector flow, etc.}
```

5. If `daily_loss_cap` was hit today, note it explicitly:

```
⚠️ Daily loss cap reached (-${MAX_DAILY_LOSS_USD}). System was in defensive mode for the rest of the session.
```

## Discipline

- **Be honest about losses.** Don't gloss over a bad day. The recap is a learning artifact, not a brag log.
- **Don't propose changes here.** That's tomorrow's job. The recap reports, doesn't decide.
- **Watch for patterns.** If you've been faded by guidance language 3 days in a row, that's signal to tune the strategy.
- **Note ladder fill efficiency.** If you typically only fill 30% of rungs, the ladder is too wide. If 100% of rungs fill instantly, it's too narrow.

## What to NOT include

- Real-time price quotes (the market is closed; quotes are stale)
- Speculation about tomorrow's prints (that's `amc-brief`'s job)
- Personal commentary or motivation talk
- Long-form analysis (keep the recap < 30 lines, scannable)

## When the day was quiet

If 0 trades happened today and 0 positions are held, post a one-liner:

```
📊 No trades today. Equity flat at ${equity}.
```

That's a perfectly fine outcome. Patient discipline > forced action.
