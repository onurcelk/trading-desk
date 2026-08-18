"""The paper broker: simulated execution against real closing prices.

Risk limits are enforced here rather than in agent prompts. An agent that asks
for an oversized position gets a refusal with a reason it can act on, which is
far more reliable than hoping the model does the arithmetic correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from . import data
from .config import Settings
from .domain import Fill, PortfolioSnapshot, Position, Side, utc_now, utc_today
from .store import Store


class RiskRejection(Exception):
    """An order that a limit refused. The message is written for the agent."""


@dataclass
class OrderResult:
    fill: Fill
    portfolio: PortfolioSnapshot

    def to_dict(self) -> dict:
        return {
            "status": "filled",
            "ticker": self.fill.ticker,
            "side": self.fill.side.value,
            "quantity": self.fill.quantity,
            "fill_price": round(self.fill.price, 4),
            "reference_close": round(self.fill.reference_price, 4),
            "commission": round(self.fill.commission, 2),
            "cash_after": round(self.portfolio.cash, 2),
            "equity_after": round(self.portfolio.equity, 2),
            "note": self.fill.note,
        }


class PaperBroker:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------- pricing

    def _reference_price(self, ticker: str) -> float:
        _, price = data.latest_bar(ticker, provider=self.settings.openbb_provider)
        return price

    def _fill_price(self, reference: float, side: Side) -> float:
        """Slippage always works against the desk."""
        drift = reference * (self.settings.costs.slippage_bps / 10_000.0)
        return reference + drift if side is Side.BUY else reference - drift

    def _commission(self, quantity: int) -> float:
        costs = self.settings.costs
        return max(costs.commission_minimum, costs.commission_per_share * quantity)

    # ----------------------------------------------------------- portfolio

    def portfolio(self, prices: Optional[dict[str, float]] = None) -> PortfolioSnapshot:
        positions = self.store.open_positions()
        if prices is None:
            prices = data.last_prices(
                list(positions), provider=self.settings.openbb_provider
            )

        cash = self.store.cash()
        rows: list[dict] = []
        market_value = 0.0
        unrealized = 0.0
        realized = sum(p.realized_pnl for p in self.store.positions().values())

        for ticker, position in sorted(positions.items()):
            # A stale average cost is a better mark than pretending the
            # position is worthless when a provider lookup fails.
            last = prices.get(ticker, position.average_cost)
            value = position.market_value(last)
            pnl = position.unrealized_pnl(last)
            market_value += value
            unrealized += pnl
            rows.append(
                {
                    "ticker": ticker,
                    "quantity": position.quantity,
                    "average_cost": round(position.average_cost, 4),
                    "last_price": round(last, 4),
                    "market_value": round(value, 2),
                    "unrealized_pnl": round(pnl, 2),
                    "unrealized_pct": round(
                        (last / position.average_cost - 1.0) if position.average_cost else 0.0, 4
                    ),
                    "priced": ticker in prices,
                }
            )

        equity = cash + market_value
        for row in rows:
            row["weight"] = round(row["market_value"] / equity, 4) if equity else 0.0

        return PortfolioSnapshot(
            as_of=utc_now(),
            cash=cash,
            positions=rows,
            market_value=market_value,
            equity=equity,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
        )

    def performance(self) -> dict:
        snapshot = self.portfolio()
        initial = self.store.initial_cash()
        curve = self.store.equity_curve()
        equities = [row["equity"] for row in curve] or [snapshot.equity]

        peak = equities[0]
        max_drawdown = 0.0
        for value in equities:
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = min(max_drawdown, value / peak - 1.0)

        return {
            "starting_cash": round(initial, 2),
            "equity": round(snapshot.equity, 2),
            "cash": round(snapshot.cash, 2),
            "total_return": round(snapshot.equity / initial - 1.0, 4) if initial else None,
            "realized_pnl": round(snapshot.realized_pnl, 2),
            "unrealized_pnl": round(snapshot.unrealized_pnl, 2),
            "gross_exposure_pct": round(snapshot.gross_exposure_pct, 4),
            "max_drawdown": round(max_drawdown, 4),
            "marks_recorded": len(curve),
            "open_positions": len(snapshot.positions),
        }

    # ---------------------------------------------------------- risk gates

    def _check_buy(
        self, ticker: str, quantity: int, fill_price: float, commission: float
    ) -> None:
        limits = self.settings.risk
        snapshot = self.portfolio()
        cost = quantity * fill_price + commission

        if cost > snapshot.cash:
            raise RiskRejection(
                f"insufficient cash: order costs {cost:,.2f} but only {snapshot.cash:,.2f} is available"
            )

        # Equity is unchanged by a buy (cash converts to shares), so the
        # post-trade denominator is today's equity minus commission drag.
        equity_after = snapshot.equity - commission
        if equity_after <= 0:
            raise RiskRejection("equity is exhausted; no further buying is possible")

        held = self.store.open_positions().get(ticker)
        existing_value = 0.0
        if held:
            existing_value = held.quantity * fill_price
        position_value_after = existing_value + quantity * fill_price
        position_pct = position_value_after / equity_after
        if position_pct > limits.max_position_pct:
            raise RiskRejection(
                f"position limit: {ticker} would be {position_pct:.1%} of equity, "
                f"limit is {limits.max_position_pct:.1%}"
            )

        gross_after = snapshot.market_value + quantity * fill_price
        if gross_after / equity_after > limits.max_gross_exposure_pct:
            raise RiskRejection(
                f"gross exposure limit: book would be {gross_after / equity_after:.1%} invested, "
                f"limit is {limits.max_gross_exposure_pct:.1%}"
            )

        cash_after = snapshot.cash - cost
        if cash_after / equity_after < limits.min_cash_pct:
            raise RiskRejection(
                f"cash floor: {cash_after / equity_after:.1%} cash would remain, "
                f"minimum is {limits.min_cash_pct:.1%}"
            )

        filled_today = self.store.fills_on(utc_today())
        if filled_today >= limits.max_orders_per_day:
            raise RiskRejection(
                f"daily order limit reached ({filled_today}/{limits.max_orders_per_day})"
            )

    def max_affordable_shares(self, ticker: str) -> dict:
        """How many shares of `ticker` the limits currently permit buying."""
        limits = self.settings.risk
        snapshot = self.portfolio()
        try:
            reference = self._reference_price(ticker)
        except data.MarketDataError as exc:
            raise RiskRejection(str(exc)) from exc
        price = self._fill_price(reference, Side.BUY)

        held = self.store.open_positions().get(ticker.upper())
        existing_value = held.quantity * price if held else 0.0

        by_position = (limits.max_position_pct * snapshot.equity - existing_value) / price
        by_gross = (
            limits.max_gross_exposure_pct * snapshot.equity - snapshot.market_value
        ) / price
        by_cash = (snapshot.cash - limits.min_cash_pct * snapshot.equity) / price

        shares = int(max(0, min(by_position, by_gross, by_cash)))
        binding = min(
            (("position_limit", by_position), ("gross_exposure", by_gross), ("cash_floor", by_cash)),
            key=lambda pair: pair[1],
        )[0]

        return {
            "ticker": ticker.upper(),
            "estimated_fill_price": round(price, 4),
            "max_shares": shares,
            "max_notional": round(shares * price, 2),
            "binding_constraint": binding,
            "equity": round(snapshot.equity, 2),
            "cash": round(snapshot.cash, 2),
        }

    # ------------------------------------------------------------- trading

    def submit(
        self,
        ticker: str,
        side: str,
        quantity: int,
        *,
        agent: str,
        note: str = "",
    ) -> OrderResult:
        """Execute a market order at the latest close, adjusted for slippage."""
        ticker = ticker.strip().upper()
        parsed_side = Side.parse(side)
        quantity = int(quantity)
        if quantity <= 0:
            raise RiskRejection("quantity must be a positive whole number of shares")

        try:
            reference = self._reference_price(ticker)
        except data.MarketDataError as exc:
            raise RiskRejection(str(exc)) from exc

        price = self._fill_price(reference, parsed_side)
        commission = self._commission(quantity)

        if parsed_side is Side.BUY:
            self._check_buy(ticker, quantity, price, commission)
        else:
            held = self.store.open_positions().get(ticker)
            available = held.quantity if held else 0
            if quantity > available:
                raise RiskRejection(
                    f"cannot sell {quantity} {ticker}; position is {available} shares "
                    "(shorting is not supported)"
                )

        fill = Fill(
            ticker=ticker,
            side=parsed_side,
            quantity=quantity,
            price=price,
            reference_price=reference,
            commission=commission,
            agent=agent,
            note=note,
        )
        self.store.record_fill(fill)

        snapshot = self.portfolio()
        self.store.mark_equity(utc_today(), snapshot.equity, snapshot.cash)
        return OrderResult(fill=fill, portfolio=snapshot)

    def close_position(self, ticker: str, *, agent: str, note: str = "") -> OrderResult:
        ticker = ticker.strip().upper()
        held = self.store.open_positions().get(ticker)
        if not held or held.quantity <= 0:
            raise RiskRejection(f"no open position in {ticker}")
        return self.submit(ticker, "sell", held.quantity, agent=agent, note=note or "close position")

    def mark_to_market(self) -> dict:
        """Record today's equity mark. Intended for the end-of-day heartbeat."""
        snapshot = self.portfolio()
        self.store.mark_equity(utc_today(), snapshot.equity, snapshot.cash)
        return {
            "as_of": utc_today().isoformat(),
            "equity": round(snapshot.equity, 2),
            "cash": round(snapshot.cash, 2),
            "positions": len(snapshot.positions),
        }
