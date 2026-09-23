"""Cross-exchange arbitrage detection.

The idea: if you can buy BTC on exchange A for less than you can sell it on
exchange B *after fees and slippage*, buy on A and sell on B at the same time.
Both legs trade from inventory already sitting on each exchange; nothing is
transferred between exchanges during a trade (transfers take minutes to hours,
by which point the price gap is gone).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .orderbook import OrderBook, simulate_fill


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    amount: float  # base currency
    buy_avg_price: float
    sell_avg_price: float
    buy_limit_price: float
    sell_limit_price: float
    buy_cost: float  # quote spent, including taker fee
    sell_proceeds: float  # quote received, net of taker fee
    net_profit: float  # quote, after fees and safety buffer
    net_profit_pct: float

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]

    @property
    def quote(self) -> str:
        return self.symbol.split("/")[1]


def evaluate(
    buy_book: OrderBook,
    sell_book: OrderBook,
    amount: float,
    buy_fee: float,
    sell_fee: float,
    slippage_buffer_pct: float = 0.0,
) -> Optional[Opportunity]:
    """Price buying ``amount`` on ``buy_book`` and selling it on ``sell_book``.

    Fees are fractions (0.001 = 0.1%). ``slippage_buffer_pct`` is a percentage
    haircut on the buy cost to account for the book moving before our orders
    land. Returns None if either book is too thin to fill ``amount``.
    """
    if buy_book.symbol != sell_book.symbol or buy_book.exchange == sell_book.exchange:
        return None
    buy = simulate_fill(buy_book.asks, amount)
    sell = simulate_fill(sell_book.bids, amount)
    if buy is None or sell is None:
        return None

    buy_cost = buy.quote * (1 + buy_fee)
    sell_proceeds = sell.quote * (1 - sell_fee)
    buffer = buy_cost * slippage_buffer_pct / 100
    net = sell_proceeds - buy_cost - buffer
    return Opportunity(
        symbol=buy_book.symbol,
        buy_exchange=buy_book.exchange,
        sell_exchange=sell_book.exchange,
        amount=amount,
        buy_avg_price=buy.avg_price,
        sell_avg_price=sell.avg_price,
        buy_limit_price=buy.worst_price,
        sell_limit_price=sell.worst_price,
        buy_cost=buy_cost,
        sell_proceeds=sell_proceeds,
        net_profit=net,
        net_profit_pct=net / buy_cost * 100,
    )


def max_affordable_amount(
    buy_book: OrderBook,
    buy_fee: float,
    quote_available: float,
    base_available: float,
    max_trade_quote: float,
) -> float:
    """Largest base amount we can trade given inventory on both exchanges.

    Sized off the best ask with a 1% cushion so that walking deeper into the
    book cannot overspend the quote balance.
    """
    ask = buy_book.best_ask
    if not ask:
        return 0.0
    budget = min(quote_available, max_trade_quote)
    by_quote = budget / (ask * 1.01 * (1 + buy_fee))
    return max(0.0, min(by_quote, base_available))
