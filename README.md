# earnings-trader

Personal earnings-trading copilot. Reads the day's earnings prints, asks Claude (the analyst) to score each, posts the best-signaled trades to Discord for ✅/❌ approval, then places laddered limit orders on Alpaca.

**Architecture: Claude is the brain, `trade.py` is the body.** A scheduled Claude Code session (via Anthropic /schedule routine) opens at 4:01 PM ET on weekdays, reads the relevant strategy doc in `references/`, and calls `trade.py` CLI primitives to gather data, propose trades, and place orders. No separate LLM API key needed — Claude itself is the model.

## Status

- ✅ Post-AMC strategy (4:01 PM ET)
- ⬜ AMC brief (3:50 PM)
- ⬜ Pre-market (7:00 AM)
- ⬜ Open + drift (9:25 AM)
- ⬜ AH close (7:55 PM)
- ⬜ Daily recap (4:05 PM)

## Workflow (post-AMC)

```
4:01 PM ET (Mon–Fri)
   │
   ▼
/schedule routine spawns a Claude session inside this repo
   │
   ▼
Claude reads references/strategy_post_amc.md
   │
   ├─▶  python scripts/trade.py fetch-amc-context
   │      → JSON: today's AMC reporters filtered by liquidity (≥$2B cap, ≥1M vol, ≥$5)
   │              with print numbers + AH quote + headlines + prior 4 quarters
   │
   ├─▶  python scripts/trade.py account-snapshot
   │      → JSON: Alpaca cash/equity/positions + risk headroom
   │
   │   (Claude analyzes each reporter, applies hard skips + composite signal)
   │
   ├─▶  python scripts/trade.py propose --symbol X --target Y --shares Z \
   │                                    --rationale "..." --extended-hours
   │      → builds 10-rung ladder
   │      → risk-checks (per-position $500, max 3 concurrent, daily loss $200, ±15% price)
   │      → posts Discord embed with rationale, ✅/❌ reactions
   │      → on ✅, places ladder on Alpaca
   │
   └─▶  one-line summary to Discord
```

## CLI

```
python scripts/trade.py <subcommand>

  fetch-amc-context [--symbol X] [--for-date YYYY-MM-DD]
      Universe pull (or test-mode for specific tickers / past dates).
      Returns JSON with print + quote + headlines + prior 4 quarters per reporter.

  account-snapshot
      Alpaca cash, equity, positions, today's P&L, risk headroom.

  list-orders
      Currently-open Alpaca orders.

  propose --symbol X --target P --shares N --rationale "..."
          [--down-band 0.10] [--up-band 0.05] [--rungs 10]
          [--side buy|sell] [--extended-hours] [--tif day]
          [--timeout-s 90] [--dry-run] [--phase post-amc]
      Build ladder → risk-check → Discord ✅/❌ → place. JSON result.

  cancel --order-id ID
  cancel-all
```

## Risk caps (hard-coded in `scripts/risk.py`)

- Max per-ticker exposure: **$500**
- Max concurrent positions: **3**
- Max daily P&L drawdown: **−$200** (blocks new buys; exits still allowed)
- Max price deviation from current quote: **±15%** (typo guard)

These query Alpaca on every call — no local state required.

## Universe filter (`watchlist.yaml`)

- Min market cap: $2B
- Min 10-day avg volume: 1M shares
- Min price: $5
- US exchanges, common stock only

Per-ticker overrides for ladder shape (TSLA / NVDA / AMD / META / NFLX get wider bands).

## Environment variables required

```
BROKER=alpaca
ALPACA_KEY_ID=...
ALPACA_SECRET=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # or https://api.alpaca.markets

FINNHUB_KEY=...

DISCORD_TOKEN=...
DISCORD_USER_ID=...        # only this user's reactions count
DISCORD_CHANNEL_ID=...
```

For local dev, drop these in `.env.local` at the repo root.
For cloud, they go in the /schedule routine's prompt (private to your Anthropic account).

## Local dev

```bash
pip install -e .
pytest scripts
python scripts/trade.py account-snapshot
python scripts/trade.py fetch-amc-context --for-date 2026-04-29  # known busy day
python scripts/trade.py propose --symbol AAPL --target 300 --shares 1 \
    --rationale "smoke test" --dry-run
```

## Cloud deployment

A scheduled Anthropic routine clones this repo and runs Claude with a prompt like:

> It's 4:01 PM ET. Run the post-AMC earnings workflow.
>
> 1. Read `references/strategy_post_amc.md` (the strategy doc).
> 2. Call `python scripts/trade.py fetch-amc-context` and `account-snapshot`.
> 3. For each reporter, apply hard skips, build the composite signal, and decide.
> 4. For each survivor, call `python scripts/trade.py propose ...` with a rationale.
> 5. Summarize at the end.
>
> Credentials (export before running):
> ALPACA_KEY_ID=...
> ...

## Strategy docs

- [Post-AMC reaction](references/strategy_post_amc.md) — 4:01 PM ET workflow

## What this is NOT

- Not a magic prediction engine. The edge is execution quality + discipline + risk caps, not crystal balls.
- Not a high-frequency system. We trade prints, not micro-moves.
- Not for shorting (MVP is long-only). Bearish prints → skip, not short.
- Not for Robinhood (yet). RH support deferred — credentials too sensitive for cloud routine prompts.
