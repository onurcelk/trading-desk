"""MCP server exposing the desk to Paperclip-orchestrated agents.

Runs on streamable HTTP, which is the transport Paperclip prefers for tool
Connections (`remote_http`), so it can be registered once and then granted to
individual agents through Paperclip's tool profiles and policies.

Tool naming is grouped by prefix (`research_`, `predict_`, `book_`, `desk_`)
so Paperclip's access profiles can select whole capability bands: give the
analysts `research_*` and `predict_*`, and reserve `book_*` for the trader.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import Field

from . import data
from .backtest import Backtester
from .config import Settings, load_settings
from .domain import Direction, Prediction, PredictionStatus, utc_today
from .paper import PaperBroker, RiskRejection
from .reports import ReportWriter
from .scoring import Scorer
from .store import Store

try:  # ToolError is surfaced verbatim to the caller; other exceptions are masked
    from fastmcp.exceptions import ToolError
except ImportError:  # pragma: no cover - fallback for older fastmcp
    ToolError = ValueError  # type: ignore[assignment, misc]

mcp = FastMCP(
    name="trading-desk",
    instructions=(
        "Paper-trading research desk for US equities on a swing horizon (days to weeks). "
        "Market data comes from OpenBB. Every prediction you record is graded automatically "
        "against real prices once its horizon elapses, and your accuracy is tracked by name, "
        "so state a confidence you are willing to be measured on. All trading is simulated; "
        "no order reaches a real market."
    ),
)


# MCP tool annotations. Governance layers (Paperclip's catalog among them)
# classify risk from these hints; without them every tool is assumed read-only,
# which would file order placement alongside a price lookup.
READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
"""Reserved for calls that move the book or drop coverage. Irreversible from the
agent's side: a fill cannot be un-filled, only offset by another trade."""


class _Desk:
    """Lazily-built singletons. Import of `openbb` is slow, so nothing loads at import time."""

    def __init__(self) -> None:
        self._settings: Optional[Settings] = None
        self._store: Optional[Store] = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings()
        return self._settings

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.settings.db_path, self.settings.starting_cash)
        return self._store

    @property
    def broker(self) -> PaperBroker:
        return PaperBroker(self.store, self.settings)

    @property
    def scorer(self) -> Scorer:
        return Scorer(self.store, self.settings)

    @property
    def backtester(self) -> Backtester:
        return Backtester(self.store, self.settings)

    @property
    def reporter(self) -> ReportWriter:
        return ReportWriter(self.store, self.settings)


desk = _Desk()


def _since(days: Optional[int]) -> Optional[date]:
    return utc_today() - timedelta(days=days) if days else None


# =============================================================== research


@mcp.tool(tags={"research", "read"}, annotations=READ)
def research_watchlist() -> dict:
    """List the tickers the desk currently covers."""
    tickers = desk.store.watchlist(fallback=desk.settings.watchlist)
    return {"tickers": tickers, "count": len(tickers)}


@mcp.tool(tags={"research", "write"}, annotations=WRITE)
def research_add_to_watchlist(
    tickers: Annotated[list[str], Field(description="Ticker symbols to add, e.g. ['NVDA','AMD']")],
    agent: Annotated[str, Field(description="Your agent name, for the audit trail")],
) -> dict:
    """Add tickers to the desk watchlist."""
    added = desk.store.add_to_watchlist(tickers, added_by=agent)
    return {"added": added, "watchlist": desk.store.watchlist()}


@mcp.tool(tags={"research", "destructive"}, annotations=DESTRUCTIVE)
def research_remove_from_watchlist(
    tickers: Annotated[list[str], Field(description="Ticker symbols to drop")],
) -> dict:
    """Remove tickers from the desk watchlist."""
    removed = desk.store.remove_from_watchlist(tickers)
    return {"removed": removed, "watchlist": desk.store.watchlist()}


@mcp.tool(tags={"research", "read"}, annotations=READ)
def research_snapshot(
    ticker: Annotated[str, Field(description="A single US equity ticker, e.g. 'AAPL'")],
) -> dict:
    """Pre-computed technicals for one ticker: moving averages, RSI, ATR, realised
    volatility, trailing returns and 52-week range position.

    Prefer this over computing indicators yourself from raw bars.
    """
    try:
        return data.snapshot(ticker, provider=desk.settings.openbb_provider).to_dict()
    except data.MarketDataError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(tags={"research", "read"}, annotations=READ)
