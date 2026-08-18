# Trading Desk

A paper-trading equity research desk for a **Paperclip**-orchestrated team of AI
analysts, with market data from the **OpenBB Platform**.

Agents research US equities on a swing horizon, record dated and falsifiable
predictions, and trade a simulated book. Every call is graded automatically
against real prices once its horizon elapses, and each agent carries a running
scorecard. Output lands as Markdown artifacts in Paperclip's dashboard, and as a
local operator dashboard (`tradingdesk dashboard`).

**No order placed here reaches a real market.** This is a research and
evaluation harness, not investment advice.

---

## How the three pieces fit

```
┌──────────────────────────────────────────────────────────────┐
│  Paperclip            org chart · budgets · heartbeats ·      │
│  (Node + Postgres)    approvals · tool access governance      │
└───────────────┬──────────────────────────────────────────────┘
                │ MCP (remote_http), governed per agent
    ┌───────────┴────────────┐
    ▼                        ▼
┌───────────────┐   ┌──────────────────────┐
│ OpenBB MCP    │   │ trading-desk MCP     │  ◀── this repo
│ fundamentals, │   │ predictions, grading, │
│ estimates,    │   │ paper book, risk      │
│ news, macro   │   │ limits, reports       │
└───────────────┘   └──────────┬───────────┘
                               │ OpenBB Python API
                               ▼
                        price history, indicators
```

Paperclip is the control plane and never sees market data. OpenBB is the data
layer and holds no state. This repo is the part that had to be built: the
domain logic that turns "an LLM said NVDA looks good" into a scored, auditable
track record.

**Why a second MCP server rather than giving agents OpenBB alone?** Because the
things that make a prediction desk work — a stamped entry price, a horizon that
cannot be quietly revised, position limits enforced in code, a Brier score
nobody can edit — are exactly the things an agent must not be able to talk its
way around. They live behind a tool boundary, not in a prompt.

---

## Quickstart

