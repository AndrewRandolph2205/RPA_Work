"""Constant-product pool math for Uniswap V2 and V3 style pools.

V2 pools are exact: output = constant-product formula on the real reserves.

V3 pools are modelled with *virtual reserves* derived from the current price
and active liquidity (x = L / sqrtP, y = L * sqrtP). This is exact while a
trade stays inside the current tick range and an approximation beyond it, so
every candidate is re-checked by simulating the real transaction on-chain
before anything is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

Q96 = 2 ** 96
FEE_DENOMINATOR = 1_000_000  # fees are in parts per million: 3000 = 0.30%

ROUTER_KINDS = {"v2": 0, "v3_router02": 1, "v3_router": 2}


@dataclass(eq=False)
class Pool:
    address: str
    dex: str
    kind: str  # "v2" | "v3"
    token0: str  # lower address, as sorted by the factory
    token1: str
    fee_ppm: int
    router: str
    router_kind: str  # key of ROUTER_KINDS
    reserve0: int = 0
    reserve1: int = 0

    def __hash__(self) -> int:
        return hash(self.address)

    @property
    def label(self) -> str:
        return f"{self.dex}/{self.fee_ppm / 10_000:g}%"

    def has(self, token: str) -> bool:
        return token in (self.token0, self.token1)

    def other(self, token: str) -> str:
        return self.token1 if token == self.token0 else self.token0

    def reserves_for(self, token_in: str) -> Tuple[int, int]:
        if token_in == self.token0:
            return self.reserve0, self.reserve1
        return self.reserve1, self.reserve0

    def update_v2(self, reserve0: int, reserve1: int) -> None:
        self.reserve0, self.reserve1 = int(reserve0), int(reserve1)

    def update_v3(self, sqrt_price_x96: int, liquidity: int) -> None:
        if sqrt_price_x96 <= 0 or liquidity <= 0:
            self.reserve0 = self.reserve1 = 0
            return
        self.reserve0 = liquidity * Q96 // sqrt_price_x96
        self.reserve1 = liquidity * sqrt_price_x96 // Q96

    @property
    def active(self) -> bool:
        return self.reserve0 > 0 and self.reserve1 > 0

    def amount_out(self, token_in: str, amount_in: int) -> int:
        """Exact integer output, matching UniswapV2Library.getAmountOut."""
        r_in, r_out = self.reserves_for(token_in)
        if amount_in <= 0 or r_in <= 0 or r_out <= 0:
            return 0
        with_fee = amount_in * (FEE_DENOMINATOR - self.fee_ppm)
        return with_fee * r_out // (r_in * FEE_DENOMINATOR + with_fee)