def research_screen(
    tickers: Annotated[
        Optional[list[str]],
        Field(description="Tickers to screen. Defaults to the whole watchlist."),
    ] = None,
) -> dict:
    """Snapshot several tickers at once, for ranking candidates in a single pass."""
    universe = tickers or desk.store.watchlist(fallback=desk.settings.watchlist)
    rows: list[dict] = []
    errors: list[dict] = []
    for ticker in universe:
        try:
            rows.append(data.snapshot(ticker, provider=desk.settings.openbb_provider).to_dict())
        except data.MarketDataError as exc:
            errors.append({"ticker": ticker.upper(), "error": str(exc)})
    return {"count": len(rows), "snapshots": rows, "errors": errors}


@mcp.tool(tags={"research", "read"}, annotations=READ)
def research_price_history(
    ticker: Annotated[str, Field(description="A single US equity ticker")],
    sessions: Annotated[int, Field(ge=1, le=250, description="How many recent sessions to return")] = 30,
) -> dict:
    """Recent daily OHLCV bars for one ticker."""
    try:
        frame = data.bars(ticker, provider=desk.settings.openbb_provider).tail(sessions)
    except data.MarketDataError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "ticker": ticker.upper(),
        "sessions": len(frame),
        "bars": [
            {
                "date": day.isoformat(),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
                "volume": None if row["volume"] != row["volume"] else int(row["volume"]),
            }
            for day, row in frame.iterrows()
        ],
    }


# ============================================================ predictions


@mcp.tool(tags={"predict", "write"}, annotations=WRITE)
def predict_record(
    ticker: Annotated[str, Field(description="US equity ticker the call is about")],
    direction: Annotated[str, Field(description="One of: up, down, flat")],
    confidence: Annotated[
        float, Field(ge=0.0, le=1.0, description="Your probability that this call is right, 0-1")
    ],
    horizon_sessions: Annotated[
        int, Field(ge=1, description="Horizon in trading sessions, e.g. 10 for about two weeks")
    ],
    rationale: Annotated[
        str, Field(description="Why. Cite the evidence you used; this is reviewed by the PM.")
    ],
    agent: Annotated[str, Field(description="Your agent name â€” accuracy is tracked against it")],
    target_return: Annotated[
        Optional[float], Field(description="Optional expected return as a decimal, e.g. 0.04 for +4%")
    ] = None,
) -> dict:
    """Record a dated, falsifiable prediction.

    The entry price is stamped from the latest close at the moment of recording,
    and the call is graded automatically once `horizon_sessions` have elapsed.
    A `flat` call means "moves less than Â±2%".
    """
    if desk.settings.max_horizon_days and horizon_sessions > desk.settings.max_horizon_days:
        raise ToolError(
            f"horizon_sessions {horizon_sessions} exceeds the desk maximum of "
            f"{desk.settings.max_horizon_days}"
        )
    try:
        as_of, entry_price = data.latest_bar(ticker, provider=desk.settings.openbb_provider)
        prediction = Prediction(
            ticker=ticker,
            direction=Direction.parse(direction),
            confidence=confidence,
            horizon_sessions=horizon_sessions,
            rationale=rationale,
            agent=agent,
            as_of=as_of,
            entry_price=entry_price,
            target_return=target_return,
        )
    except (data.MarketDataError, ValueError) as exc:
        raise ToolError(str(exc)) from exc

    desk.store.add_prediction(prediction)
    return {
        "prediction_id": prediction.id,
        "ticker": prediction.ticker,
        "direction": prediction.direction.value,
        "confidence": prediction.confidence,
        "entry_price": round(entry_price, 4),
        "as_of": as_of.isoformat(),
        "horizon_sessions": horizon_sessions,
        "grades_on_or_after": prediction.estimated_resolution.isoformat(),
    }


