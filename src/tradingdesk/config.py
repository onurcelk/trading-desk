"""Runtime configuration, sourced from environment variables.

Every knob has a working default so the desk runs with no .env at all; see
`.env.example` for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "AVGO", "JPM", "XOM", "SPY",
]


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits the paper broker refuses to cross.

    These are enforced in code rather than left to agent judgement, because an
    agent that miscalculates position size should bounce off a wall, not
    silently concentrate the book.
    """

    max_position_pct: float = 0.20
    """Cap on a single ticker's market value as a fraction of total equity."""

    max_gross_exposure_pct: float = 1.00
    """Cap on total long market value as a fraction of equity. 1.0 = no leverage."""

    min_cash_pct: float = 0.05
    """Fraction of equity that must remain in cash after any buy."""

    max_orders_per_day: int = 20
    """Circuit breaker against a runaway agent loop."""


@dataclass(frozen=True)
class Costs:
    """Friction applied to every simulated fill."""

    commission_per_share: float = 0.005
    commission_minimum: float = 1.00
    slippage_bps: float = 5.0
    """Applied against the agent: buys fill higher, sells fill lower."""


@dataclass(frozen=True)
class Settings:
    db_path: Path
    artifacts_dir: Path
    starting_cash: float
    watchlist: list[str]
    risk: RiskLimits
    costs: Costs
    mcp_host: str
    mcp_port: int
    openbb_provider: str
    max_horizon_days: int

    @property
    def artifact_path(self) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        return self.artifacts_dir


def load_settings() -> Settings:
    root = Path(_env_str("TRADINGDESK_HOME", str(Path(__file__).resolve().parents[2])))
    raw_watchlist = _env_str("TRADINGDESK_WATCHLIST", "")
    watchlist = (
        [t.strip().upper() for t in raw_watchlist.split(",") if t.strip()]
        if raw_watchlist
        else list(DEFAULT_WATCHLIST)
    )

    return Settings(
        db_path=Path(_env_str("TRADINGDESK_DB", str(root / "data" / "desk.sqlite3"))),
        artifacts_dir=Path(_env_str("TRADINGDESK_ARTIFACTS", str(root / "artifacts"))),
        starting_cash=_env_float("TRADINGDESK_STARTING_CASH", 100_000.0),
        watchlist=watchlist,
        risk=RiskLimits(
            max_position_pct=_env_float("TRADINGDESK_MAX_POSITION_PCT", 0.20),
            max_gross_exposure_pct=_env_float("TRADINGDESK_MAX_GROSS_PCT", 1.00),
            min_cash_pct=_env_float("TRADINGDESK_MIN_CASH_PCT", 0.05),
            max_orders_per_day=_env_int("TRADINGDESK_MAX_ORDERS_PER_DAY", 20),
        ),
        costs=Costs(
            commission_per_share=_env_float("TRADINGDESK_COMMISSION_PER_SHARE", 0.005),
            commission_minimum=_env_float("TRADINGDESK_COMMISSION_MIN", 1.00),
            slippage_bps=_env_float("TRADINGDESK_SLIPPAGE_BPS", 5.0),
        ),
        mcp_host=_env_str("TRADINGDESK_MCP_HOST", "127.0.0.1"),
        mcp_port=_env_int("TRADINGDESK_MCP_PORT", 8010),
        openbb_provider=_env_str("TRADINGDESK_PROVIDER", "yfinance"),
        max_horizon_days=_env_int("TRADINGDESK_MAX_HORIZON_DAYS", 60),
    )
