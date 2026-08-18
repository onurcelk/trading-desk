# Paperclip wiring

Everything needed to hire the desk as a team of agents inside Paperclip.

Verified against **Paperclip 2026.722.0**.

## Order of operations

1. **Install Paperclip** (separate from this repo — Node 20+).

   The `install.sh` one-liner refuses Windows (it gates on `uname`), but it only
   bootstraps an npm package, so install that directly on any platform:

   ```bash
   npm install -g pnpm paperclipai
   paperclipai onboard -y
   ```

   Quickstart defaults give you embedded PostgreSQL, loopback binding, and the
   server on `http://127.0.0.1:3100`. `paperclipai doctor` checks the setup; on
   Windows expect one warning about `chmod` on the secrets key file, which is a
   POSIX permission check that does not apply.

2. **Start the desk MCP server** so Paperclip has something to connect to:

   ```bash
   tradingdesk serve            # http://127.0.0.1:8010/mcp
   ```

   Leave it **stateless** (the default). Paperclip's catalog refresh issues a
   bare `tools/list` with no `initialize` handshake; a stateful server answers
   `Missing session ID` and Paperclip reports only *"Remote app returned an
   error"*, which is a miserable thing to debug.

3. **Register the connection and access profiles**:

   ```bash
   python scripts/register_paperclip.py            # dry run first
   PAPERCLIP_COMPANY_ID=<id> python scripts/register_paperclip.py --apply
   ```

   Get the company id from `GET /api/companies`, or create one with
   `POST /api/companies {"name": "...", "shortName": "..."}`.

4. **Hire the agents.** Create one agent per file in [agents/](agents/) and paste
   the brief as its role instructions. Set the heartbeat named at the top of each
   brief.

5. **Bind the profiles.** In the Paperclip UI, Tools → Profiles → Bind, attach
   each profile to its agent at the `agent` scope:

   | Profile | Agent |
   | --- | --- |
   | `desk.pm` | Head of Research |
   | `desk.analyst` | Technical Analyst, Fundamental Analyst |
   | `desk.trader` | Trader |
   | `desk.risk` | Risk Manager |

6. **Enable the connection** once its health check passes. Paperclip creates
   connections disabled on purpose.

## How risk levels are decided

Paperclip sorts every discovered tool into read / write / destructive by reading
MCP's `readOnlyHint` and `destructiveHint` annotations — and treats anything
unannotated as **read**. The desk sets them explicitly in `mcp_server.py`:

| Level | Tools |
|---|---|
| read (14) | all `research_*` lookups, `predict_list/scorecard/leaderboard/replay`, `book_portfolio/performance/position_sizing/trade_log`, `desk_status/list_reports` |
| write (6) | `research_add_to_watchlist`, `predict_record`, `predict_resolve_due`, `book_mark_to_market`, `desk_publish_report`, `desk_publish_daily_brief` |
| **destructive (3)** | `book_submit_order`, `book_close_position`, `research_remove_from_watchlist` |

Trading is marked destructive deliberately. A fill cannot be un-filled — only
offset by another trade — and the classification is what puts orders behind
Paperclip's approval gate instead of letting them run unattended.

If you add a tool and forget its annotation, it will be catalogued as read-only
and swept up by any read-level grant. Annotate first.

## Recommended governance

Paperclip separates *visibility* (profiles: can this agent see the tool) from
*runtime decisions* (policies: is this exact call allowed right now). Profiles
above handle visibility. Worth adding as policies:

- **`require_approval` on `book_submit_order`** for the first few weeks. Every
  trade opens an action request you approve by hand, which is the cheapest way
  to find out whether the desk's judgement is worth trusting unattended. Promote
  to a trust rule once you have seen enough.
- **`rate_limit` on `research_screen`** — it fans out one provider call per
  ticker and an agent in a loop will hammer the data source.
- **`block` on `book_*` for every agent except the Trader**, as a backstop
  behind the profile grants. Deny beats allow in Paperclip's policy engine, so
  this holds even if a profile is later misconfigured.

## Budgets

Set a per-agent monthly token budget in Paperclip. The Technical Analyst and
Trader are cheap — they call tools and write short notes. The Fundamental
Analyst is the expensive one: it pulls filings and news through OpenBB and
reasons over long documents. Budget it several times the others, and expect the
Risk Manager to be the cheapest of all.

## A note on trust

The desk's risk limits are enforced in `paper.py`, below the tool boundary, so
they hold regardless of what any agent is prompted to believe. Paperclip's
policies are a *second* layer on top of that, governing who may call what and
when. Neither layer makes the agents' judgement good — they only make its
consequences bounded and its record honest.
