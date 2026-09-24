"""Trade execution: a paper (simulated) executor and a live ccxt executor."""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from typing import Dict, Mapping

from .strategy import Opportunity

log = logging.getLogger(__name__)

Balances = Dict[str, Dict[str, float]]  # exchange -> asset -> free amount


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    realized_pnl: float
    detail: str
    # True when one leg filled and the other did not: we now hold unhedged
    # inventory and the bot must stop until a human looks at it.
    imbalanced: bool = False


class PaperExecutor:
    """Fills every order exactly as simulated against the order book."""

    def __init__(self, starting_balances: Mapping[str, Mapping[str, float]]):
        self._balances: Balances = {ex: dict(assets) for ex, assets in starting_balances.items()}

    async def balances(self) -> Balances:
        return copy.deepcopy(self._balances)

    async def execute(self, opp: Opportunity) -> ExecutionResult:
        buy = self._balances.setdefault(opp.buy_exchange, {})
        sell = self._balances.setdefault(opp.sell_exchange, {})
        if buy.get(opp.quote, 0.0) < opp.buy_cost:
            return ExecutionResult(False, 0.0, f"insufficient {opp.quote} on {opp.buy_exchange}")
        if sell.get(opp.base, 0.0) < opp.amount:
            return ExecutionResult(False, 0.0, f"insufficient {opp.base} on {opp.sell_exchange}")

        buy[opp.quote] -= opp.buy_cost
        buy[opp.base] = buy.get(opp.base, 0.0) + opp.amount
        sell[opp.base] -= opp.amount
        sell[opp.quote] = sell.get(opp.quote, 0.0) + opp.sell_proceeds
        pnl = opp.sell_proceeds - opp.buy_cost
        return ExecutionResult(True, pnl, "paper fill")


class LiveExecutor:
    """Sends both legs at once as immediate-or-cancel limit orders.

    Limit prices are the deepest book level our simulation needed, so an order
    can never fill worse than what we priced. If the book moved, IOC cancels the
    unfilled part instead of chasing the price.
    """

    def __init__(self, hub, fill_tolerance: float = 0.01):
        self._hub = hub
        self._fill_tolerance = fill_tolerance

    async def balances(self) -> Balances:
        return await self._hub.fetch_balances()

    async def _place(self, exchange: str, side: str, opp: Opportunity, price: float) -> dict:
        client = self._hub.client(exchange)
        order = await client.create_order(
            opp.symbol, "limit", side, opp.amount, price, {"timeInForce": "IOC"}
        )
        if order.get("status") not in ("closed", "canceled", "expired") and order.get("id"):
            order = await client.fetch_order(order["id"], opp.symbol)
        return order

    async def execute(self, opp: Opportunity) -> ExecutionResult:
        buy_res, sell_res = await asyncio.gather(
            self._place(opp.buy_exchange, "buy", opp, opp.buy_limit_price),
            self._place(opp.sell_exchange, "sell", opp, opp.sell_limit_price),
            return_exceptions=True,
        )
        bought = 0.0 if isinstance(buy_res, BaseException) else float(buy_res.get("filled") or 0)
        sold = 0.0 if isinstance(sell_res, BaseException) else float(sell_res.get("filled") or 0)
        errors = [f"{name}: {res!r}" for name, res in (("buy", buy_res), ("sell", sell_res))
                  if isinstance(res, BaseException)]

        if bought == 0 and sold == 0:
            return ExecutionResult(False, 0.0, "; ".join(errors) or "neither leg filled")

        buy_fee = self._hub.fee(opp.buy_exchange, opp.symbol)
        sell_fee = self._hub.fee(opp.sell_exchange, opp.symbol)
        buy_cost = float(buy_res.get("cost") or 0) * (1 + buy_fee) if bought else 0.0
        sell_proceeds = float(sell_res.get("cost") or 0) * (1 - sell_fee) if sold else 0.0

        if abs(bought - sold) > opp.amount * self._fill_tolerance:
            detail = (f"LEG IMBALANCE: bought {bought} on {opp.buy_exchange}, "
                      f"sold {sold} on {opp.sell_exchange}. {'; '.join(errors)}")
            log.error(detail)
            return ExecutionResult(False, sell_proceeds - buy_cost, detail, imbalanced=True)

        return ExecutionResult(True, sell_proceeds - buy_cost,
                               f"filled buy {bought} / sell {sold} (pnl uses configured fee rates)")
