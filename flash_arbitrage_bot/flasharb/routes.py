"""Cycle discovery, optimal sizing and USD valuation.

A route is a cycle token A -> ... -> A through 2+ pools. Borrow A with a flash
loan, run the swaps, and if more A comes back than was borrowed, the
difference is profit.

Sizing uses the fact that a chain of constant-product swaps composes into a
single function out(x) = A*x / (B + C*x). Profit out(x) - x is maximised at
x* = (sqrt(A*B) - B) / C, and any profit exists only when A > B.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .amm import FEE_DENOMINATOR, Pool


@dataclass(frozen=True)
class Route:
    pools: Tuple[Pool, ...]
    path: Tuple[str, ...]  # token addresses, path[0] == path[-1]

    @property
    def start(self) -> str:
        return self.path[0]

    def hops(self):
        """Yield (pool, token_in, token_out) per swap."""
        for i, pool in enumerate(self.pools):
            yield pool, self.path[i], self.path[i + 1]

    def amount_out(self, amount_in: int) -> int:
        amount = amount_in
        for pool, token_in, _ in self.hops():
            amount = pool.amount_out(token_in, amount)
        return amount

    def mobius(self) -> Optional[Tuple[float, float, float]]:
        """Coefficients (A, B, C) of the composed out(x) = A*x / (B + C*x)."""
        a_acc, b_acc, c_acc = 1.0, 1.0, 0.0  # identity: x / (1 + 0x)
        for pool, token_in, _ in self.hops():
            r_in, r_out = pool.reserves_for(token_in)
            if r_in <= 0 or r_out <= 0:
                return None
            g = (FEE_DENOMINATOR - pool.fee_ppm) / FEE_DENOMINATOR
            a, b, c = g * r_out, float(r_in), g
            a_acc, b_acc, c_acc = a * a_acc, b * b_acc, b * c_acc + c * a_acc
        return a_acc, b_acc, c_acc

    def describe(self, symbols: Mapping[str, str]) -> str:
        parts = [symbols.get(self.path[0], self.path[0])]
        for pool, _, token_out in self.hops():
            parts.append(f"-[{pool.label}]-> {symbols.get(token_out, token_out)}")
        return " ".join(parts)


def find_cycles(pools: Iterable[Pool], start_tokens: Iterable[str], max_hops: int) -> List[Route]:
    """All simple cycles of 2..max_hops pools starting and ending at a start token."""
    by_token: Dict[str, List[Pool]] = defaultdict(list)
    for pool in pools:
        by_token[pool.token0].append(pool)
        by_token[pool.token1].append(pool)

    routes: List[Route] = []

    def walk(start: str, path: List[str], used: List[Pool]) -> None:
        current = path[-1]
        for pool in by_token[current]:
            if pool in used:
                continue
            nxt = pool.other(current)
            if nxt == start:
                if len(used) + 1 >= 2:
                    routes.append(Route(tuple(used + [pool]), tuple(path + [nxt])))
            elif len(used) + 1 < max_hops and nxt not in path:
                walk(start, path + [nxt], used + [pool])

    for start in start_tokens:
        walk(start, [start], [])
    return routes


def optimal_input(route: Route, max_input: int) -> Optional[int]:
    """Profit-maximising input amount (raw units), or None if never profitable."""
    return optimal_input_from(route.mobius(), max_input)


def marginal_edge_pct(coeffs) -> Optional[float]:
    """Percent gained on a tiny trade after pool fees (before gas). Negative = losing."""
    if coeffs is None:
        return None
    a, b, _ = coeffs
    return (a / b - 1) * 100


def optimal_input_from(coeffs, max_input: int) -> Optional[int]:
    if coeffs is None:
        return None
    a, b, c = coeffs
    if a <= b or c <= 0:
        return None
    x = (math.sqrt(a * b) - b) / c
    x = min(x, float(max_input))
    return int(x) if x >= 1 else None


def usd_prices(pools: Sequence[Pool], decimals: Mapping[str, int], stables: Set[str]) -> Dict[str, float]:
    """Price every reachable token in USD, each via its deepest pool to an already-priced token."""
    prices: Dict[str, float] = {s: 1.0 for s in stables}
    while True:
        best: Dict[str, Tuple[float, float]] = {}  # token -> (depth_usd, price)
        for pool in pools:
            if not pool.active:
                continue
            for known, unknown in ((pool.token0, pool.token1), (pool.token1, pool.token0)):
                if known not in prices or unknown in prices:
                    continue
                r_known, r_unknown = pool.reserves_for(known)
                depth_usd = r_known / 10 ** decimals[known] * prices[known]
                price = depth_usd / (r_unknown / 10 ** decimals[unknown])
                if depth_usd > best.get(unknown, (0.0, 0.0))[0]:
                    best[unknown] = (depth_usd, price)
        if not best:
            return prices
        for token, (_, price) in best.items():
            prices[token] = price


def pool_liquidity_usd(pool: Pool, prices: Mapping[str, float], decimals: Mapping[str, int]) -> float:
    """USD value of both sides (V3: of the active range's virtual reserves). 0 if unpriced."""
    if pool.token0 not in prices or pool.token1 not in prices:
        return 0.0
    return (pool.reserve0 / 10 ** decimals[pool.token0] * prices[pool.token0]
            + pool.reserve1 / 10 ** decimals[pool.token1] * prices[pool.token1])


def to_usd(amount_raw: int, token: str, prices: Mapping[str, float], decimals: Mapping[str, int]) -> float:
    return amount_raw / 10 ** decimals[token] * prices.get(token, 0.0)


def from_usd(usd: float, token: str, prices: Mapping[str, float], decimals: Mapping[str, int]) -> int:
    price = prices.get(token, 0.0)
    return int(usd / price * 10 ** decimals[token]) if price > 0 else 0
