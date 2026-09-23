"""Blockchain reads via web3.py: pool discovery and per-block state refresh.

Every read in a refresh goes through one Multicall3 request pinned to a single
block, so all pools are priced from the same, consistent chain state.
Addresses are kept lowercase internally and checksummed only for web3.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from .amm import Pool
from .config import Config

log = logging.getLogger(__name__)

# Multicall3 is deployed at this address on virtually every EVM chain.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI = [{
    "type": "function", "name": "aggregate3", "stateMutability": "payable",
    "inputs": [{"name": "calls", "type": "tuple[]", "components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"}]}],
    "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
        {"name": "success", "type": "bool"},
        {"name": "returnData", "type": "bytes"}]}],
}]
_CHUNK = 250


class Chain:
    def __init__(self, cfg: Config, rpc_url: str):
        from web3 import Web3  # noqa: WPS433 - optional heavy dependency

        self.Web3 = Web3
        self.cfg = cfg
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        self._mc = self.w3.eth.contract(address=self.cs(MULTICALL3), abi=MULTICALL3_ABI)
        self.pools: List[Pool] = []
        self.decimals: Dict[str, int] = {}
        self.vault_balances: Dict[str, int] = {}
        self.last_gas_price_wei = 0
        self._last_block: Optional[int] = None

    # ----- helpers ----------------------------------------------------------

    def cs(self, address: str) -> str:
        return self.Web3.to_checksum_address(address)

    def selector(self, signature: str) -> bytes:
        return bytes(self.Web3.keccak(text=signature)[:4])

    def encode(self, types: Sequence[str], values: Sequence) -> bytes:
        return self.w3.codec.encode(list(types), list(values))

    def decode(self, types: Sequence[str], data: bytes):
        return self.w3.codec.decode(list(types), data)

    def call_many(self, calls: Sequence[Tuple[str, bytes]], block="latest") -> List[Optional[bytes]]:
        """Batch eth_calls through Multicall3. Failed or empty results come back as None."""
        results: List[Optional[bytes]] = []
        for i in range(0, len(calls), _CHUNK):
            chunk = [(self.cs(target), True, data) for target, data in calls[i:i + _CHUNK]]
            for success, data in self._mc.functions.aggregate3(chunk).call(block_identifier=block):
                results.append(bytes(data) if success and len(data) > 0 else None)
        return results

    # ----- setup ------------------------------------------------------------

    def check_network(self) -> None:
        chain_id = self.w3.eth.chain_id
        if chain_id != self.cfg.chain_id:
            raise RuntimeError(f"RPC is on chain {chain_id}, config expects {self.cfg.chain_id} "
                               f"({self.cfg.chain_name})")

    def missing_code(self, addresses: Dict[str, str]) -> List[str]:
        """Labels of configured addresses that have no contract deployed."""
        return [label for label, addr in addresses.items() if len(self.w3.eth.get_code(self.cs(addr))) == 0]

    def load(self) -> None:
        self.check_network()
        self.decimals = self._verify_tokens()
        if self.cfg.token(self.cfg.native_wrapped) not in self.decimals:
            raise RuntimeError(f"{self.cfg.native_wrapped} failed verification; "
                               "the bot can't value gas without it")
        if not any(self.cfg.token(s) in self.decimals for s in self.cfg.stable_tokens):
            raise RuntimeError("no stable token passed verification; profits can't be valued in USD")
        tokens = list(self.decimals)
        self.pools = self._discover_pools(tokens)
        self.refresh_pool_balances()
        log.info("found %d pools across %d dexes for %d tokens",
                 len(self.pools), len(self.cfg.dexes), len(tokens))

    def _verify_tokens(self) -> Dict[str, int]:
        """Ask every configured token for decimals() and symbol().

        Tokens that don't answer decimals() (wrong address, not a token) are
        skipped with a warning. A symbol that differs from the config name only
        warns, since some tokens use unusual on-chain symbols (e.g. USDT0)."""
        entries = list(self.cfg.tokens.items())
        calls = []
        for _, token in entries:
            calls += [(token, self.selector("decimals()")), (token, self.selector("symbol()"))]
        results = self.call_many(calls)
        decimals: Dict[str, int] = {}
        for i, (name, token) in enumerate(entries):
            dec_data, sym_data = results[2 * i], results[2 * i + 1]
            if dec_data is None:
                log.warning("SKIPPING token %s (%s): no decimals() - wrong address?", name, token)
                continue
            decimals[token] = self.decode(["uint8"], dec_data)[0]
            onchain = self._decode_symbol(sym_data)
            if onchain is not None and onchain.lower() != name.lower():
                log.warning("token %s (%s) calls itself %r on-chain; check the address if that's "
                            "not expected", name, token, onchain)
        log.info("%d of %d tokens verified", len(decimals), len(entries))
        return decimals

    def _decode_symbol(self, data: Optional[bytes]) -> Optional[str]:
        if not data:
            return None
        try:
            return self.decode(["string"], data)[0]
        except Exception:  # a few old tokens return bytes32 instead of string
            return data[:32].rstrip(b"\0").decode("utf-8", "replace") or None

    def _discover_pools(self, tokens: List[str]) -> List[Pool]:
        get_pair = self.selector("getPair(address,address)")
        get_pool = self.selector("getPool(address,address,uint24)")
        calls, meta = [], []
        for dex in self.cfg.dexes.values():
            for i, a in enumerate(tokens):
                for b in tokens[i + 1:]:
                    t0, t1 = sorted((a, b), key=lambda x: int(x, 16))
                    if dex.type == "v2":
                        calls.append((dex.factory, get_pair + self.encode(["address", "address"], [self.cs(t0), self.cs(t1)])))
                        meta.append((dex, t0, t1, dex.fee))
                    else:
                        for fee in dex.fee_tiers:
                            calls.append((dex.factory, get_pool + self.encode(
                                ["address", "address", "uint24"], [self.cs(t0), self.cs(t1), fee])))
                            meta.append((dex, t0, t1, fee))
        pools = []
        for (dex, t0, t1, fee), data in zip(meta, self.call_many(calls)):
            if data is None:
                continue
            address = self.decode(["address"], data)[0].lower()
            if int(address, 16) == 0:
                continue
            pools.append(Pool(address=address, dex=dex.name, kind=dex.type, token0=t0, token1=t1,
                              fee_ppm=fee, router=dex.router, router_kind=dex.router_kind,
                              quoter=dex.quoter if dex.type == "v3" else ""))
        return pools

    def refresh_pool_balances(self) -> None:
        """Read the tokens each V3 pool actually holds (see Pool.depth)."""
        v3 = [p for p in self.pools if p.kind == "v3"]
        balance_of = self.selector("balanceOf(address)")
        calls = []
        for pool in v3:
            owner = self.encode(["address"], [self.cs(pool.address)])
            calls += [(pool.token0, balance_of + owner), (pool.token1, balance_of + owner)]
        results = self.call_many(calls)
        for i, pool in enumerate(v3):
            b0, b1 = results[2 * i], results[2 * i + 1]
            pool.balance0 = self.decode(["uint256"], b0)[0] if b0 else 0
            pool.balance1 = self.decode(["uint256"], b1)[0] if b1 else 0

    def quote_route(self, route, amount_in: int) -> Tuple[int, bool]:
        """Exact output of a route. V2 hops use the fresh reserves; V3 hops ask the
        dex's QuoterV2 via eth_call, which walks the real liquidity across price
        bands. Returns (amount_out, fully_verified); 0 if a quote reverts."""
        selector = self.selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
        amount, verified = amount_in, True
        for pool, token_in, token_out in route.hops():
            if pool.kind == "v2":
                amount = pool.amount_out(token_in, amount)
            elif pool.quoter:
                data = selector + self.encode(
                    ["(address,address,uint256,uint24,uint160)"],
                    [(self.cs(token_in), self.cs(token_out), int(amount), pool.fee_ppm, 0)])
                try:
                    raw = self.w3.eth.call({"to": self.cs(pool.quoter), "data": data})
                except Exception:  # e.g. not enough liquidity for this size
                    return 0, True
                amount = self.decode(["uint256", "uint160", "uint32", "uint256"], bytes(raw))[0]
            else:
                amount = pool.amount_out(token_in, amount)
                verified = False
            if amount <= 0:
                return 0, verified
        return amount, verified

    # ----- per block --------------------------------------------------------

    def refresh(self) -> int:
        """Re-read every pool and the vault's balances if a new block exists."""
        block = self.w3.eth.block_number
        if block == self._last_block:
            return block
        get_reserves = self.selector("getReserves()")
        slot0 = self.selector("slot0()")
        liquidity = self.selector("liquidity()")
        balance_of = self.selector("balanceOf(address)") + self.encode(["address"], [self.cs(self.cfg.balancer_vault)])

        calls = []
        for pool in self.pools:
            if pool.kind == "v2":
                calls.append((pool.address, get_reserves))
            else:
                calls += [(pool.address, slot0), (pool.address, liquidity)]
        flash_tokens = [self.cfg.token(s) for s in self.cfg.flash_tokens]
        calls += [(t, balance_of) for t in flash_tokens]

        results = iter(self.call_many(calls, block))
        for pool in self.pools:
            if pool.kind == "v2":
                data = next(results)
                if data is None:
                    pool.update_v2(0, 0)
                else:
                    r0, r1, _ = self.decode(["uint112", "uint112", "uint32"], data)
                    pool.update_v2(r0, r1)
            else:
                s0, liq = next(results), next(results)
                if s0 is None or liq is None:
                    pool.update_v3(0, 0)
                else:
                    # Only the first two slot0 fields are read; forks differ after that.
                    sqrt_price, _ = self.decode(["uint160", "int24"], s0[:64])
                    pool.update_v3(sqrt_price, self.decode(["uint128"], liq)[0])
        for token in flash_tokens:
            data = next(results)
            self.vault_balances[token] = self.decode(["uint256"], data)[0] if data else 0
        self._last_block = block
        return block

    def gas_price_wei(self) -> int:
        self.last_gas_price_wei = int(self.w3.eth.gas_price)
        return self.last_gas_price_wei
