"""Load and validate the TOML config."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List

from .amm import ROUTER_KINDS

MODES = ("scan", "simulate", "live")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass
class RiskLimits:
    min_profit_usd: float = 0.50          # net of gas
    max_loan_usd: float = 25_000.0
    min_pool_liquidity_usd: float = 25_000.0
    max_gas_price_gwei: float = 1.0
    max_daily_gas_usd: float = 5.0        # gas burnt on reverted transactions
    max_consecutive_reverts: int = 3


@dataclass
class DexConfig:
    name: str
    type: str  # "v2" | "v3"
    factory: str
    router: str
    router_kind: str
    fee: int = 3000  # v2 swap fee in parts per million
    fee_tiers: List[int] = field(default_factory=list)  # v3


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
    route_rebuild_s: float = 3600.0
    summary_interval_s: float = 60.0
    log_dir: str = "logs"
    risk: RiskLimits = field(default_factory=RiskLimits)

    def token(self, symbol: str) -> str:
        return self.tokens[symbol]


def _check_address(label: str, value: str) -> None:
    if not _ADDRESS.match(value or ""):
        raise ValueError(f"{label} is not a valid address: {value!r}")


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

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
    cfg = Config(risk=RiskLimits(**risk_raw), dexes=dexes, **raw)
    validate(cfg)
    # Lowercase every address so lookups never depend on checksum casing.
    cfg.tokens = {sym: addr.lower() for sym, addr in cfg.tokens.items()}
    cfg.balancer_vault = cfg.balancer_vault.lower()
    cfg.contract_address = cfg.contract_address.lower()
    cfg.owner_address = cfg.owner_address.lower()
    for dex in cfg.dexes.values():
        dex.factory, dex.router = dex.factory.lower(), dex.router.lower()
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
        if dex.type not in ("v2", "v3"):
            raise ValueError(f"dex {dex.name}: type must be 'v2' or 'v3'")
        if dex.router_kind not in ROUTER_KINDS:
            raise ValueError(f"dex {dex.name}: router_kind must be one of {list(ROUTER_KINDS)}")
        if dex.type == "v3" and not dex.fee_tiers:
            raise ValueError(f"dex {dex.name}: v3 dexes need fee_tiers")
        _check_address(f"dex {dex.name} factory", dex.factory)
        _check_address(f"dex {dex.name} router", dex.router)
    if cfg.contract_address:
        _check_address("contract_address", cfg.contract_address)
    if cfg.owner_address:
        _check_address("owner_address", cfg.owner_address)
