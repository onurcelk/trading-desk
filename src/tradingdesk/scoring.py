"""Grading open predictions and scoring the agents that made them.

Hit rate alone rewards an agent that only ever calls the obvious. The number
this module treats as primary is `edge`: the mean return earned in the
direction actually called, which is what a book would have captured.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import fmean
from typing import Optional

from . import data
from .config import Settings
from .domain import Direction, Prediction, PredictionStatus, Scorecard, utc_today
from .store import Store

CALIBRATION_BINS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def signed_return(prediction: Prediction) -> Optional[float]:
    """Return as experienced by someone who traded the call.

    A correct FLAT call earns nothing, so it is scored as the negative of the
    absolute move: staying out of a quiet name is worth zero, not a win.
    """
    if prediction.realized_return is None:
        return None
    if prediction.direction is Direction.UP:
        return prediction.realized_return
    if prediction.direction is Direction.DOWN:
        return -prediction.realized_return
    return -abs(prediction.realized_return)


class Scorer:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------ resolving

    def resolve_due(self, *, force: bool = False) -> dict:
        """Grade every open prediction whose horizon has elapsed.

        Predictions whose bar does not exist yet are left open and reported as
        `still_open`, so this is safe to call from a heartbeat on any schedule.
        """
        today = utc_today()
        open_predictions = self.store.predictions(status=PredictionStatus.OPEN, limit=10_000)

        resolved: list[dict] = []
        still_open = 0
        failures: list[dict] = []

        # One fetch per ticker, reused across that ticker's predictions.
        frames: dict[str, object] = {}

        for prediction in open_predictions:
            if not force and prediction.estimated_resolution > today:
                still_open += 1
                continue

            ticker = prediction.ticker
            if ticker not in frames:
                try:
                    frames[ticker] = data.bars(
                        ticker,
                        start=prediction.as_of - timedelta(days=10),
                        end=today,
                        provider=self.settings.openbb_provider,
                    )
                except data.MarketDataError as exc:
                    frames[ticker] = None
                    failures.append({"prediction_id": prediction.id, "ticker": ticker, "error": str(exc)})

            frame = frames[ticker]
            if frame is None:
                continue

            outcome = data.close_after_sessions(frame, prediction.as_of, prediction.horizon_sessions)
            if outcome is None:
                still_open += 1
                continue

            session, exit_price = outcome
            prediction.grade(exit_price)
            prediction.resolved_at = session
            self.store.update_prediction(prediction)
            resolved.append(
                {
                    "prediction_id": prediction.id,
                    "ticker": ticker,
                    "agent": prediction.agent,
                    "direction": prediction.direction.value,
                    "confidence": prediction.confidence,
                    "entry_price": round(prediction.entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "realized_return": round(prediction.realized_return, 4),
                    "correct": prediction.correct,
                    "brier": round(prediction.brier, 4),
                    "resolved_at": session.isoformat(),
                }
            )

        return {
            "checked": len(open_predictions),
            "resolved": len(resolved),
            "still_open": still_open,
            "failures": failures,
            "details": resolved,
        }

    # ------------------------------------------------------------- scoring

    def scorecard(self, *, agent: Optional[str] = None, since: Optional[date] = None) -> Scorecard:
        resolved = [
            p
            for p in self.store.predictions(
                status=PredictionStatus.RESOLVED, agent=agent, since=since, limit=10_000
            )
            if p.correct is not None
        ]
        open_count = len(
            self.store.predictions(
                status=PredictionStatus.OPEN, agent=agent, since=since, limit=10_000
            )
        )

        if not resolved:
            return Scorecard(
                agent=agent or "all",
                resolved=0,
                open=open_count,
                hit_rate=None,
                mean_brier=None,
                mean_return_when_right=None,
                mean_return_when_wrong=None,
                edge=None,
            )

        right = [p for p in resolved if p.correct]
        wrong = [p for p in resolved if not p.correct]
        edges = [e for e in (signed_return(p) for p in resolved) if e is not None]

        calibration = []
        for low, high in CALIBRATION_BINS:
            bucket = [p for p in resolved if low <= p.confidence < high]
            if not bucket:
                continue
            calibration.append(
                {
                    "confidence_band": f"{low:.0%}-{min(high, 1.0):.0%}",
                    "count": len(bucket),
                    "stated_confidence": round(fmean(p.confidence for p in bucket), 4),
                    "actual_hit_rate": round(fmean(1.0 if p.correct else 0.0 for p in bucket), 4),
                }
            )

        return Scorecard(
            agent=agent or "all",
            resolved=len(resolved),
            open=open_count,
            hit_rate=round(len(right) / len(resolved), 4),
            mean_brier=round(fmean(p.brier for p in resolved if p.brier is not None), 4),
            mean_return_when_right=(
                round(fmean(abs(p.realized_return) for p in right), 4) if right else None
            ),
            mean_return_when_wrong=(
                round(fmean(abs(p.realized_return) for p in wrong), 4) if wrong else None
            ),
            edge=round(fmean(edges), 4) if edges else None,
            calibration=calibration,
        )

    def leaderboard(self, *, since: Optional[date] = None) -> list[dict]:
        """Every agent's scorecard, best edge first."""
        cards = [
            self.scorecard(agent=agent, since=since)
            for agent in self.store.agents_with_predictions()
        ]
        ranked = sorted(
            cards,
            key=lambda c: (c.edge if c.edge is not None else float("-inf"), c.resolved),
            reverse=True,
        )
        return [
            {
                "agent": c.agent,
                "resolved": c.resolved,
                "open": c.open,
                "hit_rate": c.hit_rate,
                "edge": c.edge,
                "mean_brier": c.mean_brier,
            }
            for c in ranked
        ]
