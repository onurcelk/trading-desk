"""Command line entry point â€” operator-facing counterpart to the MCP tools.

Everything an agent can do through MCP can also be driven from here, which
makes the desk testable and inspectable without a running agent.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from typing import Optional

from . import data
from .backtest import Backtester
from .config import load_settings
from .domain import Direction, Prediction, PredictionStatus, utc_today
from .paper import PaperBroker, RiskRejection
from .reports import ReportWriter
from .scoring import Scorer
from .store import Store


def _emit(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _context():
    settings = load_settings()
    store = Store(settings.db_path, settings.starting_cash)
    return settings, store


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_server import mcp

    settings = load_settings()
    host = args.host or settings.mcp_host
    port = args.port or settings.mcp_port
    mode = "stateful" if args.stateful else "stateless"
    print(f"trading-desk MCP server on http://{host}:{port}/mcp ({mode})", file=sys.stderr)
    mcp.run(transport="http", host=host, port=port, stateless_http=not args.stateful)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .web import serve

    settings = load_settings()
    host = args.host or settings.mcp_host
    port = args.port
    print(f"trading-desk dashboard on http://{host}:{port}", file=sys.stderr)
    serve(host, port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings, store = _context()
    broker = PaperBroker(store, settings)
    open_predictions = store.predictions(status=PredictionStatus.OPEN, limit=1000)
    _emit(
        {
            "database": str(settings.db_path),
            "artifacts": str(settings.artifacts_dir),
            "watchlist": store.watchlist(fallback=settings.watchlist),
            "book": broker.performance(),
            "open_predictions": len(open_predictions),
            "reports": len(store.reports(limit=200)),
        }
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    settings, _ = _context()
    try:
        _emit(data.snapshot(args.ticker, provider=settings.openbb_provider).to_dict())
    except data.MarketDataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    settings, store = _context()
    try:
        as_of, entry = data.latest_bar(args.ticker, provider=settings.openbb_provider)
        prediction = Prediction(
            ticker=args.ticker,
            direction=Direction.parse(args.direction),
            confidence=args.confidence,
            horizon_sessions=args.horizon,
            rationale=args.rationale,
            agent=args.agent,
            as_of=as_of,
            entry_price=entry,
        )
    except (data.MarketDataError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    store.add_prediction(prediction)
    _emit({"prediction_id": prediction.id, "ticker": prediction.ticker, "entry_price": entry})
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    settings, store = _context()
    _emit(Scorer(store, settings).resolve_due(force=args.force))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    settings, store = _context()
    scorer = Scorer(store, settings)
    if args.agent:
        card = scorer.scorecard(agent=args.agent)
        _emit(card.__dict__)
    else:
        _emit({"leaderboard": scorer.leaderboard(), "desk": scorer.scorecard().__dict__})
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    settings, store = _context()
    result = Backtester(store, settings).replay(
        agent=args.agent,
        capital=args.capital,
        position_pct=args.position_pct,
        include_benchmark=not args.no_benchmark,
    )
    result.pop("equity_curve", None)
    _emit(result)
    return 0


def cmd_buy_sell(args: argparse.Namespace) -> int:
    settings, store = _context()
    broker = PaperBroker(store, settings)
    try:
        result = broker.submit(
            args.ticker, args.side, args.quantity, agent=args.agent, note=args.note
        )
    except RiskRejection as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return 1
    _emit(result.to_dict())
    return 0


def cmd_portfolio(args: argparse.Namespace) -> int:
    settings, store = _context()
    broker = PaperBroker(store, settings)
    snapshot = broker.portfolio()
    _emit(
        {
            "cash": round(snapshot.cash, 2),
            "equity": round(snapshot.equity, 2),
            "positions": snapshot.positions,
            "performance": broker.performance(),
        }
    )
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    settings, store = _context()
    broker = PaperBroker(store, settings)
    scorer = Scorer(store, settings)
    writer = ReportWriter(store, settings)
    if args.stdout:
        print(writer.daily_brief(broker, scorer))
        return 0
    _emit(writer.publish_daily_brief(broker, scorer))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Backfill dated predictions from real history so scoring has something to grade.

    Calls are stamped in the past and priced from the actual bar on that date,
    so `resolve` grades them against genuine subsequent prices. It proves the
    pipeline end to end; it says nothing about whether any strategy works.
    """
    settings, store = _context()
    scorer = Scorer(store, settings)
    as_of_target = utc_today() - timedelta(days=args.days_ago)
    tickers = args.tickers or store.watchlist(fallback=settings.watchlist)[:6]

    created: list[dict] = []
    skipped: list[str] = []
    for index, ticker in enumerate(tickers):
        agent = f"demo-analyst-{index % 2 + 1}"
        try:
            frame = data.bars(
                ticker,
                start=as_of_target - timedelta(days=30),
                end=utc_today(),
                provider=settings.openbb_provider,
            )
            anchor = data.close_on_or_before(frame, as_of_target)
        except data.MarketDataError as exc:
            print(f"skip {ticker}: {exc}", file=sys.stderr)
            continue
        if anchor is None:
            continue

        session, price = anchor
        # Re-running the backfill must not pile up duplicate calls.
        if store.has_prediction(ticker, agent, session):
            skipped.append(ticker.upper())
            continue

        # Alternate the calls so the scorecard has both hits and misses.
        direction = Direction.UP if index % 2 == 0 else Direction.DOWN
        prediction = Prediction(
            ticker=ticker,
            direction=direction,
            confidence=0.55 + 0.05 * (index % 4),
            horizon_sessions=args.horizon,
            rationale=f"Demo backfill for {ticker} anchored on {session}.",
            agent=agent,
            as_of=session,
            entry_price=price,
        )
        store.add_prediction(prediction)
        created.append({"id": prediction.id, "ticker": ticker, "as_of": session.isoformat()})

    resolution = scorer.resolve_due()
    _emit(
        {
            "created": created,
            "skipped_existing": skipped,
            "resolution": resolution,
            "leaderboard": scorer.leaderboard(),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradingdesk",
        description="Paper-trading equity research desk backed by OpenBB.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server for Paperclip agents")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument(
        "--stateful",
        action="store_true",
        help="require the MCP initialize handshake; breaks clients that issue a bare tools/list",
    )
    serve.set_defaults(func=cmd_serve)

    dashboard = sub.add_parser("dashboard", help="run the operator web dashboard")
    dashboard.add_argument("--host")
    dashboard.add_argument("--port", type=int, default=8020)
    dashboard.set_defaults(func=cmd_dashboard)

    status = sub.add_parser("status", help="show desk configuration and book state")
    status.set_defaults(func=cmd_status)

    snapshot = sub.add_parser("snapshot", help="technical snapshot for one ticker")
    snapshot.add_argument("ticker")
    snapshot.set_defaults(func=cmd_snapshot)

    predict = sub.add_parser("predict", help="record a prediction")
    predict.add_argument("ticker")
    predict.add_argument("direction", choices=[d.value for d in Direction])
    predict.add_argument("--confidence", type=float, required=True)
    predict.add_argument("--horizon", type=int, default=10)
    predict.add_argument("--rationale", required=True)
    predict.add_argument("--agent", default="operator")
    predict.set_defaults(func=cmd_predict)

    resolve = sub.add_parser("resolve", help="grade predictions whose horizon has elapsed")
    resolve.add_argument("--force", action="store_true", help="attempt every open call")
    resolve.set_defaults(func=cmd_resolve)

    score = sub.add_parser("score", help="scorecards and leaderboard")
    score.add_argument("--agent")
    score.set_defaults(func=cmd_score)

    replay = sub.add_parser("replay", help="backtest resolved calls")
    replay.add_argument("--agent")
    replay.add_argument("--capital", type=float, default=100_000.0)
    replay.add_argument("--position-pct", type=float, default=0.10)
    replay.add_argument("--no-benchmark", action="store_true")
    replay.set_defaults(func=cmd_replay)

    trade = sub.add_parser("trade", help="submit a simulated order")
    trade.add_argument("side", choices=["buy", "sell"])
    trade.add_argument("ticker")
    trade.add_argument("quantity", type=int)
    trade.add_argument("--agent", default="operator")
    trade.add_argument("--note", default="")
    trade.set_defaults(func=cmd_buy_sell)

    portfolio = sub.add_parser("portfolio", help="show the paper book")
    portfolio.set_defaults(func=cmd_portfolio)

    brief = sub.add_parser("brief", help="publish the desk brief artifact")
    brief.add_argument("--stdout", action="store_true", help="print instead of publishing")
    brief.set_defaults(func=cmd_brief)

    demo = sub.add_parser("demo", help="backfill dated predictions from real history and grade them")
    demo.add_argument("--tickers", nargs="*")
    demo.add_argument("--days-ago", type=int, default=45)
    demo.add_argument("--horizon", type=int, default=10)
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
