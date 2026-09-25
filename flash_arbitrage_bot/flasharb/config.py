"""Load and validate the TOML config."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List

from .amm import POOL_TYPES, ROUTER_KINDS

MODES = ("scan", "paper", "simulate", "live")
PAPER_TIMEBOOST = ("auto", "on", "off")
STATE_SOURCES = ("rpc", "logs")
ORDERINGS = ("arrival", "fee")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass
class RiskLimits:
    min_profit_usd: float = 0.50          # net of gas
    max_loan_usd: float = 25_000.0
    min_pool_liquidity_usd: float = 25_000.0
    # Each side of a pool must hold at least this much (USD). Filters pools whose
    # price promises a gap they can't pay out (e.g. a V3 pool out of its range).
    min_pool_reserve_usd: float = 2_500.0
    max_gas_price_gwei: float = 1.0
    max_daily_gas_usd: float = 5.0        # gas burnt on reverted transactions
    max_consecutive_reverts: int = 3


@dataclass
class DexConfig:
    name: str
    type: str  # one of amm.POOL_TYPES
    factory: str
    router: str
    router_kind: str
    fee: int = 3000  # v2 swap fee in parts per million
    fee_tiers: List[int] = field(default_factory=list)  # v3
    quoter: str = ""  # v3: QuoterV2 address, lets scan mode price trades exactly


@dataclass
class DiscoveryConfig:
    enabled: bool = False
    min_liquidity_usd: float = 20_000.0   # real WETH/stable value paired against the token
    max_tokens: int = 150
    max_pairs_per_factory: int = 30_000   # newest pairs first
    # Concentrated-liquidity exchanges (Uniswap V3, Algebra) keep no list of their
    # pools, so their tokens are found from recent trading instead: every pool that
    # emitted a Swap in the last this-many blocks (0 = off; ~1800 is an hour on Polygon).
    active_pool_blocks: int = 0
    cache_hours: float = 24.0
    cache_file: str = "logs/discovered_tokens.json"


@dataclass
class Config:
    mode: str
    chain_name: str
    chain_id: int
    balancer_vault: str
    native_wrapped: str
    tokens: Dict[str, str]
    stable_tokens: List[str]
    flash_tokens: List[str]
    dexes: Dict[str, DexConfig]
    rpc_url_env: str = "RPC_URL"
    contract_address: str = ""
    owner_address: str = ""
    max_hops: int = 3
    poll_interval_s: float = 0.25
    max_candidates_per_block: int = 3
    sim_cooldown_blocks: int = 20
    gas_units_estimate: int = 450_000
    # How the chain's sequencer orders transactions inside a block: "arrival"
    # (Arbitrum: first come, first served, so speed wins) or "fee" (Base, Optimism
    # and other OP-stack chains: highest priority fee first, so the bid wins).
    ordering: str = "arrival"
    # "fee" chains: offer this share of each trade's expected profit above
    # min_profit_usd as priority fee (0 = no bid; 1 = give it all away).
    priority_fee_share: float = 0.5
    # "fee" chains: never bid less than this per gas (Polygon's validators drop
    # transactions tipping under ~25-30 gwei).
    min_priority_fee_gwei: float = 0.0
    # "fee" chains: "share" always bids priority_fee_share of the expected profit;
    # "learned" bids just above what winning transactions paid for gaps of a similar
    # size (bid_percentile of them, plus bid_margin), once paper trading has seen
    # enough of them, and never more than "share" would.
    bid_strategy: str = "share"
    bid_percentile: float = 0.75
    bid_margin: float = 0.10
    # Trades in flight at once (live and paper modes). Above 1, a new trade must
    # not touch any pool an earlier one in flight uses.
    max_inflight: int = 1
    # A fixed cost per transaction on top of L2 gas, e.g. the L1 data fee every
    # OP-stack transaction pays (a few tenths of a cent on Base).
    extra_tx_cost_usd: float = 0.0
    route_rebuild_s: float = 3600.0
    summary_interval_s: float = 60.0
    log_dir: str = "logs"
    # Arbitrum sequencer feed: learn about each block as it's sequenced instead of
    # polling the RPC. Empty = poll. L2 block = feed sequence number + offset.
    sequencer_feed_url: str = ""
    feed_block_offset: int = 22207817
    # Where each block's pool state comes from: "rpc" re-reads every tracked pool
    # (one Multicall per block); "logs" follows the pools' events over a websocket
    # subscription and re-reads them only every logs_resync_s (needs the feed).
    state_source: str = "rpc"
    ws_rpc_url_env: str = "WS_RPC_URL"   # unset = RPC_URL with https:// -> wss://
    logs_settle_ms: float = 20.0         # after the node's newHeads, wait this long for the block's events
    logs_resync_s: float = 60.0          # re-read the pools this often and measure drift
    # Scan mode: check candidates with RouteSimulator (exact, one eth_call with a
    # state override) instead of quoter contracts.
    exact_sim: bool = True
    # Scan mode: when a verified gap closes, find the transaction that closed it.
    trace_closers: bool = True
    # Live mode: where to send transactions (e.g. the sequencer's own endpoint);
    # empty = the RPC_URL node. Reads always use RPC_URL.
    send_rpc_url: str = ""
    # Live mode: dry-run each trade with eth_call before sending (one more round
    # trip). Off: send at once and let the contract's profit check revert it.
    presimulate_live: bool = False
    live_gas_limit: int = 2_000_000
    # Paper mode: trade like live mode, but place paper orders instead of sending.
    # A paper transaction lands this long after its block appeared: the bot's own
    # decision time (measured from the sequencer feed), plus the trip to the
    # sequencer, plus Timeboost's hold when it applies.
    paper_send_latency_ms: float = 50.0      # one way, this machine -> the sequencer
    # Timeboost holds ordinary transactions back only while someone controls the
    # express lane; with no controller Arbitrum is first-come-first-served. "auto"
    # adds the hold when the feed saw express-lane transactions within the last
    # auction round, or can't tell; "on" and "off" force it.
    paper_timeboost: str = "auto"
    paper_timeboost_delay_ms: float = 200.0  # the hold itself
    paper_block_time_ms: float = 250.0
    # A competitor took the gap inside your landing block: order within a block is
    # unknowable, so by default that counts as lost.
    paper_same_block_wins: bool = False
    paper_gas_units: int = 0                 # gas per paper trade, filled or reverted; 0 = gas_units_estimate
    risk: RiskLimits = field(default_factory=RiskLimits)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)

    def token(self, symbol: str) -> str:
        return self.tokens[symbol]


def _check_address(label: str, value: str) -> None:
    if not _ADDRESS.match(value or ""):
        raise ValueError(f"{label} is not a valid address: {value!r}")


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    discovery_raw = raw.pop("discovery", {})
    unknown = set(discovery_raw) - {f.name for f in fields(DiscoveryConfig)}
    if unknown:
        raise ValueError(f"unknown [discovery] keys: {sorted(unknown)}")
    risk_raw = raw.pop("risk", {})
    unknown = set(risk_raw) - {f.name for f in fields(RiskLimits)}
    if unknown:
        raise ValueError(f"unknown [risk] keys: {sorted(unknown)}")
    dex_keys = {f.name for f in fields(DexConfig)} - {"name"}
    dexes = {}
    for name, opts in raw.pop("dexes", {}).items():
        unknown = set(opts) - dex_keys
        if unknown:
            raise ValueError(f"unknown keys in [dexes.{name}]: {sorted(unknown)} "
                             "(top-level settings must come before any [section])")
        dexes[name] = DexConfig(name=name, **opts)
    unknown = set(raw) - {f.name for f in fields(Config)}
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    cfg = Config(risk=RiskLimits(**risk_raw), discovery=DiscoveryConfig(**discovery_raw),
                 dexes=dexes, **raw)
    validate(cfg)
    # Lowercase every address so lookups never depend on checksum casing.
    cfg.tokens = {sym: addr.lower() for sym, addr in cfg.tokens.items()}
    cfg.balancer_vault = cfg.balancer_vault.lower()
    cfg.contract_address = cfg.contract_address.lower()
    cfg.owner_address = cfg.owner_address.lower()
    for dex in cfg.dexes.values():
        dex.factory, dex.router, dex.quoter = dex.factory.lower(), dex.router.lower(), dex.quoter.lower()
    return cfg


def validate(cfg: Config) -> None:
    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {cfg.mode!r}")
    if not 2 <= cfg.max_hops <= 4:
        raise ValueError("max_hops must be between 2 and 4")
    _check_address("balancer_vault", cfg.balancer_vault)
    for symbol, address in cfg.tokens.items():
        _check_address(f"token {symbol}", address)
    for symbol in [cfg.native_wrapped, *cfg.stable_tokens, *cfg.flash_tokens]:
        if symbol not in cfg.tokens:
            raise ValueError(f"{symbol!r} is used but not listed under [tokens]")
    if not cfg.stable_tokens:
        raise ValueError("list at least one stable token so profits can be valued in USD")
    if not cfg.flash_tokens:
        raise ValueError("list at least one flash_token to borrow")
    if not cfg.dexes:
        raise ValueError("configure at least one [dexes.<name>] section")
    for dex in cfg.dexes.values():
        if dex.type not in POOL_TYPES:
            raise ValueError(f"dex {dex.name}: type must be one of {POOL_TYPES}")
        if dex.router_kind not in ROUTER_KINDS:
            raise ValueError(f"dex {dex.name}: router_kind must be one of {list(ROUTER_KINDS)}")
        if dex.type == "v3" and not dex.fee_tiers:
            raise ValueError(f"dex {dex.name}: v3 dexes need fee_tiers")
        _check_address(f"dex {dex.name} factory", dex.factory)
        _check_address(f"dex {dex.name} router", dex.router)
        if dex.quoter:
            _check_address(f"dex {dex.name} quoter", dex.quoter)
    if cfg.sequencer_feed_url and not cfg.sequencer_feed_url.startswith(("ws://", "wss://")):
        raise ValueError("sequencer_feed_url must start with ws:// or wss://")
    if cfg.state_source not in STATE_SOURCES:
        raise ValueError(f"state_source must be one of {STATE_SOURCES}, got {cfg.state_source!r}")
    if cfg.ordering not in ORDERINGS:
        raise ValueError(f"ordering must be one of {ORDERINGS}, got {cfg.ordering!r}")
    if not 0 <= cfg.priority_fee_share <= 1:
        raise ValueError("priority_fee_share must be between 0 and 1")
    if cfg.bid_strategy not in ("share", "learned"):
        raise ValueError(f"bid_strategy must be \"share\" or \"learned\", got {cfg.bid_strategy!r}")
    if not 0 < cfg.bid_percentile <= 1 or cfg.bid_margin < 0:
        raise ValueError("bid_percentile must be in (0, 1] and bid_margin can't be negative")
    if cfg.max_inflight < 1:
        raise ValueError("max_inflight must be at least 1")
    if cfg.min_priority_fee_gwei < 0:
        raise ValueError("min_priority_fee_gwei can't be negative")
    if cfg.extra_tx_cost_usd < 0:
        raise ValueError("extra_tx_cost_usd can't be negative")
    if cfg.discovery.active_pool_blocks < 0:
        raise ValueError("[discovery] active_pool_blocks can't be negative")
    if cfg.logs_settle_ms < 0 or cfg.logs_resync_s <= 0:
        raise ValueError("logs_settle_ms can't be negative and logs_resync_s must be positive")
    if cfg.send_rpc_url and not cfg.send_rpc_url.startswith(("http://", "https://")):
        raise ValueError("send_rpc_url must start with http:// or https://")
    if cfg.paper_timeboost not in PAPER_TIMEBOOST:
        raise ValueError(f"paper_timeboost must be one of {PAPER_TIMEBOOST}, got {cfg.paper_timeboost!r}")
    if cfg.paper_send_latency_ms < 0 or cfg.paper_timeboost_delay_ms < 0:
        raise ValueError("paper_send_latency_ms and paper_timeboost_delay_ms can't be negative")
    if cfg.paper_block_time_ms <= 0:
        raise ValueError("paper_block_time_ms must be positive")
    if cfg.paper_gas_units < 0:
        raise ValueError("paper_gas_units can't be negative")
    if cfg.live_gas_limit < 100_000:
        raise ValueError("live_gas_limit is too low for a flash-loan arbitrage")
    if cfg.contract_address:
        _check_address("contract_address", cfg.contract_address)
    if cfg.owner_address:
        _check_address("owner_address", cfg.owner_address)
