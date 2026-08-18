"""Core value objects for the desk.

Horizons are counted in *trading sessions*, never calendar days, and
resolution is done by indexing into the actual bar series returned by OpenBB.
That sidesteps market-holiday math entirely: if the bar isn't there yet, the
prediction simply stays open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"

    @classmethod
    def parse(cls, value: str) -> "Direction":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            allowed = ", ".join(d.value for d in cls)
            raise ValueError(f"direction must be one of: {allowed}") from exc


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @classmethod
    def parse(cls, value: str) -> "Side":
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError("side must be 'buy' or 'sell'") from exc


class PredictionStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    VOID = "void"


# A "flat" call is judged against this band: a move smaller than this in either
# direction counts as flat. Without a band, "flat" would be unfalsifiable.
FLAT_BAND = 0.02


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_today() -> date:
    """Today in UTC.

    Every timestamp the desk stores is UTC, so anything that compares a date
    against stored data must use this rather than `date.today()`. Using the
    local date silently breaks the daily order limit for the hours where the
    two disagree — the counter looks at a day the fills were never filed under,
    finds nothing, and the circuit breaker fails open.
    """
    return utc_now().date()


def estimate_resolution_date(as_of: date, horizon_sessions: int) -> date:
    """Calendar date by which `horizon_sessions` sessions have *probably* passed.

    Only used to decide when it is worth re-checking a prediction. Actual
    resolution is driven by real bars, so an early guess costs nothing but a
    lookup that returns "still open".
    """
    from datetime import timedelta

    calendar_days = math.ceil(horizon_sessions * 7 / 5) + 1
    return as_of + timedelta(days=calendar_days)


@dataclass
class Prediction:
    ticker: str
    direction: Direction
    confidence: float
    horizon_sessions: int
    rationale: str
    agent: str
    as_of: date
    entry_price: float
    id: Optional[int] = None
    target_return: Optional[float] = None
    status: PredictionStatus = PredictionStatus.OPEN
    resolved_at: Optional[date] = None
    exit_price: Optional[float] = None
    realized_return: Optional[float] = None
    correct: Optional[bool] = None
    brier: Optional[float] = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.ticker = self.ticker.strip().upper()
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if self.horizon_sessions < 1:
            raise ValueError("horizon_sessions must be at least 1")
        if not self.rationale.strip():
            raise ValueError("a prediction requires a written rationale")

    @property
    def estimated_resolution(self) -> date:
        return estimate_resolution_date(self.as_of, self.horizon_sessions)

    def grade(self, exit_price: float) -> "Prediction":
        """Return self, mutated with the outcome of this call.

        `correct` is judged on direction against FLAT_BAND; `brier` scores how
        well-calibrated the stated confidence was.
        """
        realized = (exit_price - self.entry_price) / self.entry_price
        if realized > FLAT_BAND:
            actual = Direction.UP
        elif realized < -FLAT_BAND:
            actual = Direction.DOWN
        else:
            actual = Direction.FLAT

        self.exit_price = exit_price
        self.realized_return = realized
        self.correct = actual == self.direction
        self.brier = (self.confidence - (1.0 if self.correct else 0.0)) ** 2
        self.status = PredictionStatus.RESOLVED
        return self


@dataclass
class Fill:
    ticker: str
    side: Side
    quantity: int
    price: float
    """Execution price *after* slippage."""
    commission: float
    reference_price: float
    """Pre-slippage price the fill was derived from."""
    filled_at: datetime = field(default_factory=utc_now)
    agent: str = "unknown"
    note: str = ""
    id: Optional[int] = None

    @property
    def cash_delta(self) -> float:
        """Signed effect on cash, commission included."""
        gross = self.quantity * self.price
        return -(gross + self.commission) if self.side is Side.BUY else gross - self.commission


@dataclass
class Position:
    ticker: str
    quantity: int = 0
    average_cost: float = 0.0
    realized_pnl: float = 0.0

    def apply(self, fill: Fill) -> None:
        """Fold a fill into this position using average-cost accounting."""
        if fill.side is Side.BUY:
            total_cost = self.average_cost * self.quantity + fill.price * fill.quantity
            self.quantity += fill.quantity
            self.average_cost = total_cost / self.quantity if self.quantity else 0.0
        else:
            if fill.quantity > self.quantity:
                raise ValueError(
                    f"cannot sell {fill.quantity} {self.ticker}; only {self.quantity} held "
                    "(this desk does not support short positions)"
                )
            self.realized_pnl += (fill.price - self.average_cost) * fill.quantity
            self.quantity -= fill.quantity
            if self.quantity == 0:
                self.average_cost = 0.0

    def market_value(self, last_price: float) -> float:
        return self.quantity * last_price

    def unrealized_pnl(self, last_price: float) -> float:
        return (last_price - self.average_cost) * self.quantity


@dataclass
class PortfolioSnapshot:
    as_of: datetime
    cash: float
    positions: list[dict]
    market_value: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float

    @property
    def gross_exposure_pct(self) -> float:
        return self.market_value / self.equity if self.equity else 0.0


@dataclass
class Scorecard:
    agent: str
    resolved: int
    open: int
    hit_rate: Optional[float]
    mean_brier: Optional[float]
    mean_return_when_right: Optional[float]
    mean_return_when_wrong: Optional[float]
    edge: Optional[float]
    """Mean signed return in the direction called. The number that actually matters."""
    calibration: list[dict] = field(default_factory=list)
