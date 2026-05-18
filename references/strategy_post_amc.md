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

0. Query the Notion Positions DB via MCP for every open row's Symbol + Opened Date. Then call:
   ```
   python scripts/trade.py force-flatten-stale \
       --position AAPL:YYYY-MM-DD --position NVDA:YYYY-MM-DD ...
   ```
   Any position held longer than 5 trading days is flagged as stale — PEAD has decayed. For each stale symbol, call `propose --side sell` immediately before running the nightly earnings scan.
1. Call `python scripts/trade.py fetch-amc-context` → JSON of **every** US-listed AMC reporter today that passes the universe filter (market cap ≥$1B, avg volume ≥1M, price ≥$5, US common stock — no hand-picked ticker list), with print numbers, current quote, recent headlines, and prior quarters' surprise history. Expect 5–25 names on a busy day, 0 on a quiet one.
2. Call `account-snapshot` → confirm current positions and risk headroom (you can't propose anything if caps are hit).
3. For **each** reporter, analyze and decide: skip, or propose a buy ladder. Follow the decision rules below — they are constraints, not suggestions.
4. For each proposal, call `trade.py propose ...` with a one- to two-sentence `--rationale`. The Discord embed shows your rationale to the user, who taps ✅ or ❌.
5. After all reporters are processed, summarize what you did (skipped X, proposed Y, placed Z) and exit.

## Read the press release (NEW — for any reporter you're seriously considering)

Finnhub headlines + sentiment cover the surface. The press release (8-K Item 2.02) has the substance: full guidance language, segment performance, share count, buyback authorizations, executive commentary.

For each reporter that survives the hard-skip filter:
```
python scripts/trade.py fetch-press-release --symbol X --since YYYY-MM-DD
```

This pulls the latest 8-K from SEC EDGAR. The output includes:
- `guidance_direction` — pre-classified as `"raised"` | `"maintained"` | `"lowered"` | `"withdrawn"` | `"mixed"` | `"unknown"`. Use this directly — do not re-read the text for guidance language.
- `text` — cleaned press release (capped at ~15K chars). Read it for nuance beyond the classifier.

When reading `text`, look for:
- **Segment performance**: a beat driven by one segment (data center for NVDA) vs across-the-board.
- **Share count changes**: large buybacks announced concurrent with earnings are often defensive.
- **Tone shifts**: a CEO who normally talks about "headwinds" but doesn't this quarter is signaling confidence.
- **"Non-GAAP" framing**: heavy non-GAAP adjustments can mask weakness.
- **Guidance range width**: even if `guidance_direction: "raised"`, a very wide range ("$4.80–$5.30") signals uncertainty vs a tight one ("$5.10–$5.15").

If the press release contradicts the headlines (e.g., headlines say "raised guidance" but the actual release lowers FY range), trust the release. Lower composite by 2 points.

Requires both `www.sec.gov` and `data.sec.gov` allowlisted in the cloud environment (`www.sec.gov` for filing index pages; `data.sec.gov` for the submissions JSON used by CIK lookup). If the call returns `found: false`, fall back to Finnhub headlines + sentiment.

## Decision rules — hard skips (never propose)

These are **non-negotiable**. If any of these are true, skip the ticker and do not propose:

- **EPS missed estimate by more than 5%** (eps_surprise_pct < -5)
- **Revenue missed estimate by more than 3%** (when revenue numbers are present)
- **Guidance explicitly lowered** — `fetch-press-release` output has `guidance_direction: "lowered"` or `guidance_direction: "withdrawn"`. If the call returns `found: false`, fall back to headline scan. Do not manually infer from text — the classifier does it.
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
| **RANGEBOUND** | Normal sizing. Prefer high-conviction only (composite ≥4.5, i.e., Tier A+). |
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

A `fade_candidate_skip` is a hard skip *unless* the composite is ≥5.5 and headlines clearly justify the move (e.g., a once-in-a-decade triple-beat). Default to skipping.

## Decision rules — composite signal

For the survivors of the hard skips, build a composite from these inputs:

**Bullish points — graduated scoring:**

EPS surprise (`print.eps_surprise_pct`):
- > 2% and ≤ 7% → **+1**
- > 7% and ≤ 15% → **+2**
- > 15% → **+3**

Revenue surprise (`print.revenue_surprise_pct` — only count when not null):
- > 1% and ≤ 5% → **+1**
- > 5% → **+2**

Other bullish signals (+1 each):
- `guidance_direction: "raised"` from `fetch-press-release`
- `beat_consistency.sandbagging_flag: false AND beat_rate_4q == 1.0` — proven reliable beater (add +0.5, round up to next integer if ≥ X.5)
- CEO/CFO language in headlines/quotes is confident: concrete numbers, raised outlook, references to specific growth drivers
- The current after-hours tape is positive (`quote.pct_change > 0`) AND headlines align AND (`ah_volume_today` is null OR `ah_volume_today` > 200,000 for large/mega-cap, > 50,000 for mid-cap) — thin-volume AH moves are often algo-driven and frequently reverse at open; if volume is suspiciously low, do not count the tape as a bullish point

**Bearish points (-1 each):**

- EPS surprise < -2%
- Revenue surprise < -1%
- `guidance_direction: "maintained"` when EPS beat was weak (≤+2%) — no growth signal
- `beat_consistency.sandbagging_flag: true` — this beat was expected; subtract 1 from EPS score
- Headlines use hedging language: "headwinds", "navigating", "weather", "challenging", "near-term pressure"
- Defensive Q&A characterizations
- Tape is red >2% while you have an otherwise-bullish read (tape disagreement is a red flag)

**Decision (composite = bullish total − bearish total):**

- composite < 3 → **skip**
- composite ≤ -2 → **bearish** — skip (MVP doesn't open shorts)

## Sizing — four-tier conviction grading (replaces the old $400/$250 split)

Compute the tier from three inputs that are all in the `fetch-amc-context` + `account-snapshot` output: **composite signal**, **implied-move ratio** (`implied_move.ah_move_ratio`), and **market regime**.

| Tier | Composite | Implied-move ratio | Regime | Notional (base) |
|---|---|---|---|---|
| **S** | ≥ 5.5 | < 0.7 (under-reaction) | TRENDING_UP | **$450** |
| **A** | 4.0–5.4 | < 1.0 | TRENDING_UP or RANGEBOUND | **$350** |
| **C** | 3.0–3.9 | < 1.0 | TRENDING_UP only | **$150** |
| **skip** | < 3 or other combos | — | — | — |

> **Calibration note (2026-05-18 backtest, 15 tickers × 4 quarters):** The composite formula has a theoretical max of 6.5 (blowout EPS + qual floor + beat bonus). The old S≥8 / A=6-7 thresholds were unreachable. Highest seen: 5.5 (MSFT). Recalibrated to S≥5.5, A=4.0-5.4, C=3.0-3.9 to match actual data distribution.

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

### Pre-earnings drift modifier (NEW)

Each reporter now carries `pre_earnings_5d_drift_pct` — the 5-trading-day return leading into today's print. This measures buy-the-rumor positioning: if the stock already ran hard, the market partially expected a beat, so the PEAD tailwind is weaker.

| 5-day drift | Meaning | Tier adjustment |
|---|---|---|
| > +5% | Strong buy-the-rumor run-up | **−0.5 tier** (round down: A → B; if already B, skip unless composite ≥5) |
| −5% to +5% | Flat / neutral into earnings | No adjustment |
| < −5% | Stock was sold down ahead of print | **+0.5 tier** on a beat (round up: B → A; maximum PEAD surprise effect) |

"−0.5 tier" means: if you're between two tiers, choose the lower one. A clean A (composite 5, ratio 0.8) with +7% drift → drop to B ($200). A clean A with −7% drift and confirmed beat → upgrade to S ($450), capped.

This adjustment stacks with the realized-vol modifier. Apply vol modifier first, then drift modifier.

### Market cap tier and PEAD duration

`metrics.market_cap_usd` is in every reporter. Use it to calibrate how long to hold and where to set GTC targets.

| Market cap | PEAD duration | GTC sell target | Note |
|---|---|---|---|
| > $50B (mega) | Same session or next morning | 1× implied move | Efficient pricing → reversion is fast |
| $10–50B (large) | 1–3 trading days | 1.5× implied move | Standard playbook |
| $2–10B (mid) | 3–5 trading days | 2× implied move | Wider targets, hold longer, watch volume |

**Mega-cap rule:** if target not hit by end of the next regular session, exit regardless. Don't hold AAPL, MSFT, or NVDA hoping for multi-day PEAD drift that large efficient-market names rarely sustain.

**Mega-cap sizing reality check (backtest finding):** Over 4 quarters, mega-caps ($50B+: NVDA, META, MSFT, AAPL, GOOGL, AMZN) average only 1.3% 1-day moves even on strong beats. Mid-caps (PLTR, SNOW, PANW, AMD) average 3.5%+. If a mega-cap composite is A tier, consider sizing it as C ($150) — the expected dollar move is thin relative to the bid/ask cost. Prefer mid-cap A/S setups when available on the same night.

**EPS surprise % unreliability:** For companies with near-zero or recently negative EPS bases (INTC, UBER), the surprise % can be astronomically large (100%–2000%) due to a tiny denominator. Cap composite EPS points at +3 regardless of surprise %, and note manually in the rationale if the surprise % looks like a base-effect artifact. The sandbagging flag (avg_eps_surprise_4q > 8%) will also misfire for these names — override manually.

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

- PLTR blowout beat + 100% beat rate + non-sandbagging, composite 5.5, AH +4% vs 6% implied (ratio 0.67), TRENDING_UP → **Tier S, $450 notional**.
- PANW strong beat + raised guidance, composite 5.0, AH +3% vs 4% implied (ratio 0.75), TRENDING_UP → **Tier A, $350**.
- MSFT weak beat + 100% beat rate, composite 4.5, AH +1% vs 2% implied (ratio 0.5), RANGEBOUND → **Tier A, $350**.
- AMD moderate beat, composite 4.0, AH +2% vs 3.6% implied (ratio 0.56), TRENDING_UP → **Tier A, $350**.
- CRM weak beat sandbagging, composite 3.0, AH +1% vs 2% implied (ratio 0.5), TRENDING_UP → **Tier C, $150**.
- Same setup but regime STRESSED → **Tier C × 0.5 = $75, below practical minimum — SKIP**.
- Any setup at composite ≥5.5 BUT ratio > 1.5 → `fade_candidate_skip` from the implied-move filter; SKIP regardless of tier.

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

## Earnings call timing — size in stages

Most large-cap AMC reporters release their press release at 4:00–4:15 PM ET, but the **earnings call** (Q&A with analysts) runs 4:30–5:30 PM ET. The real guidance language often comes out of the Q&A, not the press release.

**Default rule**: if the press release alone gives you a composite ≥4, enter at **75% of the tier notional** via the post-AMC ladder. Note in the `post-amc → ah-close` handoff that a second entry window is open until 5:30 PM. The `ah-close` routine at 7:55 PM can use the remaining 25% if the call reinforced the thesis.

For the 25% staged entry decision, `ah-close` should run:
```
python scripts/trade.py fetch-call-transcript --symbol X --since TODAY
```
If `found: true`, read the transcript text for Q&A tone before committing the 25%. If `found: false`, proceed based on news headlines and price action as before.

**Why not wait for the full call?** The best AH entries happen in the first 30–60 minutes after the print. Waiting for the entire Q&A means entering at a less favorable price on a confirmed winner, or missing it entirely on a fast mover.

**Skip the staged approach when:**
- Composite is ≥6 and the press release is unambiguous — enter full size immediately.
- The call is known to be short (typically non-tech: retailers, industrials, utilities). Short calls mean guidance comes with the release, not Q&A.
- `ah_move_ratio` is already > 0.8 — position is filling close to the implied move, staged entry adds limited value.

## Common pitfalls

- **Don't propose just because a print beat.** A 3.6% beat with no guidance signal isn't enough — that's a routine "in-line" print. Skip it.
- **Don't trust the tape alone.** If AH price is +5% but every headline is bearish, the tape is wrong and will reverse overnight. Read the news.
- **Don't chase.** Target should be *below* the current tape (2% by default). If you enter at the tape and it dips, you're underwater immediately.
- **Watch for "double earnings" days.** If 4+ companies report at 4:00 PM ET, you cannot propose all of them — you only have 3 position slots. Rank by signal strength, propose the top 1-2.
- **Sector crowding warning.** The fetch output includes `sector_crowding` — a dict of industries with 2+ reporters tonight (e.g., `{"Semiconductors": ["NVDA", "AMD"]}`). If an industry is crowded, pick only the highest-composite name in that sector for tonight. Two positions in the same sector on the same night means your sizing is effectively doubled on that thesis — sector-specific bad news (tariff announcement, analyst note) hits both simultaneously.
- **Use `suggested_ladder` bands from fetch-amc-context.** High-vol names need wider down-bands to fill; low-vol names don't need the full 8% width. The `suggested_ladder.down_band` / `suggested_ladder.up_band` values are pre-computed from `realized_vol_30d_pct` — pass them directly to `--down-band` / `--up-band`.
- **Late filer warning.** If `fetch-press-release` returns `filed_today: true` AND you're reading the press release at 5:00+ PM ET, the earnings call is likely already live. Do not use the staged 75%/25% entry — enter full size now or skip. Reserving 25% for ah-close makes no sense when Q&A is already underway by the time you're reading the release.

## When to skip the whole phase

If `fetch-amc-context` returns zero reporters, exit cleanly. Zero is fine — it means no US company reporting after-market today passed the liquidity/size filter. Don't try to relax the filter on the fly to find trades; the filter exists to keep us out of illiquid AH names. A quiet day is a quiet day.
