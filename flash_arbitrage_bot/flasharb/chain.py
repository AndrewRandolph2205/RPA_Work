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
        # web3's validation middleware asks the node for the chain id before every
        # call, doubling traffic. check_network() verifies the chain once instead.
        try:
            self.w3.middleware_onion.remove("validation")
        except Exception:
            pass
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
        pool_by_pair = self.selector("poolByPair(address,address)")
        calls, meta = [], []
        for dex in self.cfg.dexes.values():
            for i, a in enumerate(tokens):
                for b in tokens[i + 1:]:
                    t0, t1 = sorted((a, b), key=lambda x: int(x, 16))
                    pair = self.encode(["address", "address"], [self.cs(t0), self.cs(t1)])
                    if dex.type in ("v2", "camelot_v2"):
                        calls.append((dex.factory, get_pair + pair))
                        meta.append((dex, t0, t1, dex.fee))
                    elif dex.type == "algebra":  # one pool per pair, fee read each block
                        calls.append((dex.factory, pool_by_pair + pair))
                        meta.append((dex, t0, t1, 0))
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
                              quoter=dex.quoter if dex.type in ("v3", "algebra") else ""))
        return self._drop_stable_pairs(pools)

    def _drop_stable_pairs(self, pools: List[Pool]) -> List[Pool]:
        """Camelot V2 "stable" pairs use a different curve (x^3y + y^3x), which the
        constant-product math can't price. Keep only pairs that confirm they're volatile."""
        camelot = [p for p in pools if p.kind == "camelot_v2"]
        if not camelot:
            return pools
        results = self.call_many([(p.address, self.selector("stableSwap()")) for p in camelot])
        drop = {p.address for p, data in zip(camelot, results)
                if data is None or self.decode(["bool"], data)[0]}
        if drop:
            log.info("skipping %d Camelot stable pairs", len(drop))
        return [p for p in pools if p.address not in drop]

    def refresh_pool_balances(self) -> None:
        """Read the tokens each concentrated-liquidity pool actually holds (see Pool.depth)."""
        pools = [p for p in self.pools if p.concentrated]
        balance_of = self.selector("balanceOf(address)")
        calls = []
        for pool in pools:
            owner = self.encode(["address"], [self.cs(pool.address)])
            calls += [(pool.token0, balance_of + owner), (pool.token1, balance_of + owner)]
        results = self.call_many(calls)
        for i, pool in enumerate(pools):
            b0, b1 = results[2 * i], results[2 * i + 1]
            pool.balance0 = self.decode(["uint256"], b0)[0] if b0 else 0
            pool.balance1 = self.decode(["uint256"], b1)[0] if b1 else 0

    def _quote_hop(self, pool: Pool, token_in: str, token_out: str, amount: int) -> int:
        """eth_call the pool's quoter. Uniswap QuoterV2 and Algebra's Quoter differ."""
        if pool.kind == "v3":
            data = self.selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + \
                self.encode(["(address,address,uint256,uint24,uint160)"],
                            [(self.cs(token_in), self.cs(token_out), int(amount), pool.fee_ppm, 0)])
        else:
            data = self.selector("quoteExactInputSingle(address,address,uint256,uint160)") + \
                self.encode(["address", "address", "uint256", "uint160"],
                            [self.cs(token_in), self.cs(token_out), int(amount), 0])
        raw = bytes(self.w3.eth.call({"to": self.cs(pool.quoter), "data": data}))
        return self.decode(["uint256"], raw[:32])[0]

    def quote_route(self, route, amount_in: int) -> Tuple[int, bool]:
        """Exact output of a route. V2-style hops use the fresh reserves (exact);
        concentrated-liquidity hops ask the dex's quoter via eth_call, which walks
        the real liquidity across price bands. Returns (amount_out, fully_verified);
        0 if a quote reverts."""
        amount, verified = amount_in, True
        for pool, token_in, token_out in route.hops():
            if not pool.concentrated:
                amount = pool.amount_out(token_in, amount)
            elif pool.quoter:
                try:
                    amount = self._quote_hop(pool, token_in, token_out, amount)
                except Exception:  # e.g. not enough liquidity for this size
                    return 0, True
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
        global_state = self.selector("globalState()")
        liquidity = self.selector("liquidity()")
        balance_of = self.selector("balanceOf(address)") + self.encode(["address"], [self.cs(self.cfg.balancer_vault)])

        calls = []
        for pool in self.pools:
            if pool.kind in ("v2", "camelot_v2"):
                calls.append((pool.address, get_reserves))
            else:
                state = slot0 if pool.kind == "v3" else global_state
                calls += [(pool.address, state), (pool.address, liquidity)]
        flash_tokens = [self.cfg.token(s) for s in self.cfg.flash_tokens]
        calls += [(t, balance_of) for t in flash_tokens]

        results = iter(self.call_many(calls, block))
        for pool in self.pools:
            if pool.kind in ("v2", "camelot_v2"):
                data = next(results)
                if data is None:
                    pool.update_v2(0, 0)
                elif pool.kind == "v2":
                    r0, r1, _ = self.decode(["uint112", "uint112", "uint32"], data)
                    pool.update_v2(r0, r1)
                else:
                    # Camelot: (reserve0, reserve1, token0FeePercent, token1FeePercent),
                    # fees out of 100,000 -> parts per million.
                    r0, r1, f0, f1 = self.decode(["uint112", "uint112", "uint16", "uint16"], data[:128])
                    pool.update_v2(r0, r1)
                    pool.fee_ppm, pool.fee1_ppm = f0 * 10, f1 * 10
            else:
                state, liq = next(results), next(results)
                if state is None or liq is None:
                    pool.update_v3(0, 0)
                elif pool.kind == "v3":
                    # Only the first two slot0 fields are read; forks differ after that.
                    sqrt_price, _ = self.decode(["uint160", "int24"], state[:64])
                    pool.update_v3(sqrt_price, self.decode(["uint128"], liq)[0])
                else:
                    # Algebra (Camelot V3) globalState: price, tick, feeZto, feeOtz, ...
                    # Fees are already in parts per million and differ by direction.
                    sqrt_price, _, fee_zto, fee_otz = self.decode(
                        ["uint160", "int24", "uint16", "uint16"], state[:128])
                    pool.update_v3(sqrt_price, self.decode(["uint128"], liq)[0])
                    pool.fee_ppm, pool.fee1_ppm = fee_zto, fee_otz
        for token in flash_tokens:
            data = next(results)
            self.vault_balances[token] = self.decode(["uint256"], data)[0] if data else 0
        self._last_block = block
        return block

    def gas_price_wei(self) -> int:
        self.last_gas_price_wei = int(self.w3.eth.gas_price)
        return self.last_gas_price_wei
