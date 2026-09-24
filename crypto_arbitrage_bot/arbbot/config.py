"""Load and validate the TOML config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List

from .risk import RiskLimits

MODES = ("scan", "paper", "live")
FEEDS = ("rest", "websocket")


@dataclass
class Config:
    mode: str
    symbols: List[str]
    exchanges: Dict[str, dict]
    price_feed: str = "websocket"
    poll_interval_s: float = 1.0
    min_cycle_interval_s: float = 0.05
    balance_refresh_s: float = 30.0
    order_book_depth: int = 20
    max_trade_quote: float = 100.0
    slippage_buffer_pct: float = 0.05
    summary_interval_s: float = 60.0
    log_dir: str = "logs"
    risk: RiskLimits = field(default_factory=RiskLimits)
    paper_balances: Dict[str, Dict[str, float]] = field(default_factory=dict)


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    risk_raw = raw.pop("risk", {})
    known = {f.name for f in fields(RiskLimits)}
    unknown = set(risk_raw) - known
    if unknown:
        raise ValueError(f"unknown [risk] keys: {sorted(unknown)}")

    cfg = Config(risk=RiskLimits(**risk_raw), **raw)
    if cfg.mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {cfg.mode!r}")
    if cfg.price_feed not in FEEDS:
        raise ValueError(f"price_feed must be one of {FEEDS}, got {cfg.price_feed!r}")
    if len(cfg.exchanges) < 2:
        raise ValueError("arbitrage needs at least two exchanges")
    if not cfg.symbols:
        raise ValueError("configure at least one symbol")
    if cfg.mode == "paper" and not cfg.paper_balances:
        raise ValueError("paper mode needs [paper_balances.<exchange>] sections")
    return cfg
