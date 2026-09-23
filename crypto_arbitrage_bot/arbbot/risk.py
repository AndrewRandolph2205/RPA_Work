"""Risk controls. Every trade must pass these before it is sent."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .orderbook import OrderBook
from .strategy import Opportunity


@dataclass
class RiskLimits:
    min_profit_pct: float = 0.15
    min_profit_quote: float = 0.10
    min_trade_quote: float = 10.0
    max_book_age_s: float = 2.0
    max_daily_loss_quote: float = 25.0
    max_trades_per_day: int = 500
    max_consecutive_failures: int = 3


class RiskManager:
    def __init__(self, limits: RiskLimits, clock: Callable[[], float] = time.time):
        self.limits = limits
        self._clock = clock
        self._day = self._today()
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.consecutive_failures = 0
        self.halted_reason: Optional[str] = None

    def _today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self._clock(), tz=dt.timezone.utc).date()

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self.daily_pnl = 0.0
            self.daily_trades = 0

    def is_fresh(self, book: OrderBook) -> bool:
        return self._clock() - book.timestamp <= self.limits.max_book_age_s

    def halt(self, reason: str) -> None:
        self.halted_reason = reason

    def allow(self, opp: Opportunity) -> Tuple[bool, str]:
        self._roll_day()
        lim = self.limits
        if self.halted_reason:
            return False, f"halted: {self.halted_reason}"
        if self.daily_pnl <= -lim.max_daily_loss_quote:
            return False, "daily loss limit reached"
        if self.daily_trades >= lim.max_trades_per_day:
            return False, "daily trade limit reached"
        if opp.buy_cost < lim.min_trade_quote:
            return False, "trade below minimum size"
        if opp.net_profit_pct < lim.min_profit_pct:
            return False, "profit % below threshold"
        if opp.net_profit < lim.min_profit_quote:
            return False, "profit below minimum"
        return True, "ok"

    def record(self, pnl: float, success: bool) -> None:
        self._roll_day()
        self.daily_pnl += pnl
        self.daily_trades += 1
        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.limits.max_consecutive_failures:
                self.halt(f"{self.consecutive_failures} consecutive failed executions")
