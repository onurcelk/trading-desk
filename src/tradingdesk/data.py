"""Market data adapter over the OpenBB Platform.

Everything the desk knows about prices flows through here, so provider changes
stay in one place. Bars are returned as a DataFrame indexed by `datetime.date`
in ascending order, with lowercase OHLCV columns.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

_CACHE_TTL_SECONDS = 900.0
_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
_cache_lock = threading.Lock()


class MarketDataError(RuntimeError):
    """Raised when a provider returns nothing usable for a ticker."""


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    index = pd.to_datetime(pd.Index(df.index), errors="coerce")
    df.index = pd.Index([ts.date() for ts in index], name="date")
    df = df[~pd.isna(pd.Index(df.index))]

    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise MarketDataError(f"provider response is missing columns: {sorted(missing)}")
    if "volume" not in df.columns:
        df["volume"] = np.nan

    return df.sort_index()


def bars(
    ticker: str,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    provider: str = "yfinance",
    lookback_days: int = 420,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Daily OHLCV bars for `ticker`, oldest first."""
    ticker = ticker.strip().upper()
    end = end or date.today()
    start = start or (end - timedelta(days=lookback_days))
    key = (ticker, start, end, provider)

    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_SECONDS:
                return hit[1].copy()

    from openbb import obb  # imported lazily; the first import is slow

    try:
        response = obb.equity.price.historical(
            ticker,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            provider=provider,
        )
        frame = _normalise(response.to_dataframe())
    except MarketDataError:
        raise
    except Exception as exc:  # provider errors are wide and undocumented
        raise MarketDataError(f"could not load bars for {ticker}: {exc}") from exc

    if frame.empty:
        raise MarketDataError(f"no bars returned for {ticker} between {start} and {end}")

    with _cache_lock:
        _cache[key] = (time.monotonic(), frame.copy())
    return frame


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def latest_bar(ticker: str, *, provider: str = "yfinance") -> tuple[date, float]:
    """Most recent session date and its close."""
    frame = bars(ticker, provider=provider, lookback_days=30)
    return frame.index[-1], float(frame["close"].iloc[-1])


def last_prices(tickers: list[str], *, provider: str = "yfinance") -> dict[str, float]:
    """Latest close per ticker; tickers that fail to load are omitted."""
    prices: dict[str, float] = {}
    for ticker in tickers:
        try:
            _, price = latest_bar(ticker, provider=provider)
            prices[ticker.upper()] = price
        except MarketDataError:
            continue
    return prices


def close_on_or_before(frame: pd.DataFrame, day: date) -> Optional[tuple[date, float]]:
    """The last session at or before `day`."""
    eligible = [d for d in frame.index if d <= day]
    if not eligible:
        return None
    session = max(eligible)
    return session, float(frame.loc[session, "close"])


def close_after_sessions(
    frame: pd.DataFrame, as_of: date, sessions: int
) -> Optional[tuple[date, float]]:
    """Close `sessions` trading days after the session at or before `as_of`.

    Returns None when the market has not yet produced that bar, which is what
    keeps an unresolved prediction open instead of grading it early.
    """
    anchor = close_on_or_before(frame, as_of)
    if anchor is None:
        return None
    dates = list(frame.index)
    position = dates.index(anchor[0]) + sessions
    if position >= len(dates):
        return None
    session = dates[position]
    return session, float(frame.loc[session, "close"])


# --------------------------------------------------------------- indicators


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0 * (avg_gain > 0))


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    high, low, close = frame["high"], frame["low"], frame["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _safe(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(number) else round(number, 4)


@dataclass
class Snapshot:
    """A compact, model-readable view of one ticker.

    Deliberately pre-computed: an LLM agent reasoning about RSI is useful, an
    LLM agent computing RSI from raw bars is a liability.
    """

    ticker: str
    as_of: date
    close: float
    sma_20: Optional[float]
    sma_50: Optional[float]
    sma_200: Optional[float]
    rsi_14: Optional[float]
    atr_14: Optional[float]
    atr_pct: Optional[float]
    annualised_vol: Optional[float]
    return_5d: Optional[float]
    return_21d: Optional[float]
    return_63d: Optional[float]
    high_52w: Optional[float]
    low_52w: Optional[float]
    pct_of_52w_range: Optional[float]
    avg_volume_20d: Optional[float]
    trend: str

    def to_dict(self) -> dict:
        payload = {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "close": round(self.close, 4),
            "trend": self.trend,
        }
        for key in (
            "sma_20", "sma_50", "sma_200", "rsi_14", "atr_14", "atr_pct",
            "annualised_vol", "return_5d", "return_21d", "return_63d",
            "high_52w", "low_52w", "pct_of_52w_range", "avg_volume_20d",
        ):
            payload[key] = getattr(self, key)
        return payload


def _pct_change_over(close: pd.Series, sessions: int) -> Optional[float]:
    if len(close) <= sessions:
        return None
    return _safe(close.iloc[-1] / close.iloc[-1 - sessions] - 1.0)


def snapshot(ticker: str, *, provider: str = "yfinance") -> Snapshot:
    """Indicator bundle for one ticker, computed from ~14 months of bars."""
    frame = bars(ticker, provider=provider, lookback_days=430)
    close = frame["close"].astype(float)

    sma_20 = _safe(close.rolling(20).mean().iloc[-1])
    sma_50 = _safe(close.rolling(50).mean().iloc[-1])
    sma_200 = _safe(close.rolling(200).mean().iloc[-1])
    last = float(close.iloc[-1])

    window = close.tail(252)
    high_52w = _safe(window.max())
    low_52w = _safe(window.min())
    span = (high_52w - low_52w) if (high_52w is not None and low_52w is not None) else None
    pct_of_range = _safe((last - low_52w) / span) if span else None

    atr_value = _safe(atr(frame).iloc[-1])
    daily_returns = close.pct_change().dropna().tail(63)
    vol = _safe(daily_returns.std() * np.sqrt(252)) if len(daily_returns) > 5 else None

    if sma_50 is not None and sma_200 is not None:
        if last > sma_50 > sma_200:
            trend = "uptrend"
        elif last < sma_50 < sma_200:
            trend = "downtrend"
        else:
            trend = "mixed"
    else:
        trend = "insufficient-history"

    return Snapshot(
        ticker=ticker.upper(),
        as_of=frame.index[-1],
        close=last,
        sma_20=sma_20,
        sma_50=sma_50,
        sma_200=sma_200,
        rsi_14=_safe(rsi(close).iloc[-1]),
        atr_14=atr_value,
        atr_pct=_safe(atr_value / last) if atr_value else None,
        annualised_vol=vol,
        return_5d=_pct_change_over(close, 5),
        return_21d=_pct_change_over(close, 21),
        return_63d=_pct_change_over(close, 63),
        high_52w=high_52w,
        low_52w=low_52w,
        pct_of_52w_range=pct_of_range,
        avg_volume_20d=_safe(frame["volume"].tail(20).mean()),
        trend=trend,
    )
