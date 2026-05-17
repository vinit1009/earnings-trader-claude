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
- **Same industry/sector already held** — `propose` blocks any new ticker whose `finnhub_industry` matches an open position. Three semis is one bet with 3× size. The override `--allow-sector-double` exists for rare cases (e.g., the second name is a hedge), use sparingly.
- **Today's realized P&L is at or below -$200** (daily loss cap — `propose` will block buys, no exits to manage)
- **Implied-move classification is `fade_candidate_skip`** — tape has overreacted past 1.5× the historical earnings move. We can't short, so we skip.

## Decision rules — trading mode gate (NEW, check before macro-event)

`account-snapshot` returns a `trading_mode` block based on today's running P&L:

| Mode | P&L threshold | Behavior |
|---|---|---|
| `normal` | > -$100 | Standard playbook |
| `cautious` | -$100 to -$150 | Only tier S/A new opens; no opens in last hour of regular session |
| `defensive` | -$150 to -$180 | **No new opens.** Flatten any position down >5% immediately. |
| `exit_only` | -$180 to -$200 | Flatten all positions with negative P&L. No new orders except sells. |
| `halt` | ≤ -$200 | Cancel every open order. No orders of any kind. Discord alert. |

`risk.check_orders` enforces these — even if you call `propose`, the CLI will reject. But knowing the mode upfront saves the wasted call. Mention the mode in your Discord summary.

## Decision rules — macro-event gate (NEW, check before anything else)

`account-snapshot` now emits a `macro_events` block. Check `today_events` and `tomorrow_events`:

| Event today/tomorrow | Behavior in post-amc |
|---|---|
| `fomc` | **Tier S only.** Most catalysts get overwhelmed by Fed positioning. |
| `cpi` | **Tier S only.** Same reasoning. |
| `nfp` (next-day) | Normal sizing — but downgrade tier by 1 for any name that's macro-sensitive (banks, homebuilders). |
| `opex_fridays` (next-day) | Skip new opens with high beta to SPX. Sized positions still OK. |
| `market_closed` next day | **Skip all new opens.** Holiday-next-day flow is unreliable. |
| (none) | Standard playbook. |

`open-drift` and `premarket` have stricter rules: see those strategy docs.

## Decision rules — market regime gate (NEW, check FIRST)

`account-snapshot` now emits a `market_regime` block:
```
"market_regime": {
    "regime": "TRENDING_UP",          # or RANGEBOUND / STRESSED / CRISIS / UNKNOWN
    "spy_price": 588.12,
    "spy_50dma": 575.40,
    "spy_pct_above_50dma": 2.21,
    "spy_1d_pct": 0.45,
    "spy_5d_pct": 1.30
}
```

Adjust playbook by regime:

| Regime | Posture |
|---|---|
| **TRENDING_UP** | Normal sizing. Lean into PEAD. Hold winners longer. |
| **RANGEBOUND** | Normal sizing. Prefer high-conviction only (composite ≥4). |
| **STRESSED** | **All sizing × 0.5.** Skip composite 2–3 entirely. Prefer fade setups over rides (mean-revert is more reliable when the broader tape is heavy). |
| **CRISIS** | **No new opens.** Only manage existing positions (tighten stops, take profit fast). |
| **UNKNOWN** | Be conservative. Treat as RANGEBOUND. Flag the unknown in your Discord summary. |

## Decision rules — implied-move classifier (NEW, run BEFORE composite signal)

Every reporter in the `fetch-amc-context` output now carries an `implied_move` block:
```
"implied_move": {
    "proxy_pct": 5.4,         # historical mean abs(next-day return) over prior 4 quarters
    "current_move_pct": -2.1, # current AH move relative to previous close
    "ah_move_ratio": 0.39,    # |current_move| / proxy_pct
    "classification": "pead_candidate"
}
```

`classification` is one of:

| Class | Ratio | What it means | Action |
|---|---|---|---|
| `pead_candidate` | < 0.5 | Tape is *underreacting* to the print | Lean in — PEAD tends to extend over hours/days |
| `neutral` | 0.5–1.0 | In-line with history | Normal sizing |
| `partial_take_candidate` | 1.0–1.5 | Tape matches the typical move | Trade only if other signal is strong; consider take-half-off plan |
| `fade_candidate_skip` | > 1.5 | Tape is *overreacting* | **Skip the long.** We don't short; this is a mean-revert setup that doesn't fit. |
| `unknown` | n/a | No history or candle data | Be cautious; fall back to composite alone |

