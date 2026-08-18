"""Offline tests for the desk's accounting, grading and risk logic.

Market data is stubbed throughout: these tests must pass with no network and
no provider credentials.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest

from tradingdesk import data
from tradingdesk.backtest import Backtester
from tradingdesk.config import Costs, RiskLimits, Settings
from tradingdesk.domain import (
    FLAT_BAND,
    Direction,
    Fill,
    Position,
    Prediction,
    PredictionStatus,
    Side,
    estimate_resolution_date,
    utc_today,
)
from tradingdesk.paper import PaperBroker, RiskRejection
from tradingdesk.scoring import Scorer, signed_return
from tradingdesk.store import Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "desk.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=100_000.0,
        watchlist=["AAA", "BBB"],
        risk=RiskLimits(
            max_position_pct=0.20,
            max_gross_exposure_pct=1.00,
            min_cash_pct=0.05,
            max_orders_per_day=5,
        ),
        costs=Costs(commission_per_share=0.01, commission_minimum=1.0, slippage_bps=10.0),
        mcp_host="127.0.0.1",
        mcp_port=8010,
        openbb_provider="stub",
        max_horizon_days=60,
    )


@pytest.fixture
def store(settings) -> Store:
    return Store(settings.db_path, settings.starting_cash)


@pytest.fixture
def broker(store, settings, monkeypatch) -> PaperBroker:
    prices = {"AAA": 100.0, "BBB": 50.0}
    monkeypatch.setattr(
        data, "latest_bar", lambda ticker, provider=None: (date(2026, 8, 14), prices[ticker.upper()])
    )
    monkeypatch.setattr(
        data,
        "last_prices",
        lambda tickers, provider=None: {t.upper(): prices[t.upper()] for t in tickers},
    )
    return PaperBroker(store, settings)


# ------------------------------------------------------------------ domain


def test_position_average_cost_and_realised_pnl():
    position = Position(ticker="AAA")
    position.apply(Fill("AAA", Side.BUY, 10, 100.0, 1.0, 100.0))
    position.apply(Fill("AAA", Side.BUY, 10, 120.0, 1.0, 120.0))
    assert position.quantity == 20
    assert position.average_cost == pytest.approx(110.0)

    position.apply(Fill("AAA", Side.SELL, 5, 130.0, 1.0, 130.0))
    assert position.quantity == 15
    assert position.realized_pnl == pytest.approx(100.0)  # (130 - 110) * 5
    assert position.average_cost == pytest.approx(110.0)  # unchanged by a sale


def test_position_rejects_oversell():
    position = Position(ticker="AAA", quantity=5, average_cost=100.0)
    with pytest.raises(ValueError, match="does not support short"):
        position.apply(Fill("AAA", Side.SELL, 6, 100.0, 1.0, 100.0))


def test_fill_cash_delta_includes_commission():
    buy = Fill("AAA", Side.BUY, 10, 100.0, 5.0, 100.0)
    assert buy.cash_delta == pytest.approx(-1005.0)
    sell = Fill("AAA", Side.SELL, 10, 100.0, 5.0, 100.0)
    assert sell.cash_delta == pytest.approx(995.0)


def _prediction(direction: Direction, confidence: float = 0.6, entry: float = 100.0) -> Prediction:
    return Prediction(
        ticker="AAA",
        direction=direction,
        confidence=confidence,
        horizon_sessions=10,
        rationale="because",
        agent="analyst",
        as_of=date(2026, 7, 1),
        entry_price=entry,
    )


def test_grade_up_call_that_wins():
    prediction = _prediction(Direction.UP, confidence=0.8).grade(110.0)
    assert prediction.correct is True
    assert prediction.realized_return == pytest.approx(0.10)
    assert prediction.brier == pytest.approx(0.04)  # (0.8 - 1)^2
    assert prediction.status is PredictionStatus.RESOLVED


def test_grade_up_call_inside_flat_band_is_a_miss():
    """A move smaller than the band is 'flat', so a directional call is wrong."""
    prediction = _prediction(Direction.UP).grade(100.0 * (1 + FLAT_BAND / 2))
    assert prediction.correct is False


def test_grade_flat_call_that_wins():
    prediction = _prediction(Direction.FLAT, confidence=0.7).grade(100.5)
    assert prediction.correct is True
    assert prediction.brier == pytest.approx(0.09)


def test_prediction_validation():
    with pytest.raises(ValueError, match="confidence"):
        _prediction(Direction.UP, confidence=1.5)
    with pytest.raises(ValueError, match="rationale"):
        Prediction(
            ticker="AAA",
            direction=Direction.UP,
            confidence=0.6,
            horizon_sessions=5,
            rationale="   ",
            agent="a",
            as_of=date(2026, 7, 1),
            entry_price=10.0,
        )


def test_signed_return_flips_for_down_calls():
    down = _prediction(Direction.DOWN).grade(90.0)
    assert signed_return(down) == pytest.approx(0.10)
    flat = _prediction(Direction.FLAT).grade(103.0)
    assert signed_return(flat) == pytest.approx(-0.03)


def test_estimated_resolution_leaves_room_for_weekends():
    assert estimate_resolution_date(date(2026, 7, 1), 10) == date(2026, 7, 1) + timedelta(days=15)


# ------------------------------------------------------------------- store


def test_fills_move_cash_and_rebuild_positions(store):
    store.record_fill(Fill("AAA", Side.BUY, 10, 100.0, 1.0, 100.0, agent="t"))
    assert store.cash() == pytest.approx(100_000.0 - 1001.0)

    store.record_fill(Fill("AAA", Side.SELL, 4, 120.0, 1.0, 120.0, agent="t"))
    assert store.cash() == pytest.approx(100_000.0 - 1001.0 + 479.0)

    positions = store.open_positions()
    assert positions["AAA"].quantity == 6
    assert positions["AAA"].realized_pnl == pytest.approx(80.0)


def test_store_refuses_to_overdraw(store):
    with pytest.raises(ValueError, match="overdraw"):
        store.record_fill(Fill("AAA", Side.BUY, 100_000, 100.0, 1.0, 100.0, agent="t"))


def test_prediction_roundtrip_preserves_fields(store):
    saved = store.add_prediction(_prediction(Direction.DOWN, confidence=0.75))
    loaded = store.prediction(saved.id)
    assert loaded.direction is Direction.DOWN
    assert loaded.confidence == pytest.approx(0.75)
    assert loaded.agent == "analyst"
    assert loaded.status is PredictionStatus.OPEN


def test_has_prediction_makes_backfill_idempotent(store):
    prediction = _prediction(Direction.UP)
    assert store.has_prediction("AAA", "analyst", prediction.as_of) is False
    store.add_prediction(prediction)
    assert store.has_prediction("aaa", "analyst", prediction.as_of) is True
    assert store.has_prediction("AAA", "someone-else", prediction.as_of) is False


def test_watchlist_seeds_from_fallback_then_persists(store):
    assert store.watchlist(fallback=["ccc", "aaa"]) == ["AAA", "CCC"]
    assert store.watchlist() == ["AAA", "CCC"]
    store.remove_from_watchlist(["AAA"])
    assert store.watchlist() == ["CCC"]


# -------------------------------------------------------------- execution


def test_buy_applies_slippage_and_commission(broker):
    result = broker.submit("AAA", "buy", 10, agent="trader")
    # 10 bps of slippage, against the desk
    assert result.fill.price == pytest.approx(100.10)
    assert result.fill.commission == pytest.approx(1.0)  # max(1.00, 0.01 * 10)
    assert result.portfolio.cash == pytest.approx(100_000.0 - 1002.0)


def test_sell_slips_the_other_way(broker):
    broker.submit("AAA", "buy", 10, agent="trader")
    result = broker.submit("AAA", "sell", 10, agent="trader")
    assert result.fill.price == pytest.approx(99.90)


def test_position_limit_blocks_oversized_buy(broker):
    with pytest.raises(RiskRejection, match="position limit"):
        broker.submit("AAA", "buy", 300, agent="trader")  # ~30% of equity


def test_cash_floor_blocks_near_full_deployment(broker):
    """Position and gross-exposure limits both pass; only the cash floor bites."""
    relaxed = replace(
        broker.settings,
        risk=RiskLimits(
            max_position_pct=1.0,
            max_gross_exposure_pct=1.0,
            min_cash_pct=0.05,
            max_orders_per_day=50,
        ),
    )
    unconstrained = PaperBroker(broker.store, relaxed)

    unconstrained.submit("AAA", "buy", 900, agent="trader")  # ~90% deployed, 10% cash left
    with pytest.raises(RiskRejection, match="cash floor"):
        unconstrained.submit("AAA", "buy", 60, agent="trader")  # would leave ~4%


def test_cannot_short(broker):
    with pytest.raises(RiskRejection, match="shorting is not supported"):
        broker.submit("AAA", "sell", 1, agent="trader")


def test_quantity_must_be_positive(broker):
    with pytest.raises(RiskRejection, match="positive whole number"):
        broker.submit("AAA", "buy", 0, agent="trader")


def test_position_sizing_reports_binding_constraint(broker):
    sizing = broker.max_affordable_shares("AAA")
    assert sizing["max_shares"] == pytest.approx(199, abs=2)  # 20% of 100k at ~100.10
    assert sizing["binding_constraint"] == "position_limit"


def test_close_position_exits_fully(broker):
    broker.submit("AAA", "buy", 50, agent="trader")
    broker.close_position("AAA", agent="trader")
    assert "AAA" not in broker.store.open_positions()


def test_fills_are_counted_on_the_same_calendar_the_ledger_uses(store):
    """The daily order counter must agree with how fills are stamped.

    Fills are written with UTC timestamps, so counting them against a local
    date silently returns zero for the hours where the two calendars differ —
    and the daily order limit fails open. Regression test for exactly that.
    """
    store.record_fill(Fill("AAA", Side.BUY, 1, 100.0, 1.0, 100.0, agent="t"))
    assert store.fills_on(utc_today()) == 1


def test_daily_order_limit(broker):
    for _ in range(5):
        broker.submit("BBB", "buy", 10, agent="trader")
    with pytest.raises(RiskRejection, match="daily order limit"):
        broker.submit("BBB", "buy", 10, agent="trader")


# ---------------------------------------------------------------- scoring


def _stub_bars(monkeypatch, closes: list[float], start: date) -> None:
    """Install a deterministic bar series covering `closes` on consecutive days."""
    index = [start + timedelta(days=i) for i in range(len(closes))]
    frame = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(closes)},
        index=pd.Index(index, name="date"),
    )
    monkeypatch.setattr(data, "bars", lambda ticker, **kwargs: frame)


def test_resolve_due_grades_elapsed_calls(store, settings, monkeypatch):
    start = date.today() - timedelta(days=40)
    _stub_bars(monkeypatch, [100.0 + i for i in range(30)], start)

    prediction = _prediction(Direction.UP)
    prediction.as_of = start
    prediction.entry_price = 100.0
    prediction.horizon_sessions = 5
    store.add_prediction(prediction)

    result = Scorer(store, settings).resolve_due()
    assert result["resolved"] == 1
    graded = store.prediction(prediction.id)
    assert graded.status is PredictionStatus.RESOLVED
    assert graded.exit_price == pytest.approx(105.0)
    assert graded.correct is True


def test_resolve_leaves_immature_calls_open(store, settings, monkeypatch):
    start = date.today() - timedelta(days=40)
    _stub_bars(monkeypatch, [100.0, 101.0, 102.0], start)  # only 3 bars

    prediction = _prediction(Direction.UP)
    prediction.as_of = start
    prediction.horizon_sessions = 20
    store.add_prediction(prediction)

    result = Scorer(store, settings).resolve_due()
    assert result["resolved"] == 0
    assert result["still_open"] == 1
    assert store.prediction(prediction.id).status is PredictionStatus.OPEN


def test_scorecard_and_calibration(store, settings):
    for confidence, exit_price, agent in [
        (0.9, 110.0, "alpha"),
        (0.9, 112.0, "alpha"),
        (0.55, 95.0, "beta"),
    ]:
        prediction = _prediction(Direction.UP, confidence=confidence)
        prediction.agent = agent
        store.add_prediction(prediction)
        prediction.grade(exit_price)
        prediction.resolved_at = date.today()
        store.update_prediction(prediction)

    scorer = Scorer(store, settings)
    alpha = scorer.scorecard(agent="alpha")
    assert alpha.resolved == 2
    assert alpha.hit_rate == pytest.approx(1.0)
    assert alpha.edge == pytest.approx(0.11)

    board = scorer.leaderboard()
    assert [row["agent"] for row in board] == ["alpha", "beta"]

    desk = scorer.scorecard()
    bands = {row["confidence_band"]: row for row in desk.calibration}
    assert bands["90%-100%"]["actual_hit_rate"] == pytest.approx(1.0)
    assert bands["50%-60%"]["actual_hit_rate"] == pytest.approx(0.0)


def test_scorecard_handles_no_resolved_calls(store, settings):
    card = Scorer(store, settings).scorecard()
    assert card.resolved == 0
    assert card.hit_rate is None
    assert card.edge is None


# --------------------------------------------------------------- backtest


def test_replay_compounds_and_reports_baselines(store, settings):
    for direction, exit_price in [(Direction.UP, 110.0), (Direction.DOWN, 110.0)]:
        prediction = _prediction(direction)
        store.add_prediction(prediction)
        prediction.grade(exit_price)
        prediction.resolved_at = date.today()
        store.update_prediction(prediction)

    result = Backtester(store, settings).replay(position_pct=0.10, include_benchmark=False)
    assert result["trades"] == 2
    assert result["win_rate"] == pytest.approx(0.5)
    assert result["baselines"]["always_call_up"]["mean_return_per_call"] == pytest.approx(0.10)


def test_replay_rejects_bad_position_size(store, settings):
    with pytest.raises(ValueError, match="between 0 and 1"):
        Backtester(store, settings).replay(position_pct=1.5)


def test_replay_with_no_history_is_not_an_error(store, settings):
    result = Backtester(store, settings).replay()
    assert result["trades"] == 0
