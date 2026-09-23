"""Order book model and depth-aware fill simulation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

Level = Tuple[float, float]  # (price, amount in base currency)


@dataclass(frozen=True)
class OrderBook:
    exchange: str
    symbol: str
    bids: Sequence[Level]  # sorted best (highest) first
    asks: Sequence[Level]  # sorted best (lowest) first
    timestamp: float  # unix seconds when the book was observed

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None


@dataclass(frozen=True)
class Fill:
    amount: float  # base filled
    quote: float  # quote value before fees
    avg_price: float
    worst_price: float  # deepest level touched; used as the limit price


def simulate_fill(levels: Sequence[Level], amount: float) -> Optional[Fill]:
    """Walk the book to fill ``amount`` of base currency.

    Returns None when the visible depth cannot fill the whole amount, so we
    never assume liquidity that is not actually there.
    """
    if amount <= 0:
        return None
    remaining = amount
    quote = 0.0
    worst = None
    for price, size in levels:
        if size <= 0:
            continue
        take = min(remaining, size)
        quote += take * price
        remaining -= take
        worst = price
        if remaining <= 1e-12:
            return Fill(amount=amount, quote=quote, avg_price=quote / amount, worst_price=worst)
    return None