@mcp.tool(tags={"predict", "read"}, annotations=READ)
def predict_list(
    status: Annotated[
        Optional[str], Field(description="Filter by 'open', 'resolved' or 'void'")
    ] = None,
    agent: Annotated[Optional[str], Field(description="Filter by agent name")] = None,
    ticker: Annotated[Optional[str], Field(description="Filter by ticker")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict:
    """List predictions, newest first."""
    try:
        parsed = PredictionStatus(status.lower()) if status else None
    except ValueError as exc:
        raise ToolError("status must be one of: open, resolved, void") from exc

    predictions = desk.store.predictions(status=parsed, agent=agent, ticker=ticker, limit=limit)
    return {
        "count": len(predictions),
        "predictions": [
            {
                "id": p.id,
                "ticker": p.ticker,
                "direction": p.direction.value,
                "confidence": p.confidence,
                "horizon_sessions": p.horizon_sessions,
                "agent": p.agent,
                "as_of": p.as_of.isoformat(),
                "entry_price": round(p.entry_price, 4),
                "status": p.status.value,
                "exit_price": None if p.exit_price is None else round(p.exit_price, 4),
                "realized_return": p.realized_return,
                "correct": p.correct,
                "rationale": p.rationale,
            }
            for p in predictions
        ],
    }


@mcp.tool(tags={"predict", "write"}, annotations={**WRITE, "idempotentHint": True})
def predict_resolve_due() -> dict:
    """Grade every open prediction whose horizon has elapsed.

    Safe to call on any schedule: calls whose bar does not exist yet stay open.
    """
    return desk.scorer.resolve_due()


@mcp.tool(tags={"predict", "read"}, annotations=READ)
def predict_scorecard(
    agent: Annotated[Optional[str], Field(description="Agent name, or omit for the whole desk")] = None,
    since_days: Annotated[Optional[int], Field(ge=1, description="Only calls made in the last N days")] = None,
) -> dict:
    """Accuracy, edge and calibration for one agent or the whole desk."""
    card = desk.scorer.scorecard(agent=agent, since=_since(since_days))
    return {
        "agent": card.agent,
        "resolved": card.resolved,
        "open": card.open,
        "hit_rate": card.hit_rate,
        "edge_per_call": card.edge,
        "mean_brier": card.mean_brier,
        "mean_move_when_right": card.mean_return_when_right,
        "mean_move_when_wrong": card.mean_return_when_wrong,
        "calibration": card.calibration,
    }


@mcp.tool(tags={"predict", "read"}, annotations=READ)
def predict_leaderboard(
    since_days: Annotated[Optional[int], Field(ge=1)] = None,
) -> dict:
    """Rank every analyst by edge per call."""
    return {"leaderboard": desk.scorer.leaderboard(since=_since(since_days))}


@mcp.tool(tags={"predict", "read"}, annotations=READ)
def predict_replay(
    agent: Annotated[Optional[str], Field(description="Agent to replay, or omit for the whole desk")] = None,
    since_days: Annotated[Optional[int], Field(ge=1)] = None,
    capital: Annotated[float, Field(gt=0)] = 100_000.0,
    position_pct: Annotated[float, Field(gt=0, le=1, description="Fraction of equity per call")] = 0.10,
) -> dict:
    """Replay resolved calls as fixed-size trades, against naive and SPY baselines.

    A per-call model, not a portfolio simulation â€” overlapping calls mean the
    compounded curve overstates what one book could have held.
    """
    try:
        return desk.backtester.replay(
            agent=agent,
            since=_since(since_days),
            capital=capital,
            position_pct=position_pct,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


# ============================================================= paper book


@mcp.tool(tags={"book", "read"}, annotations=READ)
def book_portfolio() -> dict:
    """Current simulated positions, cash and equity."""
    snapshot = desk.broker.portfolio()
    return {
        "as_of": snapshot.as_of.isoformat(),
        "cash": round(snapshot.cash, 2),
        "market_value": round(snapshot.market_value, 2),
        "equity": round(snapshot.equity, 2),
        "gross_exposure_pct": round(snapshot.gross_exposure_pct, 4),
        "positions": snapshot.positions,
    }


@mcp.tool(tags={"book", "read"}, annotations=READ)
def book_performance() -> dict:
    """Return, P&L, exposure and max drawdown for the paper book."""
    return desk.broker.performance()


@mcp.tool(tags={"book", "read"}, annotations=READ)
def book_position_sizing(
    ticker: Annotated[str, Field(description="Ticker you are considering buying")],
) -> dict:
    """Largest position the risk limits currently allow in `ticker`.

    Check this before submitting a buy â€” it tells you the ceiling and which
    limit is binding, instead of guessing and being rejected.
    """
    try:
        return desk.broker.max_affordable_shares(ticker)
    except RiskRejection as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(tags={"book", "trade"}, annotations=DESTRUCTIVE)
def book_submit_order(
    ticker: Annotated[str, Field(description="US equity ticker")],
    side: Annotated[str, Field(description="'buy' or 'sell'")],
    quantity: Annotated[int, Field(ge=1, description="Whole shares")],
    agent: Annotated[str, Field(description="Your agent name, for the audit trail")],
    note: Annotated[str, Field(description="Why you are trading; links the fill to a thesis")] = "",
) -> dict:
    """Execute a simulated market order at the latest close, plus slippage and commission.

    Long-only. Risk limits are enforced here and a rejection explains which
    limit stopped the order.
    """
    try:
        return desk.broker.submit(ticker, side, quantity, agent=agent, note=note).to_dict()
    except (RiskRejection, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(tags={"book", "trade"}, annotations=DESTRUCTIVE)
def book_close_position(
    ticker: Annotated[str, Field(description="Ticker to exit completely")],
    agent: Annotated[str, Field(description="Your agent name")],
    note: Annotated[str, Field(description="Why you are exiting")] = "",
) -> dict:
    """Sell the entire position in `ticker`."""
    try:
        return desk.broker.close_position(ticker, agent=agent, note=note).to_dict()
    except (RiskRejection, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(tags={"book", "write"}, annotations={**WRITE, "idempotentHint": True})
def book_mark_to_market() -> dict:
    """Stamp today's equity onto the curve. Run once per session, at the close."""
    return desk.broker.mark_to_market()


@mcp.tool(tags={"book", "read"}, annotations=READ)
def book_trade_log(
    ticker: Annotated[Optional[str], Field(description="Filter to one ticker")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict:
    """Every simulated fill, oldest first."""
    fills = desk.store.fills(ticker=ticker, limit=limit)
    return {
        "count": len(fills),
        "fills": [
            {
                "id": f.id,
                "ticker": f.ticker,
                "side": f.side.value,
                "quantity": f.quantity,
                "price": round(f.price, 4),
                "commission": round(f.commission, 2),
                "agent": f.agent,
                "note": f.note,
                "filled_at": f.filled_at.isoformat(),
            }
            for f in fills
        ],
    }


# ================================================================ reports


@mcp.tool(tags={"desk", "write"}, annotations=WRITE)
def desk_publish_report(
    title: Annotated[str, Field(description="Report title, e.g. 'NVDA earnings setup'")],
    body_markdown: Annotated[str, Field(description="The report body, in Markdown")],
    agent: Annotated[str, Field(description="Your agent name")],
) -> dict:
    """Publish a Markdown artifact. This is how work reaches the humans."""
    if not body_markdown.strip():
        raise ToolError("a report needs a body")
    return desk.reporter.publish(title, body_markdown, agent=agent)


@mcp.tool(tags={"desk", "write"}, annotations=WRITE)
def desk_publish_daily_brief(
    agent: Annotated[str, Field(description="Publishing agent name")] = "desk",
) -> dict:
    """Generate and publish the end-of-day brief: book, positions, open calls, scoreboard."""
    return desk.reporter.publish_daily_brief(desk.broker, desk.scorer, agent=agent)


@mcp.tool(tags={"desk", "read"}, annotations=READ)
def desk_list_reports(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict:
    """Recently published artifacts."""
    return {"reports": desk.store.reports(limit=limit)}


@mcp.tool(tags={"desk", "read"}, annotations=READ)
def desk_status() -> dict:
    """One call that orients an agent waking on a heartbeat: book, open calls, limits."""
    settings = desk.settings
    performance = desk.broker.performance()
    open_predictions = desk.store.predictions(status=PredictionStatus.OPEN, limit=1000)
    due = [p for p in open_predictions if p.estimated_resolution <= utc_today()]
    return {
        "today": utc_today().isoformat(),
        "book": performance,
        "watchlist": desk.store.watchlist(fallback=settings.watchlist),
        "open_predictions": len(open_predictions),
        "predictions_ready_to_grade": len(due),
        "risk_limits": {
            "max_position_pct": settings.risk.max_position_pct,
            "max_gross_exposure_pct": settings.risk.max_gross_exposure_pct,
            "min_cash_pct": settings.risk.min_cash_pct,
            "max_orders_per_day": settings.risk.max_orders_per_day,
            "orders_filled_today": desk.store.fills_on(utc_today()),
        },
        "costs": {
            "slippage_bps": settings.costs.slippage_bps,
            "commission_per_share": settings.costs.commission_per_share,
        },
    }


def main(stateless: bool = True) -> None:
    """Serve over streamable HTTP.

    Stateless by default: no tool here keeps per-session state, and several MCP
    clients â€” Paperclip's catalog refresh among them â€” issue a bare `tools/list`
    without first completing the `initialize` handshake. In stateful mode that
    is rejected with "Missing session ID"; stateless mode answers it.
    """
    settings = load_settings()
    mcp.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        stateless_http=stateless,
    )


if __name__ == "__main__":
    main()
