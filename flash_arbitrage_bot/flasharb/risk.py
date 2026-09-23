"""Risk controls for on-chain execution.

With flash loans the trade itself can't lose principal (the contract reverts
instead), so the real risks are gas burnt on reverted transactions and a bot
that keeps firing into a broken setup. These limits cap both.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Callable, Optional

from .config import RiskLimits


class RiskManager:
    def __init__(self, limits: RiskLimits, clock: Callable[[], float] = time.time):
        self.limits = limits
        self._clock = clock
        self._day = self._today()
        self.daily_gas_burnt_usd = 0.0
        self.consecutive_reverts = 0
        self.halted_reason: Optional[str] = None

    def _today(self) -> dt.date:
        return dt.datetime.fromtimestamp(self._clock(), tz=dt.timezone.utc).date()

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self.daily_gas_burnt_usd = 0.0

    def gas_price_ok(self, gas_price_wei: int) -> bool:
        return gas_price_wei / 1e9 <= self.limits.max_gas_price_gwei

    def can_send(self) -> tuple[bool, str]:
        self._roll_day()
        if self.halted_reason:
            return False, f"halted: {self.halted_reason}"
        if self.daily_gas_burnt_usd >= self.limits.max_daily_gas_usd:
            return False, "daily reverted-gas budget used up"
        return True, "ok"

    def record_send(self, success: bool, gas_usd: float) -> None:
        self._roll_day()
        if success:
            self.consecutive_reverts = 0
            return
        self.daily_gas_burnt_usd += gas_usd
        self.consecutive_reverts += 1
        if self.consecutive_reverts >= self.limits.max_consecutive_reverts:
            self.halted_reason = f"{self.consecutive_reverts} reverted transactions in a row"
