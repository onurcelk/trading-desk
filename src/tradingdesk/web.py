"""Local web dashboard for the desk.

A read-only operator view: the book, the open calls, how each analyst is
scoring, and the published artifacts. Nothing here can move the book â€” trading
stays behind the MCP tools, where the risk gates are.

Served by FastAPI; the page itself is a single self-contained HTML file with no
external requests, so it works offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .backtest import Backtester
from .config import Settings, load_settings
from .domain import PredictionStatus, utc_today
from .paper import PaperBroker
from .scoring import Scorer
from .store import Store

PAGE = Path(__file__).parent / "static" / "dashboard.html"


def _prediction_row(prediction) -> dict:
    return {
        "id": prediction.id,
        "ticker": prediction.ticker,
        "direction": prediction.direction.value,
        "confidence": prediction.confidence,
        "horizon_sessions": prediction.horizon_sessions,
        "agent": prediction.agent,
        "as_of": prediction.as_of.isoformat(),
        "entry_price": round(prediction.entry_price, 2),
        "exit_price": None if prediction.exit_price is None else round(prediction.exit_price, 2),
        "realized_return": prediction.realized_return,
        "correct": prediction.correct,
        "status": prediction.status.value,
        "resolves": prediction.estimated_resolution.isoformat(),
        "rationale": prediction.rationale,
    }


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path, settings.starting_cash)

    app = FastAPI(title="Trading Desk", docs_url=None, redoc_url=None)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html")

    @app.get("/api/state")
    def state() -> JSONResponse:
        broker = PaperBroker(store, settings)
        scorer = Scorer(store, settings)

        portfolio = broker.portfolio()
        desk_card = scorer.scorecard()
        open_predictions = store.predictions(status=PredictionStatus.OPEN, limit=200)
        resolved = store.predictions(status=PredictionStatus.RESOLVED, limit=200)

        return JSONResponse(
            {
                "as_of": utc_today().isoformat(),
                "performance": broker.performance(),
                "positions": portfolio.positions,
                "equity_curve": store.equity_curve(),
                "starting_cash": store.initial_cash(),
                "predictions": {
                    "open": [_prediction_row(p) for p in open_predictions],
                    "resolved": [_prediction_row(p) for p in resolved],
                },
                "leaderboard": scorer.leaderboard(),
                "calibration": desk_card.calibration,
                "desk": {
                    "resolved": desk_card.resolved,
                    "open": desk_card.open,
                    "hit_rate": desk_card.hit_rate,
                    "edge": desk_card.edge,
                    "mean_brier": desk_card.mean_brier,
                },
                "watchlist": store.watchlist(fallback=settings.watchlist),
                "reports": store.reports(limit=25),
                "risk_limits": {
                    "max_position_pct": settings.risk.max_position_pct,
                    "max_gross_exposure_pct": settings.risk.max_gross_exposure_pct,
                    "min_cash_pct": settings.risk.min_cash_pct,
                    "max_orders_per_day": settings.risk.max_orders_per_day,
                    "orders_filled_today": store.fills_on(utc_today()),
                },
                "fills": [
                    {
                        "ticker": f.ticker,
                        "side": f.side.value,
                        "quantity": f.quantity,
                        "price": round(f.price, 2),
                        "agent": f.agent,
                        "note": f.note,
                        "filled_at": f.filled_at.isoformat(),
                    }
                    for f in store.fills(limit=100)
                ][::-1],
            }
        )

    @app.get("/api/replay")
    def replay(agent: Optional[str] = None, position_pct: float = 0.10) -> JSONResponse:
        result = Backtester(store, settings).replay(
            agent=agent, position_pct=position_pct, include_benchmark=True
        )
        return JSONResponse(result)

    @app.get("/api/reports/{report_id}", response_class=PlainTextResponse)
    def report(report_id: int) -> str:
        match = next((r for r in store.reports(limit=500) if r["id"] == report_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="no such report")
        path = Path(match["path"])
        if not path.exists():
            raise HTTPException(status_code=410, detail="report file has been removed")
        return path.read_text(encoding="utf-8")

    return app


def serve(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
