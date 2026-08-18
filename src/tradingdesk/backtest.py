"""Replay an agent's resolved calls as if each had been traded.

This is deliberately a *per-call* model, not a portfolio simulation: each
resolved prediction is treated as one independent position of fixed size, and
results are chained in resolution order. Real calls overlap in time, so the
compounded curve here overstates what a single book could have held. Use it to
compare agents against each other and against the naive baseline — not as a
forecast of live returns.
"""

from __future__ import annotations

from datetime import date
from statistics import fmean, pstdev
from typing import Optional

from . import data
from .config import Settings
from .domain import Direction, PredictionStatus
from .scoring import signed_return
from .store import Store


def _drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


class Backtester:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def replay(
        self,
        *,
        agent: Optional[str] = None,
        since: Optional[date] = None,
        capital: float = 100_000.0,
        position_pct: float = 0.10,
        include_benchmark: bool = True,
    ) -> dict:
        if not 0 < position_pct <= 1:
            raise ValueError("position_pct must be between 0 and 1")

        resolved = [
            p
            for p in self.store.predictions(
                status=PredictionStatus.RESOLVED, agent=agent, since=since, limit=10_000
            )
            if p.realized_return is not None and p.resolved_at is not None
        ]
        resolved.sort(key=lambda p: (p.resolved_at, p.id or 0))

        if not resolved:
            return {
                "agent": agent or "all",
                "trades": 0,
                "note": "no resolved predictions to replay yet",
            }

        # FLAT calls express "no trade", so they contribute no P&L but still
        # count against the record.
        equity = capital
        curve = [capital]
        trades: list[dict] = []
        slippage = self.settings.costs.slippage_bps / 10_000.0

        for prediction in resolved:
            if prediction.direction is Direction.FLAT:
                trades.append(
                    {
                        "resolved_at": prediction.resolved_at.isoformat(),
                        "ticker": prediction.ticker,
                        "direction": "flat",
                        "pnl": 0.0,
                        "equity": round(equity, 2),
                    }
                )
                curve.append(equity)
                continue

            gross = signed_return(prediction) or 0.0
            net = gross - 2 * slippage  # entry and exit friction
            pnl = equity * position_pct * net
            equity += pnl
            curve.append(equity)
            trades.append(
                {
                    "resolved_at": prediction.resolved_at.isoformat(),
                    "ticker": prediction.ticker,
                    "direction": prediction.direction.value,
                    "confidence": prediction.confidence,
                    "gross_return": round(gross, 4),
                    "net_return": round(net, 4),
                    "pnl": round(pnl, 2),
                    "equity": round(equity, 2),
                }
            )

        traded = [t for t in trades if t.get("direction") != "flat"]
        wins = [t for t in traded if t["pnl"] > 0]
        losses = [t for t in traded if t["pnl"] < 0]
        returns = [t["net_return"] for t in traded]

        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))

        result = {
            "agent": agent or "all",
            "since": since.isoformat() if since else None,
            "starting_capital": round(capital, 2),
            "ending_equity": round(equity, 2),
            "total_return": round(equity / capital - 1.0, 4),
            "trades": len(traded),
            "flat_calls": len(trades) - len(traded),
            "win_rate": round(len(wins) / len(traded), 4) if traded else None,
            "average_win": round(fmean(t["net_return"] for t in wins), 4) if wins else None,
            "average_loss": round(fmean(t["net_return"] for t in losses), 4) if losses else None,
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
            "max_drawdown": round(_drawdown(curve), 4),
            "return_stdev": round(pstdev(returns), 4) if len(returns) > 1 else None,
            "position_pct": position_pct,
            "equity_curve": [round(v, 2) for v in curve],
            "trade_log": trades[-50:],
        }

        result["baselines"] = self._baselines(resolved, include_benchmark)
        return result

    def _baselines(self, resolved, include_benchmark: bool) -> dict:
        """What the agent is actually competing with."""
        always_up = fmean(p.realized_return for p in resolved)
        baselines = {
            "always_call_up": {
                "description": "mean return of simply predicting UP on every name the agent looked at",
                "mean_return_per_call": round(always_up, 4),
            }
        }

        if not include_benchmark:
            return baselines

        start = min(p.as_of for p in resolved)
        end = max(p.resolved_at for p in resolved)
        try:
            frame = data.bars(
                "SPY", start=start, end=end, provider=self.settings.openbb_provider
            )
            first, last = float(frame["close"].iloc[0]), float(frame["close"].iloc[-1])
            baselines["spy_buy_and_hold"] = {
                "description": "SPY total price return across the same window",
                "start": frame.index[0].isoformat(),
                "end": frame.index[-1].isoformat(),
                "return": round(last / first - 1.0, 4),
            }
        except data.MarketDataError as exc:
            baselines["spy_buy_and_hold"] = {"error": str(exc)}

        return baselines
