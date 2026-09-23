"""Main scan -> evaluate -> risk-check -> execute loop."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from itertools import permutations
from typing import Dict, List, Optional

from .config import Config
from .journal import Journal
from .risk import RiskManager
from .strategy import Opportunity, evaluate, max_affordable_amount

log = logging.getLogger(__name__)


@dataclass
class SymbolStats:
    scans: int = 0
    profitable: int = 0
    best_pct: float = float("-inf")


class ArbitrageBot:
    def __init__(self, cfg: Config, hub, executor, risk: RiskManager, journal: Journal):
        self.cfg = cfg
        self.hub = hub
        self.executor = executor  # None in scan mode
        self.risk = risk
        self.journal = journal
        self.stats: Dict[str, SymbolStats] = {s: SymbolStats() for s in cfg.symbols}
        self.total_pnl = 0.0
        self.trades = 0
        self._started = time.time()
        self._last_summary = self._started

    async def _find_best(self, symbol: str, balances) -> Optional[Opportunity]:
        books = await self.hub.fetch_books(symbol, self.cfg.order_book_depth)
        books = {ex: b for ex, b in books.items() if self.risk.is_fresh(b) and b.bids and b.asks}
        base, quote = symbol.split("/")
        best = None
        for buy_ex, sell_ex in permutations(books, 2):
            buy_book, sell_book = books[buy_ex], books[sell_ex]
            if buy_book.best_ask >= sell_book.best_bid:
                continue  # no gross spread; skip the expensive part
            buy_fee = self.hub.fee(buy_ex, symbol)
            if balances is None:  # scan mode: size by max_trade_quote only
                amount = self.cfg.max_trade_quote / (buy_book.best_ask * 1.01 * (1 + buy_fee))
            else:
                amount = max_affordable_amount(
                    buy_book, buy_fee,
                    quote_available=balances.get(buy_ex, {}).get(quote, 0.0),
                    base_available=balances.get(sell_ex, {}).get(base, 0.0),
                    max_trade_quote=self.cfg.max_trade_quote,
                )
            amount = self.hub.round_amount(symbol, amount, (buy_ex, sell_ex))
            if amount <= 0:
                continue
            opp = evaluate(buy_book, sell_book, amount, buy_fee, self.hub.fee(sell_ex, symbol),
                           self.cfg.slippage_buffer_pct)
            if opp and (best is None or opp.net_profit > best.net_profit):
                best = opp
        return best

    async def run_cycle(self) -> List[Opportunity]:
        balances = await self.executor.balances() if self.executor else None
        executed = []
        for symbol in self.cfg.symbols:
            opp = await self._find_best(symbol, balances)
            stats = self.stats[symbol]
            stats.scans += 1
            if opp is None:
                continue
            stats.best_pct = max(stats.best_pct, opp.net_profit_pct)
            if opp.net_profit <= 0:
                continue
            stats.profitable += 1

            ok, reason = self.risk.allow(opp)
            if not ok or self.executor is None:
                self.journal.opportunity(opp, reason if not ok else "scan only")
                continue

            self.journal.opportunity(opp, "execute")
            result = await self.executor.execute(opp)
            self.risk.record(result.realized_pnl, result.success)
            self.journal.trade(opp, result, self.cfg.mode)
            if result.imbalanced:
                self.risk.halt(result.detail)
            if result.success:
                self.total_pnl += result.realized_pnl
                self.trades += 1
                executed.append(opp)
                log.info("TRADE %s buy %s sell %s amt %.6f pnl %.4f %s", symbol,
                         opp.buy_exchange, opp.sell_exchange, opp.amount,
                         result.realized_pnl, opp.quote)
                # Balances changed; refresh before looking at the next symbol.
                balances = await self.executor.balances()
            else:
                log.warning("execution failed: %s", result.detail)
            if self.risk.halted_reason:
                break
        return executed

    def summary(self) -> str:
        hours = max((time.time() - self._started) / 3600, 1e-9)
        lines = [f"mode={self.cfg.mode} trades={self.trades} pnl={self.total_pnl:.4f} "
                 f"({self.total_pnl / hours:.4f}/hr)"]
        for symbol, s in self.stats.items():
            best = f"{s.best_pct:.3f}%" if s.best_pct != float("-inf") else "n/a"
            lines.append(f"  {symbol}: scans={s.scans} profitable={s.profitable} best_net={best}")
        return "\n".join(lines)

    async def run(self, max_cycles: Optional[int] = None) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            started = time.time()
            try:
                await self.run_cycle()
            except Exception:
                log.exception("cycle failed")
            cycles += 1
            if self.risk.halted_reason:
                log.error("HALTED: %s", self.risk.halted_reason)
                break
            if time.time() - self._last_summary >= self.cfg.summary_interval_s:
                log.info("summary\n%s", self.summary())
                self._last_summary = time.time()
            await asyncio.sleep(max(0.0, self.cfg.poll_interval_s - (time.time() - started)))
        log.info("final summary\n%s", self.summary())
