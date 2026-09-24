"""Pick long-tail tokens worth watching from every pair a V2-style factory lists.

A token qualifies when it's paired against a token we can already price (WETH,
stablecoins, the configured core list) with enough real value on that priced
side. Measuring only the priced side means a token can't look liquid just by
minting itself a huge supply.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

PairRecord = Tuple[str, str, int, int]  # token0, token1, reserve0, reserve1


def select_tokens(records: Iterable[PairRecord], prices: Mapping[str, float],
                  decimals: Mapping[str, int], min_liquidity_usd: float, max_tokens: int,
                  exclude: Set[str]) -> List[Tuple[str, float]]:
    """(token, liquidity_usd) for the most liquid new tokens, deepest first."""
    best: Dict[str, float] = {}
    for t0, t1, r0, r1 in records:
        for known, other, reserve in ((t0, t1, r0), (t1, t0, r1)):
            if other in exclude or other in prices or known not in prices or known not in decimals:
                continue
            liquidity = 2 * reserve / 10 ** decimals[known] * prices[known]
            if liquidity > best.get(other, 0.0):
                best[other] = liquidity
    chosen = sorted((t for t, v in best.items() if v >= min_liquidity_usd), key=lambda t: -best[t])
    return [(t, best[t]) for t in chosen[:max_tokens]]


def unique_name(symbol: str, address: str, taken: Set[str]) -> str:
    """Config-style token name; disambiguated with the address when symbols clash."""
    clean = "".join(ch for ch in symbol if ch.isprintable() and not ch.isspace())[:20] or "TOKEN"
    name = clean if clean not in taken else f"{clean}_{address[2:6]}"
    while name in taken:
        name += "_"
    return name


def load_cache(path: Path, chain_id: int, max_age_hours: float, min_liquidity_usd: float) -> Optional[List[dict]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    fresh = time.time() - data.get("time", 0) < max_age_hours * 3600
    if data.get("chain_id") != chain_id or not fresh or data.get("min_liquidity_usd") != min_liquidity_usd:
        return None
    return data.get("tokens")


def save_cache(path: Path, chain_id: int, min_liquidity_usd: float, tokens: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chain_id": chain_id, "time": time.time(),
                                "min_liquidity_usd": min_liquidity_usd, "tokens": tokens}, indent=1))
