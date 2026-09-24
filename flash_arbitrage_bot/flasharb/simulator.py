"""Exact whole-route checks with contracts/RouteSimulator.sol, and why estimates miss.

RouteSimulator is never deployed. Each check is one eth_call that places its
code at SIM_ADDRESS through a state override, pinned to the block the estimate
was made on. It flash-borrows from the real Balancer vault, swaps through the
real routers exactly like FlashArbitrage, and reverts with what arrived after
every hop. So a check includes real pool fees, liquidity depth, transfer taxes
and the flash-loan fee, and costs one request however many hops the route has.

diagnose() compares those per-hop amounts with the bot's own pool model on the
same inputs, at the same block, to say why an estimate was wrong:
  - V2-style hops are exact at a given block, so a shortfall there is a
    transfer tax (or a pool fee above the configured one);
  - a concentrated-liquidity hop that matches the model at a tiny size but not
    at full size ran out of liquidity (the model assumes it never ends);
  - one that is off even at a tiny size charged a different fee (Camelot V3
    recomputes its dynamic fee on the first swap of a block) or price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Mapping, Optional, Set

from .amm import ROUTER_KINDS

SIM_ADDRESS = "0xaB08D2CAAD0862E8699C93C90DDA71dABAc60cc8"  # keccak("flasharb.RouteSimulator")[12:]
CALLER = "0x52816c0a4bBD59865D9Fed0557145126BBac6AF0"       # keccak("flasharb.RouteSimulator.caller")[12:]
SIMULATE_SELECTOR = bytes.fromhex("40004d37")  # simulate(address,address,uint256,(uint8,address,address,address,uint24)[])
SIMULATE_TYPES = ["address", "address", "uint256", "(uint8,address,address,address,uint24)[]"]

_SIM_RESULT = "8340162e"    # SimResult(uint256,uint256[],uint256[])
_HOP_FAILED = "81f89af4"    # HopFailed(uint256,bytes)
_ERROR_STRING = "08c379a0"  # Error(string)
_PANIC = "4e487b71"         # Panic(uint256)
_CUSTOM_ERRORS = {"c4c321d1": "BadRoute", "14d4a4e8": "OnlySelf", "fecb69b4": "TokenCallFailed"}

V2_KINDS = ("v2", "camelot_v2")

# Revert reasons that mean "the pool didn't receive what the router sent",
# i.e. the input token took a cut in transfer (Uniswap V2 "K", V3 "IIA", ...).
_TAX_REASON = re.compile(r"(^|[^A-Za-z])K$|IIA|TRANSFER_FROM_FAILED|TRANSFER_FAILED|TransferHelper|^STF$")


def route_steps(route, cs: Callable[[str], str] = lambda a: a) -> List[tuple]:
    """FlashArbitrage/RouteSimulator Step tuples: (kind, router, tokenIn, tokenOut, fee)."""
    return [(ROUTER_KINDS[p.router_kind], cs(p.router), cs(t_in), cs(t_out), p.fee_ppm if p.kind == "v3" else 0)
            for p, t_in, t_out in route.hops()]


@dataclass
class Outcome:
    """One exact check of a route at one size."""
    amount_in: int
    block: int
    method: str                                        # "sim" (RouteSimulator) or "quoter"
    received: List[int] = field(default_factory=list)  # what arrived after each hop
    reported: List[int] = field(default_factory=list)  # what the router said it sent (0 = not reported)
    flash_fee: int = 0
    failed_hop: Optional[int] = None                   # hop that reverted; -1 = outside the swaps
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.failed_hop is None and bool(self.received)

    @property
    def out(self) -> int:
        return self.received[-1] if self.ok else 0

    @property
    def profit_raw(self) -> int:
        """Left over after repaying the loan and its fee (negative = loss)."""
        return self.out - self.amount_in - self.flash_fee


def reason_text(data: bytes, decode) -> str:
    """Readable revert reason from raw revert data."""
    if not data:
        return "reverted without a reason"
    sel, body = data[:4].hex(), data[4:]
    try:
        if sel == _ERROR_STRING:
            return str(decode(["string"], body)[0])
        if sel == _PANIC:
            return f"panic 0x{decode(['uint256'], body)[0]:02x}"
        if sel == _HOP_FAILED:
            hop, inner = decode(["uint256", "bytes"], body)
            return f"hop {hop + 1}: {reason_text(bytes(inner), decode)}"
    except Exception:
        pass
    return _CUSTOM_ERRORS.get(sel, f"error 0x{sel}")


def decode_result(data: bytes, decode, amount_in: int, block: int) -> Outcome:
    """Turn RouteSimulator's revert data into an Outcome."""
    sel = data[:4].hex() if data else ""
    if sel == _SIM_RESULT:
        fee, received, reported = decode(["uint256", "uint256[]", "uint256[]"], data[4:])
        return Outcome(amount_in, block, "sim", [int(x) for x in received], [int(x) for x in reported], int(fee))
    if sel == _HOP_FAILED:
        hop, inner = decode(["uint256", "bytes"], data[4:])
        return Outcome(amount_in, block, "sim", failed_hop=int(hop), reason=reason_text(bytes(inner), decode))
    # The flash loan itself failed (e.g. vault short of the token) or something unexpected.
    return Outcome(amount_in, block, "sim", failed_hop=-1, reason=reason_text(data, decode))


