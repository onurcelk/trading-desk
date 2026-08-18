"""Markdown artifacts â€” the desk's only user-facing surface.

Paperclip renders agent artifacts in its own dashboard, so reports are written
as plain Markdown files on disk and indexed in the store. No separate frontend.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from .config import Settings
from .domain import PredictionStatus, utc_now, utc_today
from .paper import PaperBroker
from .scoring import Scorer
from .store import Store

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.strip().lower()).strip("-")[:60] or "report"


def _pct(value: Optional[float], digits: int = 1) -> str:
    return "â€”" if value is None else f"{value:.{digits}%}"


def _money(value: Optional[float]) -> str:
    return "â€”" if value is None else f"${value:,.2f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_None._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cells) + " |" for cells in rows)
    return "\n".join(lines) + "\n"


class ReportWriter:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def publish(self, title: str, body: str, *, agent: str) -> dict:
        """Write a Markdown artifact and index it."""
        stamp = utc_now()
        filename = f"{stamp:%Y-%m-%d}-{slugify(agent)}-{slugify(title)}.md"
        path = self.settings.artifact_path / filename

        header = (
            f"# {title}\n\n"
            f"*Author: {agent} Â· Generated: {stamp:%Y-%m-%d %H:%M UTC}*\n\n"
            "---\n\n"
        )
        path.write_text(header + body.strip() + "\n", encoding="utf-8")
        report_id = self.store.record_report(title, agent, path)

        return {
            "report_id": report_id,
            "title": title,
            "agent": agent,
            "path": str(path),
            "bytes": path.stat().st_size,
        }

    def daily_brief(self, broker: PaperBroker, scorer: Scorer) -> str:
        """The end-of-day desk summary, assembled from live state."""
        today = utc_today()
        portfolio = broker.portfolio()
        performance = broker.performance()
        open_predictions = self.store.predictions(status=PredictionStatus.OPEN, limit=200)
        leaderboard = scorer.leaderboard()
        overall = scorer.scorecard()

        sections: list[str] = [f"## Desk brief â€” {today:%A, %d %B %Y}\n"]

        sections.append("### Book\n")
        sections.append(
            _table(
                ["Metric", "Value"],
                [
                    ["Equity", _money(performance["equity"])],
                    ["Cash", _money(performance["cash"])],
                    ["Total return", _pct(performance["total_return"], 2)],
                    ["Realised P&L", _money(performance["realized_pnl"])],
                    ["Unrealised P&L", _money(performance["unrealized_pnl"])],
                    ["Gross exposure", _pct(performance["gross_exposure_pct"])],
                    ["Max drawdown", _pct(performance["max_drawdown"], 2)],
                ],
            )
        )

        sections.append("\n### Positions\n")
        sections.append(
            _table(
                ["Ticker", "Qty", "Avg cost", "Last", "Value", "Unrealised", "Weight"],
                [
                    [
                        row["ticker"],
                        str(row["quantity"]),
                        _money(row["average_cost"]),
                        _money(row["last_price"]),
                        _money(row["market_value"]),
                        f"{_money(row['unrealized_pnl'])} ({_pct(row['unrealized_pct'])})",
                        _pct(row["weight"]),
                    ]
                    for row in portfolio.positions
                ],
            )
        )

        sections.append("\n### Open predictions\n")
        sections.append(
            _table(
                ["#", "Ticker", "Call", "Conf.", "Horizon", "Made", "Resolves ~", "Analyst"],
                [
                    [
                        str(p.id),
                        p.ticker,
                        p.direction.value.upper(),
                        _pct(p.confidence, 0),
                        f"{p.horizon_sessions}d",
                        p.as_of.isoformat(),
                        p.estimated_resolution.isoformat(),
                        p.agent,
                    ]
                    for p in open_predictions[:25]
                ],
            )
        )

        sections.append("\n### Analyst scoreboard\n")
        sections.append(
            _table(
                ["Analyst", "Resolved", "Open", "Hit rate", "Edge/call", "Brier"],
                [
                    [
                        row["agent"],
                        str(row["resolved"]),
                        str(row["open"]),
                        _pct(row["hit_rate"]),
                        _pct(row["edge"], 2),
                        "â€”" if row["mean_brier"] is None else f"{row['mean_brier']:.3f}",
                    ]
                    for row in leaderboard
                ],
            )
        )
        sections.append(
            "\n*Edge is the mean return earned in the direction called â€” the number that "
            "matters. Brier scores calibration; lower is better, and 0.25 is what you get "
            "by saying 50% to everything.*\n"
        )

        if overall.calibration:
            sections.append("\n### Calibration (desk-wide)\n")
            sections.append(
                _table(
                    ["Stated confidence", "Calls", "Claimed", "Actual"],
                    [
                        [
                            row["confidence_band"],
                            str(row["count"]),
                            _pct(row["stated_confidence"]),
                            _pct(row["actual_hit_rate"]),
                        ]
                        for row in overall.calibration
                    ],
                )
            )

        return "\n".join(sections)

    def publish_daily_brief(self, broker: PaperBroker, scorer: Scorer, *, agent: str = "desk") -> dict:
        return self.publish(
            f"Desk brief {utc_today():%Y-%m-%d}",
            self.daily_brief(broker, scorer),
            agent=agent,
        )