A `fade_candidate_skip` is a hard skip *unless* the composite is ≥6 and headlines clearly justify the move (e.g., a once-in-a-decade triple-beat). Default to skipping.

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

## Sizing — four-tier conviction grading (replaces the old $400/$250 split)

Compute the tier from three inputs that are all in the `fetch-amc-context` + `account-snapshot` output: **composite signal**, **implied-move ratio** (`implied_move.ah_move_ratio`), and **market regime**.

| Tier | Composite | Implied-move ratio | Regime | Notional (base) |
|---|---|---|---|---|
| **S** | ≥ 6 | < 0.7 (under-reaction) | TRENDING_UP | **$450** |
| **A** | 4–5 | < 1.0 | TRENDING_UP or RANGEBOUND | **$350** |
| **B** | 4–5 | 1.0–1.3 | any non-CRISIS | **$200** |
| **C** | 2–3 | < 1.0 | TRENDING_UP only | **$150** |
| **skip** | other combos | — | — | — |

Then apply the regime multiplier:
- TRENDING_UP / RANGEBOUND → × 1.0 (use base)
- STRESSED → **× 0.5** (halve every tier)
- CRISIS → **× 0.0** (no new opens at all)

Shares = `floor(notional / target)`. Target = `quote.current * 0.98` (enter 2% below the tape; don't chase).

### Realized-vol sizing modifier (NEW)

Each reporter now carries `realized_vol_30d_pct` — annualized vol from the last 30 daily candles. Use it to fine-tune the tier:

| Realized vol (annualized) | Modifier |
|---|---|
| < 20% (sleepy: KO, JNJ, PG) | **+1 tier** — a strong beat on a low-vol name is rare and meaningful |
| 20–45% | No modifier |
| 45–70% | No modifier (already volatile baseline) |
| > 70% (PLTR, RIVN, MSTR) | **−1 tier** — today's move could be noise; let the trade prove itself |

Tier downgrade below C → skip. Tier upgrade above S → cap at S.

### Hard stop (NEW — set on every entry)

After `propose` returns successfully, the result includes `recommended_hard_stop`. Write that value into the new Notion Positions row's **`Hard Stop Price`** field. The stop is computed as:

```
stop_pct = max(0.05, implied_move_proxy_pct/100 * 0.75)   # floor 5%, never tighter than 75% of implied
hard_stop = avg_fill_estimate * (1 - stop_pct)
```

You must pass `--implied-move-pct <value>` when calling `propose` so it uses the correct proxy. Otherwise the stop defaults to 5%.

The actual Alpaca stop-limit sell order is **not placed in this phase** (shares haven't filled yet). ah-close at 19:55 ET reads the Notion row and calls `place-stop` once the position is filled.

### Ladder shape (unchanged)

- `down-band` = 0.08, `up-band` = 0.02, `rungs` = 10, `weight-ratio` = 5.0
- `extended-hours` = true, `tif` = day
- Per-ticker overrides in `watchlist.yaml` (TSLA 15%/8%, NVDA 12%/7%) still apply when those tickers trigger.

### Examples

- NVDA strong beat + raised guidance, composite 7, AH +3% vs 6% implied (ratio 0.5), TRENDING_UP → **Tier S, $450 notional**.
- AVGO beat, composite 4, AH +4% vs 5% implied (ratio 0.8), RANGEBOUND → **Tier A, $350**.
- INTC beat-and-fade language, composite 3, AH +2% vs 3.5% implied (ratio 0.57), TRENDING_UP → **Tier C, $150**.
- Same INTC setup but regime STRESSED → **Tier C × 0.5 = $75, but $75 is below our practical minimum — SKIP**.
- Any setup at composite ≥6 BUT ratio > 1.5 → `fade_candidate_skip` from the implied-move filter; SKIP regardless of tier.

### Fade mode (kept for the edge case)

If a tier-S/A setup is the only one tonight AND the tape is +8% already (ratio ≈ 1.3) but you still want exposure: reduce size 50% AND widen down-band to 0.12. Mention "fade mode" in the rationale. Use sparingly — the implied-move filter exists precisely to flag this.

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