Requires **Python 3.9.21–3.12** (OpenBB does not support 3.13+).

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cp .env.example .env            # optional — every default works as-is
```

Prove it works end to end. This backfills dated predictions from real history
and grades them against genuine subsequent prices:

```bash
tradingdesk demo --days-ago 45 --horizon 10
tradingdesk score
tradingdesk brief --stdout
```

Drive the book by hand:

```bash
tradingdesk snapshot NVDA
tradingdesk predict NVDA up --confidence 0.62 --horizon 10 --rationale "..." --agent me
tradingdesk trade buy MSFT 40 --agent me --note "pred #7"
tradingdesk portfolio
tradingdesk resolve            # grade anything whose horizon has elapsed
tradingdesk replay             # backtest the resolved calls
```

Serve it to agents, and look at it yourself:

```bash
tradingdesk serve               # MCP, streamable HTTP on 127.0.0.1:8010/mcp
tradingdesk dashboard           # operator web UI on 127.0.0.1:8020
```

Run the tests (offline — no network, no API keys):

```bash
pytest
```

---

## The desk in one page

**Predictions** carry a ticker, direction (`up` / `down` / `flat`), a confidence
between 0 and 1, a horizon in *trading sessions*, and a written rationale. The
entry price is stamped from the latest close at the moment of recording, so it
cannot be backdated.

**Grading** is automatic. Horizons count real sessions, and resolution indexes
into the actual bar series — if the bar does not exist yet, the call simply
stays open. There is no market-holiday calendar to drift out of date. A `flat`
call means "moves less than ±2%", which also makes a directional call that lands
inside that band a **miss**: predicting UP and getting +1.2% is not a win.

**Scoring** reports three numbers:

| Metric | What it means |
| --- | --- |
| **Hit rate** | Fraction of calls graded correct. Easy to game by only calling the obvious. |
| **Edge per call** | Mean return *in the direction called*. The number that actually matters. |
| **Brier score** | Calibration. Lower is better; 0.25 is what you get by saying 50% to everything. |

A calibration table breaks accuracy down by stated-confidence band, which is
what catches an agent that says 80% and hits 55%.

**The paper book** fills market orders at the latest close, with slippage always
working against the desk and a per-share commission with a floor. It is long
only. Risk limits — position size, gross exposure, cash floor, daily order count
— are enforced in `paper.py` and rejections come back to the agent with the
binding constraint named, so it can resize rather than guess.

---

## MCP tools

23 tools, grouped by prefix so Paperclip's access profiles can grant whole
capability bands.

| Prefix | Tools | Granted to |
| --- | --- | --- |
| `research_*` | watchlist, snapshot, screen, price history | analysts, PM, trader (read) |
| `predict_*` | record, list, resolve, scorecard, leaderboard, replay | analysts, PM |
| `book_*` | portfolio, performance, sizing, submit order, close, mark, trade log | **trader only** |
| `desk_*` | publish report, publish daily brief, list reports, status | all |

`research_snapshot` returns pre-computed technicals — SMA 20/50/200, Wilder RSI
and ATR, annualised volatility, trailing returns, 52-week range position. An LLM
reasoning about RSI is useful; an LLM computing RSI from raw bars is a liability.

---

## The dashboard

```bash
tradingdesk dashboard --port 8020      # http://127.0.0.1:8020
```

A **read-only** operator view — nothing on this page can move the book. Trading
stays behind the MCP tools where the risk gates are, so the dashboard cannot
become a way to bypass them.

It shows equity as a hero figure with a KPI row, the equity curve against the
starting balance, analyst **edge per call** as a diverging bar chart, a
**calibration dumbbell** of stated confidence versus actual hit rate per band,
and tables for positions, predictions (filterable), fills, and published
artifacts. Clicking an artifact renders it inline.

The page is a single self-contained HTML file — no CDN, no external requests, so
it works offline. It follows the OS light/dark setting, with a toggle and a
deep-linkable `?theme=light` / `?theme=dark`.

On color: profit and loss are encoded with the validated **blue↔red** diverging
pair rather than the conventional green/red, which sits in the colorblind-unsafe
band for deuteranopia and protanopia — the exact readers a P&L screen must not
fail. Direction is always carried by an arrow and an explicit sign as well as
hue, so color is never the only cue.

## The agent org chart

Role briefs live in [paperclip/agents/](paperclip/agents/) — paste them in when
hiring each agent in Paperclip.

| Agent | Heartbeat | Can do |
| --- | --- | --- |
| [Head of Research](paperclip/agents/head-of-research.md) | daily, post-close | Reviews calls, instructs the trader, publishes the brief |
| [Technical Analyst](paperclip/agents/technical-analyst.md) | daily, post-close | Screens for swing setups, records calls |
| [Fundamental Analyst](paperclip/agents/fundamental-analyst.md) | twice weekly + earnings | Fundamentals and catalysts via OpenBB MCP |
| [Risk Manager](paperclip/agents/risk-manager.md) | twice daily | Read-only oversight; reports to the operator, not the PM |
| [Trader](paperclip/agents/trader.md) | daily, at the close | The **only** agent that can move the book |

The separation is deliberate. Analysts cannot trade, so a bad call costs
credibility before it costs money. The Risk Manager reports outside the research
chain, so its veto is not the PM's to overrule. The Trader must cite a
prediction id on every fill, which makes an unattributed trade detectable.

---

## Wiring into Paperclip

Verified end to end against **Paperclip 2026.722.0** on Windows.

### Installing Paperclip

The one-line installer (`curl -fsSL https://paperclip.ing/install.sh | bash`)
**refuses Windows** — it gates on `uname` returning Darwin or Linux. It is only a
bootstrapper for an npm package, so install that directly instead:

```bash
npm install -g pnpm paperclipai
paperclipai onboard -y      # quickstart: embedded Postgres, loopback, port 3100
```

That brings up an embedded PostgreSQL and the server on `http://127.0.0.1:3100`.
On loopback in `local_trusted` mode the REST API needs no token.

