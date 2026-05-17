# Notion State Layer

The earnings-trader uses Notion as the shared-state layer across the 6 phase routines. This doc is your reference for which page to read/write, what fields exist, and how to query.

The routine prompt passes you these IDs as env vars (or inline). If any are missing, log an error to Discord and skip the Notion step — never invent IDs.

```
NOTION_PARENT_PAGE_ID      "Earnings Trader Control" page
NOTION_POSITIONS_DB_ID     Positions database
NOTION_DAILY_LOG_DB_ID     Daily Log database
NOTION_HANDOFFS_PAGE_ID    Handoffs page (5 level-2 sections)
NOTION_OBSERVATIONS_PAGE_ID Observations page
```

## Stale-position rule (every phase)

Before any per-phase work, query the Positions DB for rows with `Opened Date` more than **5 trading days** before today (ET). These are stale earnings trades — the PEAD effect has decayed; what was a thesis-driven trade has become a directional bet you never planned. Force-flatten via:

```
python scripts/trade.py propose --symbol X --side sell --rationale "stale earnings trade (>5 days)" \
    --target <current_price> --shares <position_qty> --down-band 0.03 --up-band 0.05 --rungs 5 \
    --phase <this-phase>
```

Even AH-only phases (ah-close) do this; the sell ladder may not fill until next session, but the order is in.

Computing "5 trading days ago":
- Monday → previous Monday (or earlier if there's a holiday)
- Tuesday–Friday → 7 calendar days ago is a safe approximation (it includes the prior weekend, so 5 trading days ≈ 7 calendar days). If precise: count back 5 weekdays.

After flattening, archive the Notion row.

## When to read / write per phase

| Phase | Read | Write |
|---|---|---|
| amc-brief | Positions (context), Observations | Daily Log row |
| post-amc | Positions, Observations | Positions (new opens), Daily Log row, Handoffs[post-amc→ah-close] |
| daily-recap | Daily Log (today), Positions | Daily Log (own row), Observations (if pattern), Handoffs[daily-recap→next-day post-amc] |
| ah-close | Positions, Handoffs[post-amc→ah-close] | Positions (P&L %, closes), Daily Log row, Handoffs[ah-close→premarket]; clear inbound section |
| premarket | Positions, Handoffs[ah-close→premarket], Observations | Positions (overnight Δ), Daily Log row, Handoffs[premarket→open-drift] |
| open-drift | Positions, Handoffs[premarket→open-drift] | Positions (open-bell action), Daily Log row, Handoffs[open-drift→daily-recap] |

## Positions database

One row per currently-open Alpaca position. Prune (delete or archive) when a position closes to flat.

| Field | Type | Source |
|---|---|---|
| Symbol | Title | Alpaca position.symbol |
| Opened By Phase | Select | post-amc / premarket / open-drift |
| Opened Date | Date | YYYY-MM-DD (America/New_York) |
| Composite Score | Number | from your decision tree (e.g. +3) |
| Original Thesis | Text | ≤200 chars, your one-sentence rationale |
| Target Price | Number | original ladder target |
| Stop Plan | Text | e.g. "GTC stop -8%" or "fade if gap > implied" |
| Hard Stop Price | Number (dollar) | the computed stop level; downstream phases enforce |
| Implied Move Proxy % | Number | historical mean abs(next-day return) from prior 4 prints |
| Ladder Rungs Filled | Number | count of filled rungs out of total |
| Avg Entry Price | Number | Alpaca position.avg_entry_price |
| Current P&L % | Number | refreshed each phase (signed, e.g. -3.2) |
| Last Touched By | Select | which phase last updated this row |
| Last Touched At | Date | timestamp in UTC |
| Notes | Text | optional running commentary |

**Upsert pattern**: query by Symbol; if a row exists, update fields; otherwise create. Never duplicate by symbol.

**Close pattern**: when Alpaca shows the position is flat, archive (or delete) the row — keep the database lean.

## Daily Log database

Append-only. One row per phase run, **regardless of whether the phase traded**. This is the audit trail.

| Field | Type | Source |
|---|---|---|
| Run Title | Title | `YYYY-MM-DD phase-name` e.g. `2026-05-19 post-amc` |
| Date | Date | trading date (ET) |
| Phase | Select | amc-brief / post-amc / ah-close / premarket / open-drift / daily-recap |
| Started At | Date | timestamp UTC |
| Status | Select | ok / partial / error |
| Trades Proposed | Number | how many approval messages you sent |
| Trades Approved | Number | ✅ reactions received |
| Trades Filled | Number | rungs that actually filled |
| Realized P&L Today | Number | only daily-recap sets this |
| Summary | Text | same blob you posted to Discord |
| Errors | Text | stack traces / "data unavailable" / empty |

## Handoffs page

A single page with 5 level-2 headings. Each section: short bullet list (≤5 items, ≤300 chars each).

```
## post-amc → ah-close
## ah-close → premarket
## premarket → open-drift
## open-drift → daily-recap
## daily-recap → next-day post-amc
```

**Write pattern**: replace the contents of your outbound section entirely each run. Don't append (these are ephemeral).

**Read pattern**: read your inbound section at start. After acting on it, **clear that section** (write empty content under the heading) so the next cycle starts clean.

**The `daily-recap → next-day post-amc` section** is the exception — it's longer-lived (one section per trading day). Post-amc reads it the next afternoon, then clears it.

Example post-amc → ah-close note:
```
- Watch META for guidance Q&A clip ~6:30 PM — Zuck flagged efficiency on call
- AMD opened ladder at $145 (composite +3, raised data-center guide)
- If TSLA prints late (>5pm ET), flag — calendar shows after-close but >1h slip possible
```

## Observations page

Append-only diary of strategy-tuning patterns. Daily-recap is the primary writer.

Structure: one section per ISO week (heading `## Week of YYYY-MM-DD`, Monday of that week).

Daily-recap appends short bullets under the current week's section when it sees something worth flagging:

```
## Week of 2026-05-12

- 2026-05-13: faded on raised-guidance signals — 3/5 winners gave back gains by next morning. Tighten composite floor from +2 to +3?
- 2026-05-15: ladder fill rate 22% on small-caps. Bands probably too wide for <$10B mcap.
```

amc-brief and post-amc **read** this for context but never write.

## Notion MCP tool quick reference

The Notion MCP exposes (names vary slightly by version — call `notion-search` to list resources if unsure):

- `notion-fetch` — get a page or database by ID
- `notion-search` — find pages by query
- `notion-create-pages` — create a page (in a parent page or database)
- `notion-update-page` — modify a page's properties or content
- `notion-create-database` — create a new DB (one-time setup only)
- `notion-update-data-source` — modify DB schema (one-time setup only)

For database row operations: use `notion-create-pages` with the database as parent. Each row is a page whose properties are the DB fields.

For querying a database: use `notion-fetch` on the DB ID — it returns rows, optionally filtered.

## Failure modes

- **Notion API rate limit (429)**: retry once after 2 sec, then proceed without Notion writes; mark Daily Log row Status=partial in next phase.
- **Page ID not found**: log to Discord ("Notion state unreachable — running in degraded mode"), do not block trading.
- **Schema mismatch (missing field)**: log specific field name to Discord, continue with the fields you can write.

**Never block a trade decision on a Notion failure.** The trading layer is Alpaca + Discord; Notion is observability.
