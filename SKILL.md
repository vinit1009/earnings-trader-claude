---
name: earnings-trader
description: >
  Personal earnings-trading copilot. Use when the user wants to trade stocks around
  earnings releases (pre-market, after-hours, post-print drift), wants laddered limit
  orders to optimize fill prices, or asks to "trade earnings", "buy AAPL with a ladder",
  "scan tonight's earnings", "approve trade in discord", or any phase-based earnings
  workflow (amc-brief, post-amc, premarket, open-drift, ah-close, daily-recap).
  Runs as scheduled Anthropic /schedule routines and posts orders to Discord for
  human approval before execution.
version: 0.1.0
triggers:
  - trade earnings
  - earnings trader
  - run post-amc
  - scan tonight's earnings
  - ladder order
  - approve in discord
  - daily recap
  - pre-market scan
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
---

# Earnings Trader

A disciplined earnings-trading desk. Five scheduled phases, laddered limit orders, Discord approval, hard risk caps. The agent's job is **execution quality**, not prediction.

## Architecture

**Claude is the brain. `trade.py` is the body.** A /schedule routine fires at the right time, which opens Claude Code with a prompt pointing at the relevant `references/strategy_<phase>.md`. Claude reads the strategy doc, calls `trade.py` for data + ladder/order primitives, applies judgment, and proposes trades for Discord approval. No separate API key needed — Claude *is* the analyst.

## CLI primitives (`scripts/trade.py`)

| Subcommand | What it does |
|---|---|
| `fetch-amc-context [--symbol X]` | JSON: today's AMC reporters + print numbers + quote + headlines + prior 4 quarters |
| `account-snapshot` | JSON: cash, equity, open positions, today's realized P&L, risk headroom |
| `list-orders` | JSON: currently-open broker orders |
| `propose --symbol X --target P --shares N --rationale "..."` | Build ladder → risk-check → Discord ✅/❌ → place |
| `cancel --order-id ID` / `cancel-all` | Order hygiene |

## Phases (scheduled routines)

| Phase | When (ET) | Strategy doc |
|---|---|---|
| `amc-brief` | 3:50 PM | references/strategy_amc_brief.md (TBD) |
| `post-amc` | 4:01 PM | references/strategy_post_amc.md |
| `ah-close` | 7:55 PM | references/strategy_ah_close.md (TBD) |
| `premarket` | 7:00 AM | references/strategy_premarket.md (TBD) |
| `open-drift` | 9:25 AM | references/strategy_open_drift.md (TBD) |
| `daily-recap` | 4:05 PM | references/strategy_daily_recap.md (TBD) |

## Hard rules (do not relax)

1. **Every order placement goes through Discord approval.** No exceptions. The agent never auto-executes a trade.
2. **risk.py caps are inviolable.** Per-position $500, max 3 concurrent, daily loss $200.
3. **Extended-hours orders are limit-only.** No market, no stop, no stop-limit outside RTH.
4. **On any error, post to Discord and exit.** Never auto-retry trade actions.

## Workflow when this skill is invoked

The scheduled routine opens Claude Code with a prompt like *"It's 4:01 PM ET. Run the post-AMC workflow per `references/strategy_post_amc.md`."* From there Claude:

1. Reads the strategy doc.
2. Calls `trade.py fetch-amc-context` and `account-snapshot` for data.
3. For each reporter, applies the strategy's decision rules and decides skip vs propose.
4. Calls `trade.py propose ...` with `--rationale` for each proposal.
5. The CLI handles risk-check + Discord approval + order placement deterministically.
6. Reports a one-line summary at the end.

## Files

- `scripts/trade.py` — CLI primitives (`fetch-amc-context`, `account-snapshot`, `list-orders`, `propose`, `cancel`, `cancel-all`)
- `scripts/broker.py` — abstract broker interface
- `scripts/broker_alpaca.py` — Alpaca implementation
- `scripts/broker_robinhood.py` — robin_stocks implementation (week 2+)
- `scripts/ladder.py` — laddered limit orders
- `scripts/risk.py` — hard risk caps
- `scripts/state.py` — SQLite ledger
- `scripts/earnings.py` — Finnhub earnings calendar + news + quote
- `scripts/discord_bot.py` — Discord approval bot

## References

- `references/strategy_post_amc.md` — **decision tree for after-hours reaction (live)**
- `references/strategy_premarket.md` — overnight + pre-market continuation/fade (TBD)
- `references/strategy_open_drift.md` — gap fade vs PEAD ride (TBD)
- `references/ladder_math.md` — ladder pricing distributions (TBD)
- `references/alpaca_extended_hours.md` — TIF flags, allowed order types (TBD)

## Setup

```bash
cd /Users/vinit/Documents/work/robinhood
cp /Users/vinit/.claude/skills/earnings-trader/.env.example .env
# Fill in ALPACA_KEY_ID, ALPACA_SECRET, FINNHUB_KEY, DISCORD_*
pip install -e /Users/vinit/.claude/skills/earnings-trader
pytest /Users/vinit/.claude/skills/earnings-trader/scripts
```

Then schedule the routines (see `references/scheduling.md`).
