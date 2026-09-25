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
from .amm import V2_STYLE, Pool
from .logstate import LATEST, apply_event, drifted, pool_state
from .config import Config
from .routes import flash_fee, usd_prices
from .simulator import (CALLER, SIM_ADDRESS, SIMULATE_SELECTOR, SIMULATE_TYPES, Outcome, decode_result,
                        looks_unsupported, route_steps)
from .simulator_code import RUNTIME_HEX

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
# Errors a node gives for a block it hasn't imported yet. The sequencer feed
# announces blocks before RPC providers have them, so these are retried briefly.
_NOT_YET = ("header not found", "unknown block", "block not found", "not found", "out of range",
            "future block", "unfinalized", "too high", "exceeds latest")


class SimUnsupported(Exception):
    """The RPC ignores or rejects eth_call state overrides."""


def _revert_bytes(error: dict) -> Optional[bytes]:
    data = error.get("data")
    if isinstance(data, dict):
        data = data.get("data") or data.get("result")
    if isinstance(data, str) and data.startswith("0x"):
        try:
            return bytes.fromhex(data[2:])
        except ValueError:
            return None
    return None


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
        self._verify_executor = ThreadPoolExecutor(max_workers=_PARALLEL_REQUESTS)
        self.flash_fee_rate = 0.0       # Balancer flash-loan fee, as a fraction (read in load())
        self.sim_supported: Optional[bool] = None  # None until the first exact check
        self.rpc_behind_timeouts = 0    # pinned reads that gave up waiting for the RPC
        # Push-based state (state_source = "logs"), see logstate.py.
        self.logs = None                # LogStream, attached by attach_logs()
        self._logs_generation = -1      # the stream generation the pools were last read at
        self._logs_synced_at = float("-inf")
        self._marks: Dict[str, Tuple[int, int]] = {}  # pool -> (block, log index) its state reflects
        self.state_stats = {"logs": 0, "rpc": 0, "resyncs": 0, "timeouts": 0, "stale_events": 0,
                            "unapplied": 0, "late_events": 0}
        self.last_drift: Optional[Tuple[int, int]] = None  # (pools that differed, pools compared)

    # ----- helpers ----------------------------------------------------------

    def cs(self, address: str) -> str:
        return self.Web3.to_checksum_address(address)

    def selector(self, signature: str) -> bytes:
        return bytes(self.Web3.keccak(text=signature)[:4])

    def encode(self, types: Sequence[str], values: Sequence) -> bytes:
        return self.w3.codec.encode(list(types), list(values))

    def decode(self, types: Sequence[str], data: bytes):
        return self.w3.codec.decode(list(types), data)

    def _raw(self, method: str, params: list) -> dict:
        """Plain JSON-RPC request; errors come back in the response, not raised."""
        return self.w3.provider.make_request(method, params)

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

    def read_flash_fee_rate(self) -> float:
        """Balancer V2's flash-loan fee (ProtocolFeesCollector), as a fraction."""
        data = self.call_many([(self.cfg.balancer_vault, self.selector("getProtocolFeesCollector()"))])[0]
        collector = self.decode(["address"], data)[0]
        data = self.call_many([(collector, self.selector("getFlashLoanFeePercentage()"))])[0]
        return self.decode(["uint256"], data)[0] / 1e18

    def load(self) -> None:
        self.check_network()
        try:
            self.flash_fee_rate = self.read_flash_fee_rate()
            log.info("Balancer flash-loan fee: %.4f%% (%.1f bps)", self.flash_fee_rate * 100,
                     self.flash_fee_rate * 1e4)
        except Exception as exc:
            log.warning("couldn't read Balancer's flash-loan fee (%s); assuming 0. Estimates will be "
                        "too high if a fee is ever switched on.", exc)
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
            if dex.type not in V2_STYLE:
                continue
            # Solidly-style factories name their list "pools" instead of "pairs".
            length, item = (("allPoolsLength()", "allPools(uint256)") if dex.type == "solidly"
                            else ("allPairsLength()", "allPairs(uint256)"))
            data = self.call_many([(dex.factory, self.selector(length))])[0]
            total = self.decode(["uint256"], data)[0] if data else 0
            first = max(0, total - self.cfg.discovery.max_pairs_per_factory)
            all_pairs = self.selector(item)
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
                r0, r1 = self.decode(["uint256", "uint256"], res[:64])
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
        get_volatile = self.selector("getPool(address,address,bool)")
        calls, meta = [], []
        for dex in self.cfg.dexes.values():
            for a, b in token_pairs:
                t0, t1 = sorted((a, b), key=lambda x: int(x, 16))
                pair = self.encode(["address", "address"], [self.cs(t0), self.cs(t1)])
                if dex.type in ("v2", "camelot_v2"):
                    calls.append((dex.factory, get_pair + pair))
                    meta.append((dex, t0, t1, dex.fee))
                elif dex.type == "solidly":  # the volatile pool only: stable pools use another curve
                    calls.append((dex.factory, get_volatile + self.encode(
                        ["address", "address", "bool"], [self.cs(t0), self.cs(t1), False])))
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
        return self._read_solidly_fees(self._drop_stable_pairs(pools))

    def _read_solidly_fees(self, pools: List[Pool]) -> List[Pool]:
        """Solidly factories set each pool's fee (basis points; custom fees are
        possible). Read once at discovery; exact checks catch a later change."""
        solidly = [p for p in pools if p.kind == "solidly"]
        if not solidly:
            return pools
        factories = {d.name: d.factory for d in self.cfg.dexes.values()}
        get_fee = self.selector("getFee(address,bool)")
        results = self.call_many([(factories[p.dex], get_fee + self.encode(
            ["address", "bool"], [self.cs(p.address), False])) for p in solidly])
        for pool, data in zip(solidly, results):
            if data:
                pool.fee_ppm = self.decode(["uint256"], data)[0] * 100
        return pools

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

    def _quote_hop(self, pool: Pool, token_in: str, token_out: str, amount: int, block) -> int:
        """eth_call the pool's quoter. Uniswap QuoterV2 and Algebra's Quoter differ."""
        if pool.kind == "v3":
            data = self.selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + \
                self.encode(["(address,address,uint256,uint24,uint160)"],
                            [(self.cs(token_in), self.cs(token_out), int(amount), pool.fee_ppm, 0)])
        else:
            data = self.selector("quoteExactInputSingle(address,address,uint256,uint160)") + \
                self.encode(["address", "address", "uint256", "uint160"],
                            [self.cs(token_in), self.cs(token_out), int(amount), 0])
        raw = bytes(self.w3.eth.call({"to": self.cs(pool.quoter), "data": data}, block_identifier=block))
        return self.decode(["uint256"], raw[:32])[0]

    # ----- exact checks ------------------------------------------------------

    def verify_route(self, route, sizes: Sequence[int], block: int) -> List[Outcome]:
        """Exact outcome of `route` at each size, all pinned to `block` (the block the
        estimate was made on), checked in parallel.

        Uses RouteSimulator (one eth_call with a state override per size: real
        swaps, taxes and flash-loan fee included). If the RPC doesn't support
        state overrides, falls back to the dexes' quoter contracts, which can't
        see transfer taxes."""
        if self.cfg.exact_sim and self.sim_supported is not False:
            try:
                outcomes = list(self._verify_executor.map(lambda a: self._simulate_at(route, a, block), sizes))
                if self.sim_supported is None:
                    self.sim_supported = True
                    log.info("exact checks: RouteSimulator via eth_call state override")
                return outcomes
            except SimUnsupported as exc:
                self.sim_supported = False
                log.warning("this RPC doesn't support eth_call state overrides (%s); exact checks fall "
                            "back to quoter contracts, which can't detect transfer-tax tokens", exc)
        return list(self._verify_executor.map(lambda a: self._quote_at(route, a, block), sizes))

    def _simulate_at(self, route, amount: int, block: int) -> Outcome:
        data = SIMULATE_SELECTOR + self.encode(SIMULATE_TYPES, [
            self.cs(self.cfg.balancer_vault), self.cs(route.start), int(amount), route_steps(route, self.cs)])
        tx = {"from": CALLER, "to": SIM_ADDRESS, "data": "0x" + data.hex()}
        override = {SIM_ADDRESS: {"code": "0x" + RUNTIME_HEX}}
        response = self._raw("eth_call", [tx, hex(block), override])
        error = response.get("error")
        if error is None:  # a call to an address without code "succeeds": the override was ignored
            raise SimUnsupported("state override ignored")
        revert = _revert_bytes(error)
        if revert is None:
            if looks_unsupported(error):
                raise SimUnsupported(str(error.get("message", error))[:120])
            return Outcome(amount, block, "sim", failed_hop=-1, reason=str(error.get("message", error))[:120])
        return decode_result(revert, self.decode, amount, block)

    def _quote_at(self, route, amount: int, block: int) -> Outcome:
        """Quoter fallback: V2-style hops use this block's reserves (exact unless the
        token is taxed); concentrated hops ask the dex's quoter at the same block."""
        received, x, method = [], amount, "quoter"
        for i, (pool, token_in, token_out) in enumerate(route.hops()):
            if pool.concentrated and pool.quoter:
                try:
                    x = self._quote_hop(pool, token_in, token_out, x, block)
                except Exception as exc:  # e.g. not enough liquidity for this size
                    return Outcome(amount, block, method, received, failed_hop=i, reason=str(exc)[:120])
            else:
                if pool.concentrated:
                    method = "quoter (partly model)"
                x = pool.amount_out(token_in, x)
            received.append(x)
        return Outcome(amount, block, method, received, [], flash_fee(amount, self.flash_fee_rate))

    # ----- history lookups (closer tracing, feed checks) -----------------------

    def _result(self, method: str, params: list):
        response = self._raw(method, params)
        if response.get("error"):
            raise RuntimeError(f"{method}: {response['error'].get('message', response['error'])}")
        return response.get("result")

    def logs_for(self, addresses: Sequence[str], from_block: int, to_block: int) -> List[dict]:
        return self._result("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                             "address": [self.cs(a) for a in addresses]}]) or []

    def receipt_raw(self, tx_hash: str) -> Optional[dict]:
        """Receipt as the node sends it: Arbitrum adds `timeboosted` (express lane)."""
        return self._result("eth_getTransactionReceipt", [tx_hash])

    def block_hash(self, number: int) -> Optional[str]:
        block = self._result("eth_getBlockByNumber", [hex(number), False])
        return block.get("hash") if block else None

    def block_base_fee(self, number: int) -> Optional[int]:
        """baseFeePerGas of a block (wei), for working out what priority fee a transaction paid."""
        block = self._result("eth_getBlockByNumber", [hex(number), False])
        fee = block.get("baseFeePerGas") if block else None
        return int(fee, 16) if fee else None

    def block_tx_count(self, number: int) -> Optional[int]:
        count = self._result("eth_getBlockTransactionCountByNumber", [hex(number)])
        return None if count is None else int(count, 16)

    def block_number_by_hash(self, block_hash: str) -> Optional[int]:
        block = self._result("eth_getBlockByHash", [block_hash, False])
        return int(block["number"], 16) if block else None

    def head(self) -> int:
        return int(self.w3.eth.block_number)

    # ----- per block --------------------------------------------------------

    def refresh_all(self) -> int:
        """Re-read every discovered pool, not just the tracked ones."""
        return self.refresh(full=True)

    def attach_logs(self, stream) -> None:
        """Follow pool events from `stream` (a started LogStream) instead of
        re-reading every tracked pool each block."""
        self.logs = stream

    def refresh(self, full: bool = False, block: Optional[int] = None, wait_s: float = 0.75) -> int:
        """Bring the tracked pools (or all, if full) up to date with a new block.
        Returns the block the pools now reflect.

        With `block` (announced by the sequencer feed) the state is pinned to it.
        By default that's one Multicall read, retried for up to `wait_s` while the
        RPC node catches up (feeds run ahead of RPC providers). With a pool event
        stream attached, the block's events are applied instead and the pools are
        only re-read periodically. Without `block`, the RPC's latest block is read."""
        if self.logs is not None and self.tracked is not None:
            self.logs.watch(self.tracked)  # also starts the subscription (chains without a feed need it)
        if self.logs is not None and block is not None and not full and self.tracked is not None:
            served = self._refresh_from_logs(block, wait_s)
            if served is not None:
                return served
        return self._refresh_rpc(full, block, wait_s)

    def _refresh_from_logs(self, block: int, wait_s: float) -> Optional[int]:
        """Serve `block` from pool events. None = read it over RPC instead."""
        stream = self.logs
        if not stream.ready():
            return None  # (re)connecting: RPC reads until the subscription is live
        if block == self._last_block:
            return block
        generation = stream.generation
        periodic = time.monotonic() - self._logs_synced_at >= self.cfg.logs_resync_s
        if generation != self._logs_generation or periodic:
            before = None
            if generation == self._logs_generation and stream.wait_ready(block, wait_s):
                self._apply_events(stream.take(block))  # state as events built it...
                before = {a: pool_state(p) for a, p in self._tracked_pools().items()}
            got = self._refresh_rpc(False, block, wait_s)  # ...vs as the RPC reads it
            if before is not None and got == block:
                changed = drifted(before, self._tracked_pools().values())
                self.last_drift = (len(changed), len(before))
                if changed:
                    names = {addr: sym for sym, addr in self.cfg.tokens.items()}
                    log.info("pool events drifted from the RPC on %d of %d pools: %s; resynced (late events "
                             "so far: %d)", len(changed), len(before), "; ".join(
                                 f"{p.address} {p.label} {names.get(p.token0, p.token0[:8])}/"
                                 f"{names.get(p.token1, p.token1[:8])} ({what})" for p, what in changed[:5]),
                             self.state_stats["late_events"])
            self._logs_generation = generation
            self._logs_synced_at = time.monotonic()
            self.state_stats["resyncs"] += 1
            return got
        if not stream.wait_ready(block, wait_s):
            self.state_stats["timeouts"] += 1
            return None
        self._apply_events(stream.take(block))
        if stream.generation != generation:
            return None  # a reorg or reconnect landed meanwhile: read it for real
        self._last_block = block
        self.state_stats["logs"] += 1
        return block

    def _tracked_pools(self) -> Dict[str, Pool]:
        return {p.address: p for p in self.pools if self.tracked is None or p.address in self.tracked}

    def _apply_events(self, events) -> None:
        pools = self._tracked_pools()
        for event in events:
            pool = pools.get(event.pool)
            if pool is None:
                continue
            if event.position <= self._marks.get(event.pool, (-1, -1)):
                self.state_stats["stale_events"] += 1  # already part of an RPC read
                continue
            if self._last_block is not None and event.block <= self._last_block:
                # This block was already priced without it: logs_settle_ms is too short.
                self.state_stats["late_events"] += 1
            if not apply_event(pool, event):
                self.state_stats["unapplied"] += 1
                self.logs.invalidate()  # can't follow this pool: re-read everything next block
                continue
            self._marks[event.pool] = event.position

    def _refresh_rpc(self, full: bool, block: Optional[int], wait_s: float) -> int:
        if block is None:
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
            if pool.kind in V2_STYLE:
                calls.append((pool.address, get_reserves))
            else:
                state = slot0 if pool.kind == "v3" else global_state
                calls += [(pool.address, state), (pool.address, liquidity)]
        flash_tokens = [self.cfg.token(s) for s in self.cfg.flash_tokens]
        calls += [(t, balance_of) for t in flash_tokens]

        data, block = self._read_at(calls, block, wait_s)
        results = iter(data)
        for pool in pools:
            if pool.kind in V2_STYLE:
                data = next(results)
                if data is None:
                    pool.update_v2(0, 0)
                elif pool.kind == "solidly":  # (uint256 reserve0, uint256 reserve1, uint256 timestamp)
                    r0, r1 = self.decode(["uint256", "uint256"], data[:64])
                    pool.update_v2(r0, r1)
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
                    sqrt_price, pool.tick = self.decode(["uint160", "int24"], state[:64])
                    pool.update_v3(sqrt_price, self.decode(["uint128"], liq)[0])
                else:
                    # Algebra (Camelot V3) globalState: price, tick, feeZto, feeOtz, ...
                    # Fees are already in parts per million and differ by direction.
                    sqrt_price, pool.tick, fee_zto, fee_otz = self.decode(
                        ["uint160", "int24", "uint16", "uint16"], state[:128])
                    pool.update_v3(sqrt_price, self.decode(["uint128"], liq)[0])
                    pool.fee_ppm, pool.fee1_ppm = fee_zto, fee_otz
        for token in flash_tokens:
            data = next(results)
            self.vault_balances[token] = self.decode(["uint256"], data)[0] if data else 0
        for pool in pools:
            self._marks[pool.address] = (block, LATEST)
        if self.logs is not None:
            self.logs.take(block)  # events up to here are part of this read
        if self.logs is not None and self._last_block is not None and block < self._last_block:
            self.logs.invalidate()  # an older read replaced newer event-built state
        self._last_block = block
        self.state_stats["rpc"] += 1
        return block

    def _read_at(self, calls, block: int, wait_s: float) -> Tuple[List[Optional[bytes]], int]:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                return self.call_many(calls, block), block
            except Exception as exc:
                if not any(marker in str(exc).lower() for marker in _NOT_YET):
                    raise
                if time.monotonic() < deadline:
                    time.sleep(0.015)
                    continue
                # The RPC is further behind the feed than we're willing to wait:
                # read whatever it has so the bot keeps moving.
                self.rpc_behind_timeouts += 1
                latest = self.w3.eth.block_number
                if latest >= block:
                    raise  # it has the block but still can't read it: a real error
                return self.call_many(calls, latest), latest

    def gas_price_wei(self) -> int:
        # Arbitrum's gas price barely moves; don't spend a request on it every block.
        if time.monotonic() - self._gas_price_at >= _GAS_PRICE_TTL_S:
            self.last_gas_price_wei = int(self.w3.eth.gas_price)
            self._gas_price_at = time.monotonic()
        return self.last_gas_price_wei
