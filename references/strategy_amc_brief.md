# Strategy: AMC Brief (3:50 PM ET)

**Fires:** weekdays at 3:50 PM ET — 10 minutes before market close, just before AMC reporters drop their prints.

**Purpose:** mental readiness, not trading. Surface tonight's earnings universe with prior-reaction context so the post-AMC routine (4:01 PM ET) has signal to work with and you're not surprised by what reports.

**Does NOT propose trades. Does NOT modify positions.** Pure informational.

## Workflow

1. `python scripts/trade.py fetch-earnings-preview --window amc` → JSON of today's filtered AMC reporters with estimates + prior 4 quarters' surprise history. No actuals yet (the prints haven't dropped).

2. For each reporter, note:
   - **Surprise history** (prior 4 quarters): consistent beats, consistent misses, or mixed?
   - **Estimate magnitude**: bigger names (higher EPS estimate) → more market attention → more violent reactions
   - **Industry**: helps you contextualize ("another semi reporting after AMD's miss yesterday" etc.)

3. Rank the universe by "watch priority":
   - High: market cap > $50B AND has consistently beat or missed the last 4 quarters (PEAD-eligible)
   - Medium: market cap $2B–$50B, mixed history
   - Skip-watch: tiny cap or very low expectation moves

4. Post a single concise summary to Discord:

```
📋 **AMC Brief for {date}**
{N} AMC reporters tonight after filtering ({M} filtered out).

Top watches:
• {SYM} — est ${eps_est}, last 4Q: {+X%, +Y%, +Z%, -W%}
• {SYM} — est ${eps_est}, last 4Q: {…}
• {SYM} — est ${eps_est}, last 4Q: {…}

Others on the list: {comma-separated ticker list}
```

5. Exit cleanly. The post-AMC routine takes over at 4:01 PM ET.

## Discipline

- **Don't trade in the AMC brief.** It's just situational awareness.
- **Don't over-rank.** If 25 reporters are on the list, surface the top 5 by attention/PEAD relevance. The post-AMC routine looks at all of them.
- **Don't speculate about direction.** "NVDA could beat" is noise. Just report what's on the calendar and what their history looks like.
- **If 0 reporters pass filters:** Post a one-liner ("ℹ️ no AMC reporters on the filtered universe tonight") and exit. Quiet weeks are fine.

## How this differs from post-amc

| | amc-brief | post-amc |
|---|---|---|
| Fires | 3:50 PM ET | 4:01 PM ET |
| Data state | Expected (estimates only) | Reported (actuals available) |
| Output | Discord summary | Discord trade proposals |
| CLI used | `fetch-earnings-preview` | `fetch-amc-context` + `propose` |
| Modifies positions | No | Yes (with ✅ approval) |

## Configuration

Same `watchlist.yaml` filters apply — `min_market_cap_usd`, `min_avg_volume`, `min_price`, `allowed_countries`, `allowed_exchanges`, `common_stock_only`. Per-ticker overrides aren't relevant here (this phase doesn't trade).
