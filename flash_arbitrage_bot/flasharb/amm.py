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
from typing import Optional, Tuple

Q96 = 2 ** 96
FEE_DENOMINATOR = 1_000_000  # fees are in parts per million: 3000 = 0.30%

# Router interface -> Step.kind in FlashArbitrage.sol
ROUTER_KINDS = {"v2": 0, "v3_router02": 1, "v3_router": 2, "camelot_v2": 3, "algebra": 4}

# Pool types. "camelot_v2" is a V2-style pair with per-direction fees;
# "algebra" is Camelot V3 (concentrated liquidity, one pool per pair, dynamic fees).
POOL_TYPES = ("v2", "v3", "camelot_v2", "algebra")
CONCENTRATED = ("v3", "algebra")
DYNAMIC_FEE = ("camelot_v2", "algebra")


@dataclass(eq=False)
class Pool:
    address: str
    dex: str
    kind: str  # one of POOL_TYPES
    token0: str  # lower address, as sorted by the factory
    token1: str
    fee_ppm: int  # fee when token0 is sold (and token1 too, unless fee1_ppm is set)
    router: str
    router_kind: str  # key of ROUTER_KINDS
    reserve0: int = 0
    reserve1: int = 0
    # Tokens the pool actually holds (V3 only; None until read). V3 virtual
    # reserves can vastly overstate depth when liquidity sits in a narrow band,
    # so depth checks use whichever is smaller.
    balance0: Optional[int] = None
    balance1: Optional[int] = None
    quoter: str = ""  # concentrated pools: quoter contract used to price trades exactly
    fee1_ppm: Optional[int] = None  # fee when token1 is sold, for per-direction fees
    # Concentrated pools: the raw state behind the virtual reserves, kept so
    # pool events (logstate.py) can update them.
    sqrt_price_x96: Optional[int] = None
    liquidity: Optional[int] = None
    tick: Optional[int] = None

    def __hash__(self) -> int:
        return hash(self.address)

    @property
    def label(self) -> str:
        if self.kind in DYNAMIC_FEE:
            return self.dex
        return f"{self.dex}/{self.fee_ppm / 10_000:g}%"

    @property
    def concentrated(self) -> bool:
        return self.kind in CONCENTRATED

    def fee_for(self, token_in: str) -> int:
        if token_in == self.token1 and self.fee1_ppm is not None:
            return self.fee1_ppm
        return self.fee_ppm

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
        self.sqrt_price_x96, self.liquidity = int(sqrt_price_x96), int(liquidity)
        if sqrt_price_x96 <= 0 or liquidity <= 0:
            self.reserve0 = self.reserve1 = 0
            return
        self.reserve0 = liquidity * Q96 // sqrt_price_x96
        self.reserve1 = liquidity * sqrt_price_x96 // Q96

    def depth(self) -> Tuple[int, int]:
        """Conservative (token0, token1) depth for liquidity and pricing decisions."""
        if self.balance0 is None or self.balance1 is None:
            return self.reserve0, self.reserve1
        return min(self.reserve0, self.balance0), min(self.reserve1, self.balance1)

    @property
    def active(self) -> bool:
        return self.reserve0 > 0 and self.reserve1 > 0

    def amount_out(self, token_in: str, amount_in: int) -> int:
        """Exact integer output, matching UniswapV2Library.getAmountOut."""
        r_in, r_out = self.reserves_for(token_in)
        if amount_in <= 0 or r_in <= 0 or r_out <= 0:
            return 0
        with_fee = amount_in * (FEE_DENOMINATOR - self.fee_for(token_in))
        out = with_fee * r_out // (r_in * FEE_DENOMINATOR + with_fee)
        if self.balance0 is not None and self.balance1 is not None:
            # A pool can never pay out more than it holds.
            out = min(out, self.balance1 if token_in == self.token0 else self.balance0)
        return out
