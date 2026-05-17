# Notion Setup (one-time)

The earnings-trader uses Notion as the live shared-state layer between its 6 phase routines. This is a one-time setup. ~15 minutes.

## 1. Create the parent page

In your Notion workspace, create a new page:

- **Title**: `Earnings Trader Control`
- Location: wherever you keep tooling pages

Copy the page URL. The 32-char hex tail is the **parent page ID** — save it.

```
https://www.notion.so/Earnings-Trader-Control-1234abcd5678efgh...
                                                ^^^^^^^^^^^^^^^^
                                                this is the page ID
```

## 2. Create the "Positions" database

Inside the parent page, add a database (inline). Title it **Positions**. Add these fields exactly as named (types matter):

| Field name | Type |
|---|---|
| Symbol | Title (default) |
| Opened By Phase | Select — add options: `post-amc`, `premarket`, `open-drift` |
| Opened Date | Date |
| Composite Score | Number |
| Original Thesis | Text |
| Target Price | Number (format: dollar) |
| Stop Plan | Text |
| Ladder Rungs Filled | Number |
| Avg Entry Price | Number (format: dollar) |
| Current P&L % | Number (format: percent) |
| Last Touched By | Select — add options: `amc-brief`, `post-amc`, `ah-close`, `premarket`, `open-drift`, `daily-recap` |
| Last Touched At | Date (with time) |
| Notes | Text |

Open the database as a full page. The URL gives you the **Positions DB ID** (32-char hex). Save it.

## 3. Create the "Daily Log" database

Same parent page, another inline database titled **Daily Log**:

| Field name | Type |
|---|---|
| Run Title | Title |
| Date | Date |
| Phase | Select — options: `amc-brief`, `post-amc`, `ah-close`, `premarket`, `open-drift`, `daily-recap` |
| Started At | Date (with time) |
| Status | Select — options: `ok`, `partial`, `error` |
| Trades Proposed | Number |
| Trades Approved | Number |
| Trades Filled | Number |
| Realized P&L Today | Number (format: dollar) |
| Summary | Text |
| Errors | Text |

Save the **Daily Log DB ID**.

Optional view tweaks: a board view grouped by Phase, or a calendar view by Date. Not required.

## 4. Create the "Handoffs" page

Inside the parent page, create a sub-page titled **Handoffs**. Add these 5 level-2 headings in order (and leave them empty):

```
## post-amc → ah-close

## ah-close → premarket

## premarket → open-drift

## open-drift → daily-recap

## daily-recap → next-day post-amc
```

Save the **Handoffs page ID**.

## 5. Create the "Observations" page

Inside the parent page, create a sub-page titled **Observations**. Leave it empty (daily-recap will add weekly sections automatically).

Save the **Observations page ID**.

## 6. Capture all five IDs

Put the five IDs somewhere safe (a password manager or a local note). They go into:

- `.env.local` on your Mac (for local CLI use, optional)
- Each /schedule routine prompt (required — that's how the cloud routines find them)

Format:

```
NOTION_PARENT_PAGE_ID=...
NOTION_POSITIONS_DB_ID=...
NOTION_DAILY_LOG_DB_ID=...
NOTION_HANDOFFS_PAGE_ID=...
NOTION_OBSERVATIONS_PAGE_ID=...
```

## 7. Grant Notion MCP access

The Notion connector in claude.ai needs explicit per-page access to read/write. From the **Earnings Trader Control** parent page:

1. Click **Share** in the top-right.
2. Search for the Notion integration / connector your claude.ai uses.
3. Grant it **Edit** access.
4. Notion will inherit access to all sub-pages and databases — you only need to share the parent.

If unsure which integration to share with, run a test in claude.ai/code: ask Claude to "list resources from the Notion MCP" and see what shows up. The integration that returns your workspace is the one to share the parent page with.

## 8. Smoke test

From a local Claude Code session (or the web UI with Notion MCP attached), ask:

> Read the "Positions" database (ID: \<your-id\>) and tell me how many rows it has.

Expected response: `0 rows`. If you get a 404 or permission error, re-check step 7.

---

After setup, you're ready to deploy the 6 /schedule routines. See `plan.md` § "Routine deployment".