@dataclass
class Diagnosis:
    cause: str                  # depth | fee/price | transfer tax | swap reverted | flash loan | small deviations
    hop: Optional[int]          # 0-based index of the hop to blame
    detail: str
    taxed_token: Optional[str] = None
    shortfall: float = 0.0      # fraction the blamed hop came up short (0.03 = 3%)


def _pick(tokens, core: Set[str]) -> Optional[str]:
    for t in tokens:
        if t not in core:
            return t
    return None


def diagnose(route, full: Outcome, tiny: Optional[Outcome], symbols: Mapping[str, str],
             core: Set[str]) -> Diagnosis:
    """Why the exact check came out below the estimate (both at the same block)."""
    hops = list(route.hops())
    sym = lambda t: symbols.get(t, t[:8])  # noqa: E731
    if full.failed_hop is not None:
        if full.failed_hop < 0:  # before or after the swaps: the loan itself, gas, the RPC...
            what = "flash loan" if "BAL#" in full.reason else "call failed"
            return Diagnosis(what, None, f"{what}: {full.reason}")
        i = full.failed_hop
        pool, t_in, t_out = hops[i]
        if _TAX_REASON.search(full.reason.strip()):
            taxed = _pick((t_in, t_out), core)
            return Diagnosis("transfer tax", i, f"hop {i + 1} {pool.label} reverted ({full.reason}): "
                             f"{sym(taxed or t_in)} doesn't arrive in full", taxed, 1.0)
        return Diagnosis("swap reverted", i, f"hop {i + 1} {pool.label} reverted: {full.reason}")

    ratios, x = [], full.amount_in
    for i, (pool, t_in, _) in enumerate(hops):
        model = pool.amount_out(t_in, x)
        real = full.received[i]
        ratios.append((real / model if model else 0.0, model, real))
        x = real
    i = min(range(len(hops)), key=lambda k: ratios[k][0])
    ratio, model, real = ratios[i]
    pool, t_in, t_out = hops[i]
    shortfall = 1 - ratio
    where = f"hop {i + 1} {pool.label} {sym(t_in)}->{sym(t_out)}"
    sent = full.reported[i] if i < len(full.reported) else 0
    if sent and real < sent * 0.999:
        taxed = t_out if t_out not in core else None
        return Diagnosis("transfer tax", i, f"{where}: router sent {sent} but only {real} arrived "
                         f"({real / sent - 1:+.2%}): {sym(t_out)} takes a cut in transfer", taxed, shortfall)
    if shortfall <= 0.0005:
        return Diagnosis("small deviations", None, f"every hop within 0.05% of the model "
                         f"(worst {where} {-shortfall:+.3%}); fees and gas ate the margin")
    if pool.kind in V2_KINDS:  # exact math on this block's reserves: the token must be taxed
        return Diagnosis("transfer tax", i, f"{where}: {-shortfall:+.2%} vs exact constant-product math "
                         f"(a transfer tax, or a pool fee above the configured one)",
                         _pick((t_out, t_in), core), shortfall)
    tiny_ratio = None
    if tiny is not None and tiny.ok and len(tiny.received) == len(hops):
        x = tiny.amount_in if i == 0 else tiny.received[i - 1]
        tiny_model = pool.amount_out(t_in, x)
        tiny_ratio = tiny.received[i] / tiny_model if tiny_model else None
    at_tiny = f", {tiny_ratio - 1:+.3%} at 1/1000 size" if tiny_ratio is not None else ""
    if tiny_ratio is not None and tiny_ratio >= 0.9995:
        return Diagnosis("depth", i, f"{where}: {-shortfall:+.2%} at full size{at_tiny}: liquidity ends "
                         "before this size (the model treats the current range as endless)", None, shortfall)
    what = "dynamic fee/price" if pool.kind == "algebra" else "fee/price"
    return Diagnosis(what, i, f"{where}: {-shortfall:+.2%} at full size{at_tiny}: the pool charged a "
                     "different fee or price than the model read", None, shortfall)


def looks_unsupported(error: dict) -> bool:
    """True when an RPC error means eth_call state overrides aren't supported."""
    text = f"{error.get('message', '')} {error.get('data', '')}".lower()
    return error.get("code") in (-32602, -32601) or "argument" in text or "override" in text