### Registering the desk

```bash
tradingdesk serve                                          # must be running first
python scripts/register_paperclip.py                       # dry run
PAPERCLIP_COMPANY_ID=<id> python scripts/register_paperclip.py --apply
```

The script runs Paperclip's own connect-wizard flow — `apps/connect` (which
pings the server, creates the application and connection, and imports the tool
catalog in one call), then `apps/{id}/finish`, then the four access profiles.

### Three things that will bite you

**1. The MCP server must be stateless.** Paperclip's catalog refresh sends a bare
`tools/list` with no `initialize` handshake. A stateful FastMCP server rejects
that with `Bad Request: Missing session ID`, and Paperclip surfaces it only as a
generic *"Remote app returned an error"*. `tradingdesk serve` runs stateless by
default; `--stateful` restores handshake-required mode.

**2. Tool annotations decide your risk classification.** Paperclip reads MCP's
`readOnlyHint` / `destructiveHint` to sort tools into read / write / destructive,
and everything defaults to **read** when unannotated. Before the desk set them,
`book_submit_order` and `book_close_position` were catalogued as read-only —
an "allow all reads" profile would have granted order placement. The desk now
annotates the trading tools `destructive`, so they land in ask-first approval.

**3. The prose docs disagree with the API.** As built:

| Documented | Actual |
|---|---|
| transport `remote_http` | `mcp_remote` (enum: `mcp_remote`, `rest_api`, `local_stdio`) |
| entry `selectorValue` | typed per selector: `toolName`, or `riskLevel` |
| runtime ops under the company | `/api/tool-connections/{id}/…` — not company-nested |

### OpenBB's own MCP server

To give the Fundamental Analyst raw OpenBB access too, run it alongside and
register it the same way:

```bash
openbb-mcp --host 127.0.0.1 --port 8001
```

---

## What this does not do

Worth being explicit, because a prediction desk invites more confidence than it
has earned:

- **No live trading.** No broker integration exists, by design.
- **No shorting, no options, no leverage.** Long-only cash equities.
- **Fills are modelled at the close**, not intraday. Slippage is a flat bps
  assumption, not a market-impact model.
- **`replay` is a per-call model, not a portfolio simulation.** Each resolved
  call is treated as one independent fixed-size position and chained in
  resolution order. Real calls overlap in time, so the compounded curve
  overstates what a single book could have held. Use it to compare agents
  against each other and against the SPY baseline — not to project returns.
- **`correct` and `pnl` can disagree.** A call graded a miss inside the ±2% flat
  band still earned a small positive return in the replay. That divergence is
  intentional: one measures forecasting skill, the other measures money.
- **Default data is yfinance** — free, unofficial, and occasionally wrong about
  splits and adjustments. Set `TRADINGDESK_PROVIDER` and the matching
  `OPENBB_*_API_KEY` for a licensed feed before drawing conclusions.
- **Agent skill is unproven.** The harness measures whether an agent has edge.
  It does not supply any. Expect the honest answer to be "no" until a scorecard
  over a meaningful number of resolved calls says otherwise.

---

## Layout

```
src/tradingdesk/
  config.py      settings and risk limits from the environment
  domain.py      Prediction, Fill, Position — grading and average-cost accounting
  store.py       SQLite; positions are replayed from fills, never cached
  data.py        OpenBB adapter, bar handling, indicators
  paper.py       the simulated broker and every risk gate
  scoring.py     resolution, hit rate, edge, Brier, calibration
  backtest.py    replay of resolved calls against baselines
  reports.py     Markdown artifacts for the Paperclip dashboard
  mcp_server.py  23 MCP tools over streamable HTTP
  web.py         read-only dashboard API
  static/        the dashboard page, self-contained
  cli.py         operator CLI — everything the agents can do
paperclip/agents/  role briefs for each hire
scripts/           Paperclip registration
tests/             30 offline tests
```

Licence note: OpenBB is AGPLv3; Paperclip is MIT.
