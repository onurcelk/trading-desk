"""SQLite persistence for predictions, fills and the paper book.

A connection is opened per operation rather than held open, so the store is
safe to share across the MCP server's worker threads without extra locking.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .domain import (
    Direction,
    Fill,
    Position,
    Prediction,
    PredictionStatus,
    Side,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT    NOT NULL,
    direction         TEXT    NOT NULL,
    confidence        REAL    NOT NULL,
    horizon_sessions  INTEGER NOT NULL,
    target_return     REAL,
    rationale         TEXT    NOT NULL,
    agent             TEXT    NOT NULL,
    as_of             TEXT    NOT NULL,
    entry_price       REAL    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'open',
    resolved_at       TEXT,
    exit_price        REAL,
    realized_return   REAL,
    correct           INTEGER,
    brier             REAL,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_agent  ON predictions(agent);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    quantity        INTEGER NOT NULL,
    price           REAL    NOT NULL,
    reference_price REAL    NOT NULL,
    commission      REAL    NOT NULL,
    agent           TEXT    NOT NULL,
    note            TEXT    NOT NULL DEFAULT '',
    filled_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);
CREATE INDEX IF NOT EXISTS idx_fills_at     ON fills(filled_at);

CREATE TABLE IF NOT EXISTS book (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker  TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS equity_curve (
    as_of  TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    agent      TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _as_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


class Store:
    def __init__(self, db_path: Path, starting_cash: float) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.starting_cash = starting_cash
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT value FROM book WHERE key='cash'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO book(key, value) VALUES ('cash', ?), ('starting_cash', ?)",
                    (str(self.starting_cash), str(self.starting_cash)),
                )

    # ------------------------------------------------------------------ book

    def cash(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM book WHERE key='cash'").fetchone()
            return float(row["value"])

    def initial_cash(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM book WHERE key='starting_cash'").fetchone()
            return float(row["value"]) if row else self.starting_cash

    def _set_cash(self, conn: sqlite3.Connection, amount: float) -> None:
        conn.execute("UPDATE book SET value=? WHERE key='cash'", (str(amount),))

    # ----------------------------------------------------------- predictions

    def add_prediction(self, prediction: Prediction) -> Prediction:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO predictions
                   (ticker, direction, confidence, horizon_sessions, target_return,
                    rationale, agent, as_of, entry_price, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prediction.ticker,
                    prediction.direction.value,
                    prediction.confidence,
                    prediction.horizon_sessions,
                    prediction.target_return,
                    prediction.rationale,
                    prediction.agent,
                    prediction.as_of.isoformat(),
                    prediction.entry_price,
                    prediction.status.value,
                    prediction.created_at.isoformat(),
                ),
            )
            prediction.id = cursor.lastrowid
        return prediction

    def update_prediction(self, prediction: Prediction) -> None:
        if prediction.id is None:
            raise ValueError("cannot update a prediction with no id")
        with self._connect() as conn:
            conn.execute(
                """UPDATE predictions SET
                     status=?, resolved_at=?, exit_price=?, realized_return=?,
                     correct=?, brier=?
                   WHERE id=?""",
                (
                    prediction.status.value,
                    prediction.resolved_at.isoformat() if prediction.resolved_at else None,
                    prediction.exit_price,
                    prediction.realized_return,
                    None if prediction.correct is None else int(prediction.correct),
                    prediction.brier,
                    prediction.id,
                ),
            )

    @staticmethod
    def _row_to_prediction(row: sqlite3.Row) -> Prediction:
        prediction = Prediction(
            id=row["id"],
            ticker=row["ticker"],
            direction=Direction(row["direction"]),
            confidence=row["confidence"],
            horizon_sessions=row["horizon_sessions"],
            target_return=row["target_return"],
            rationale=row["rationale"],
            agent=row["agent"],
            as_of=date.fromisoformat(row["as_of"]),
            entry_price=row["entry_price"],
            status=PredictionStatus(row["status"]),
            resolved_at=_as_date(row["resolved_at"]),
            exit_price=row["exit_price"],
            realized_return=row["realized_return"],
            correct=None if row["correct"] is None else bool(row["correct"]),
            brier=row["brier"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        return prediction

    def predictions(
        self,
        *,
        status: Optional[PredictionStatus] = None,
        agent: Optional[str] = None,
        ticker: Optional[str] = None,
        since: Optional[date] = None,
        limit: int = 500,
    ) -> list[Prediction]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if ticker:
            clauses.append("ticker = ?")
            params.append(ticker.upper())
        if since:
            clauses.append("as_of >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM predictions {where} ORDER BY as_of DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_prediction(r) for r in rows]

    def prediction(self, prediction_id: int) -> Optional[Prediction]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM predictions WHERE id=?", (prediction_id,)
            ).fetchone()
        return self._row_to_prediction(row) if row else None

    def has_prediction(self, ticker: str, agent: str, as_of: date) -> bool:
        """Whether this exact call already exists — used to keep backfills idempotent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM predictions WHERE ticker=? AND agent=? AND as_of=? LIMIT 1",
                (ticker.upper(), agent, as_of.isoformat()),
            ).fetchone()
        return row is not None

    def agents_with_predictions(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT agent FROM predictions ORDER BY agent").fetchall()
        return [r["agent"] for r in rows]

    # ----------------------------------------------------------------- fills

    def record_fill(self, fill: Fill) -> Fill:
        """Persist a fill and move cash atomically."""
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM book WHERE key='cash'").fetchone()
            cash = float(row["value"])
            new_cash = cash + fill.cash_delta
            if new_cash < 0:
                raise ValueError(
                    f"fill would overdraw the book: cash {cash:,.2f}, delta {fill.cash_delta:,.2f}"
                )
            cursor = conn.execute(
                """INSERT INTO fills
                   (ticker, side, quantity, price, reference_price, commission,
                    agent, note, filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    fill.ticker,
                    fill.side.value,
                    fill.quantity,
                    fill.price,
                    fill.reference_price,
                    fill.commission,
                    fill.agent,
                    fill.note,
                    fill.filled_at.isoformat(),
                ),
            )
            fill.id = cursor.lastrowid
            self._set_cash(conn, new_cash)
        return fill

    def fills(self, *, ticker: Optional[str] = None, limit: int = 500) -> list[Fill]:
        clause = "WHERE ticker = ?" if ticker else ""
        params: list[object] = [ticker.upper()] if ticker else []
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM fills {clause} ORDER BY filled_at ASC, id ASC LIMIT ?", params
            ).fetchall()
        return [
            Fill(
                id=r["id"],
                ticker=r["ticker"],
                side=Side(r["side"]),
                quantity=r["quantity"],
                price=r["price"],
                reference_price=r["reference_price"],
                commission=r["commission"],
                agent=r["agent"],
                note=r["note"],
                filled_at=datetime.fromisoformat(r["filled_at"]),
            )
            for r in rows
        ]

    def fills_on(self, day: date) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM fills WHERE substr(filled_at, 1, 10) = ?",
                (day.isoformat(),),
            ).fetchone()
        return int(row["n"])

    def positions(self) -> dict[str, Position]:
        """Rebuild positions by replaying every fill in order.

        Replay rather than a maintained balance table: fills are the ledger, so
        positions can never drift out of sync with it.
        """
        positions: dict[str, Position] = {}
        for fill in self.fills(limit=1_000_000):
            position = positions.setdefault(fill.ticker, Position(ticker=fill.ticker))
            position.apply(fill)
        return positions

    def open_positions(self) -> dict[str, Position]:
        return {t: p for t, p in self.positions().items() if p.quantity > 0}

    # ------------------------------------------------------------- watchlist

    def watchlist(self, fallback: Iterable[str] = ()) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
        tickers = [r["ticker"] for r in rows]
        if tickers:
            return tickers
        seeded = sorted({t.upper() for t in fallback})
        if seeded:
            self.add_to_watchlist(seeded, added_by="config")
        return seeded

    def add_to_watchlist(self, tickers: Iterable[str], added_by: str = "agent") -> list[str]:
        from .domain import utc_now

        cleaned = sorted({t.strip().upper() for t in tickers if t.strip()})
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO watchlist(ticker, added_at, added_by) VALUES (?,?,?)",
                [(t, utc_now().isoformat(), added_by) for t in cleaned],
            )
        return cleaned

    def remove_from_watchlist(self, tickers: Iterable[str]) -> list[str]:
        cleaned = sorted({t.strip().upper() for t in tickers if t.strip()})
        with self._connect() as conn:
            conn.executemany("DELETE FROM watchlist WHERE ticker = ?", [(t,) for t in cleaned])
        return cleaned

    # ---------------------------------------------------------- equity curve

    def mark_equity(self, as_of: date, equity: float, cash: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity_curve(as_of, equity, cash) VALUES (?,?,?)",
                (as_of.isoformat(), equity, cash),
            )

    def equity_curve(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM equity_curve ORDER BY as_of ASC").fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- reports

    def record_report(self, title: str, agent: str, path: Path) -> int:
        from .domain import utc_now

        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reports(title, agent, path, created_at) VALUES (?,?,?,?)",
                (title, agent, str(path), utc_now().isoformat()),
            )
            return int(cursor.lastrowid)

    def reports(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
