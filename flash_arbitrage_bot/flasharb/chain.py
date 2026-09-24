"""Blockchain reads via web3.py: pool discovery and per-block state refresh.

Every read in a refresh goes through one Multicall3 request pinned to a single
block, so all pools are priced from the same, consistent chain state.
Addresses are kept lowercase internally and checksummed only for web3.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence, Set, Tuple

from pathlib import Path

from . import discovery
from .amm import Pool
from .config import Config
from .routes import usd_prices

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
_CHUNK = 500
_PARALLEL_REQUESTS = 4
_GAS_PRICE_TTL_S = 15.0


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
        self._gas_price_at = float("-inf")
        self._last_block: Optional[int] = None
        # Pools the bot's routes use. Only these are re-read every block; the
        # rest are refreshed when routes are rebuilt (see refresh_all).
        self.tracked: Optional[Set[str]] = None
        self.discovered: Set[str] = set()  # long-tail tokens added by discovery
        self._pool_executor = ThreadPoolExecutor(max_workers=_PARALLEL_REQUESTS)

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
        chunks = [[(self.cs(target), True, data) for target, data in calls[i:i + _CHUNK]]
                  for i in range(0, len(calls), _CHUNK)]

        def run(chunk):
            return self._mc.functions.aggregate3(chunk).call(block_identifier=block)

        # Chunks are independent and pinned to the same block, so send them in parallel.
        batches = self._pool_executor.map(run, chunks) if len(chunks) > 1 else map(run, chunks)
        return [bytes(data) if success and len(data) > 0 else None
                for batch in batches for success, data in batch]

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
        core = list(self.decimals)
        pairs = [(a, b) for i, a in enumerate(core) for b in core[i + 1:]]
        self.discovered: Set[str] = set()
        if self.cfg.discovery.enabled:
            extra = self._discover_tokens(core, pairs)
            self.discovered = set(extra)
            # Long-tail tokens are paired with the core tokens only: pools between
            # two long-tail tokens are rare and would multiply the lookups.
            pairs += [(c, e) for e in extra for c in core]
        self.pools = self._discover_pools(pairs)
        self.refresh_pool_balances()
        self._last_block = None  # pool list changed: next refresh must read everything
        log.info("found %d pools across %d dexes for %d tokens (%d discovered)",
                 len(self.pools), len(self.cfg.dexes), len(self.decimals), len(self.discovered))

    # ----- long-tail token discovery ------------------------------------------

    def _discover_tokens(self, core: List[str], core_pairs) -> List[str]:
        """Add liquid tokens found in the V2-style factories' pair lists (cached)."""
        disc = self.cfg.discovery
        cache = Path(disc.cache_file)
        tokens = discovery.load_cache(cache, self.cfg.chain_id, disc.cache_hours, disc.min_liquidity_usd)
        if tokens is None:
            log.info("discovering tokens (one-off, cached for %.0fh; can take a few minutes)...",
                     disc.cache_hours)
            # Price the core tokens first; new tokens are valued against them.
            self.pools = self._discover_pools(core_pairs)
            self.refresh(full=True)
            self.refresh_pool_balances()
            stables = {self.cfg.token(s) for s in self.cfg.stable_tokens if self.cfg.token(s) in self.decimals}
            prices = usd_prices(self.pools, self.decimals, stables)
            records = self._enumerate_v2_pairs()
            chosen = discovery.select_tokens(records, prices, self.decimals, disc.min_liquidity_usd,
                                             disc.max_tokens, exclude=set(core))
            meta = self._token_meta([t for t, _ in chosen])
            tokens = [{"address": t, "symbol": meta[t][1], "decimals": meta[t][0],
                       "liquidity_usd": round(liq)} for t, liq in chosen if t in meta]
            discovery.save_cache(cache, self.cfg.chain_id, disc.min_liquidity_usd, tokens)
            log.info("kept %d tokens with >= $%s liquidity against priced tokens",
                     len(tokens), f"{disc.min_liquidity_usd:,.0f}")
        taken = set(self.cfg.tokens)
        added = []
        for entry in tokens:
            name = discovery.unique_name(entry["symbol"], entry["address"], taken)
            taken.add(name)
            self.cfg.tokens[name] = entry["address"]
            self.decimals[entry["address"]] = int(entry["decimals"])
            added.append(entry["address"])
        return added

    def _enumerate_v2_pairs(self) -> List[discovery.PairRecord]:
        """(token0, token1, reserve0, reserve1) for the newest pairs of every V2-style factory."""
        records: List[discovery.PairRecord] = []
        for dex in self.cfg.dexes.values():
            if dex.type not in ("v2", "camelot_v2"):
                continue
            data = self.call_many([(dex.factory, self.selector("allPairsLength()"))])[0]
            total = self.decode(["uint256"], data)[0] if data else 0
            first = max(0, total - self.cfg.discovery.max_pairs_per_factory)
            all_pairs = self.selector("allPairs(uint256)")
            results = self.call_many([(dex.factory, all_pairs + self.encode(["uint256"], [i]))
                                      for i in range(first, total)])
            pairs = [self.decode(["address"], r)[0].lower() for r in results if r]
            calls = []
            for pair in pairs:
                calls += [(pair, self.selector("token0()")), (pair, self.selector("token1()")),
                          (pair, self.selector("getReserves()"))]
            info = self.call_many(calls)
            for i in range(len(pairs)):
                t0, t1, res = info[3 * i], info[3 * i + 1], info[3 * i + 2]
                if not (t0 and t1 and res and len(res) >= 64):
                    continue
                r0, r1 = self.decode(["uint112", "uint112"], res[:64])
                records.append((self.decode(["address"], t0)[0].lower(),
                                self.decode(["address"], t1)[0].lower(), r0, r1))
            log.info("  %s: scanned %d of %d pairs", dex.name, len(pairs), total)
        return records

    def _token_meta(self, tokens: List[str]) -> Dict[str, Tuple[int, str]]:
        """{token: (decimals, symbol)} for tokens that answer like normal ERC-20s."""
        calls = []
        for token in tokens:
            calls += [(token, self.selector("decimals()")), (token, self.selector("symbol()"))]
        results = self.call_many(calls)
        meta = {}
        for i, token in enumerate(tokens):
            dec_data, sym_data = results[2 * i], results[2 * i + 1]
            if not dec_data:
                continue
            dec = self.decode(["uint8"], dec_data[:32])[0] if len(dec_data) >= 32 else None
            if dec is None or dec > 36:
                continue
            meta[token] = (dec, self._decode_symbol(sym_data) or token[:8])
        return meta

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

    def _discover_pools(self, token_pairs: Sequence[Tuple[str, str]]) -> List[Pool]:
        get_pair = self.selector("getPair(address,address)")
        get_pool = self.selector("getPool(address,address,uint24)")
        pool_by_pair = self.selector("poolByPair(address,address)")
        calls, meta = [], []
        for dex in self.cfg.dexes.values():
            for a, b in token_pairs:
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

    def refresh_all(self) -> int:
        """Re-read every discovered pool, not just the tracked ones."""
        return self.refresh(full=True)

    def refresh(self, full: bool = False) -> int:
        """Re-read the tracked pools (or all, if full) and the vault's balances
        if a new block exists."""
        block = self.w3.eth.block_number
        if block == self._last_block and not full:
            return block
        pools = self.pools if full or self.tracked is None else \
            [p for p in self.pools if p.address in self.tracked]
        get_reserves = self.selector("getReserves()")
        slot0 = self.selector("slot0()")
        global_state = self.selector("globalState()")
        liquidity = self.selector("liquidity()")
        balance_of = self.selector("balanceOf(address)") + self.encode(["address"], [self.cs(self.cfg.balancer_vault)])

        calls = []
        for pool in pools:
            if pool.kind in ("v2", "camelot_v2"):
                calls.append((pool.address, get_reserves))
            else:
                state = slot0 if pool.kind == "v3" else global_state
                calls += [(pool.address, state), (pool.address, liquidity)]
        flash_tokens = [self.cfg.token(s) for s in self.cfg.flash_tokens]
        calls += [(t, balance_of) for t in flash_tokens]

        results = iter(self.call_many(calls, block))
        for pool in pools:
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
        # Arbitrum's gas price barely moves; don't spend a request on it every block.
        if time.monotonic() - self._gas_price_at >= _GAS_PRICE_TTL_S:
            self.last_gas_price_wei = int(self.w3.eth.gas_price)
            self._gas_price_at = time.monotonic()
        return self.last_gas_price_wei
