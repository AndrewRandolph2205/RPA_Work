"""Main loop: new block -> price all pools -> find profitable cycles -> simulate -> send."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from .config import Config
from .journal import Journal
from .risk import RiskManager
from .routes import (Route, find_cycles, from_usd, marginal_edge_pct, optimal_input_from,
                     pool_liquidity_usd, to_usd, usd_prices)

log = logging.getLogger(__name__)


@dataclass
class Opportunity:
    route: Route
    amount_in: int
    amount_out: int
    profit_usd: float  # before gas
    gas_usd: float

    @property
    def net_usd(self) -> float:
        return self.profit_usd - self.gas_usd


@dataclass
class Stats:
    blocks: int = 0
    candidates: int = 0
    sim_passed: int = 0
    sim_failed: int = 0
    sent: int = 0
    succeeded: int = 0
    reverted: int = 0
    realized_net_usd: float = 0.0
    gas_spent_usd: float = 0.0
    simulated_net_usd: float = 0.0
    # "Closest miss" diagnostics, reset after every summary.
    best_edge_pct: Optional[float] = None
    best_edge_route: str = ""
    best_net_usd: Optional[float] = None
    best_net_route: str = ""

    def reset_window(self) -> None:
        self.best_edge_pct, self.best_edge_route = None, ""
        self.best_net_usd, self.best_net_route = None, ""


class FlashBot:
    def __init__(self, cfg: Config, chain, executor, risk: RiskManager, journal: Journal,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.chain = chain
        self.executor = executor  # None in scan mode
        self.risk = risk
        self.journal = journal
        self._clock = clock
        self.symbols = {addr: sym for sym, addr in cfg.tokens.items()}
        self.native = cfg.token(cfg.native_wrapped)
        self.stables = {cfg.token(s) for s in cfg.stable_tokens}
        self.flash_tokens = [cfg.token(s) for s in cfg.flash_tokens]
        self.routes: List[Route] = []
        self.stats = Stats()
        self.last_block: Optional[int] = None
        self.last_sim_failed = False
        self._routes_built_at = float("-inf")
        self._started = clock()
        self._last_summary = self._started
        self._prev_logged: Set[Tuple] = set()
        # Routes whose simulation failed recently -> block until which to skip
        # them, so a persistent false positive doesn't hammer the RPC each block.
        self._cooldown: Dict[Tuple, int] = {}
        # Simulate mode: gaps that already passed and are still open. A real trade
        # would have closed them, so count each one once, not once per block.
        self._open_simulated: Set[Tuple] = set()

    # ----- analysis ---------------------------------------------------------

    def _prices(self) -> Dict[str, float]:
        return usd_prices(self.chain.pools, self.chain.decimals, self.stables)

    def _liquid_pools(self, prices) -> Set[str]:
        floor = self.cfg.risk.min_pool_liquidity_usd
        return {p.address for p in self.chain.pools
                if pool_liquidity_usd(p, prices, self.chain.decimals) >= floor}

    def build_routes(self) -> None:
        prices = self._prices()
        liquid = self._liquid_pools(prices)
        pools = [p for p in self.chain.pools if p.address in liquid]
        self.routes = find_cycles(pools, self.flash_tokens, self.cfg.max_hops)
        self._routes_built_at = self._clock()
        log.info("%d liquid pools (of %d), %d candidate routes", len(pools),
                 len(self.chain.pools), len(self.routes))

    def find_opportunities(self, prices: Dict[str, float], gas_price_wei: int) -> List[Opportunity]:
        dec = self.chain.decimals
        gas_usd = self.cfg.gas_units_estimate * gas_price_wei / 1e18 * prices.get(self.native, 0.0)
        liquid = self._liquid_pools(prices)
        found = []
        for route in self.routes:
            if any(p.address not in liquid for p in route.pools):
                continue
            start = route.start
            max_in = min(self.chain.vault_balances.get(start, 0),
                         from_usd(self.cfg.risk.max_loan_usd, start, prices, dec))
            coeffs = route.mobius()
            edge = marginal_edge_pct(coeffs)
            stats = self.stats
            if edge is not None and (stats.best_edge_pct is None or edge > stats.best_edge_pct):
                stats.best_edge_pct, stats.best_edge_route = edge, route.describe(self.symbols)
            amount_in = optimal_input_from(coeffs, max_in)
            if not amount_in:
                continue
            amount_out = route.amount_out(amount_in)
            profit_usd = to_usd(amount_out - amount_in, start, prices, dec)
            net = profit_usd - gas_usd
            if stats.best_net_usd is None or net > stats.best_net_usd:
                stats.best_net_usd, stats.best_net_route = net, route.describe(self.symbols)
            if net >= self.cfg.risk.min_profit_usd:
                found.append(Opportunity(route, amount_in, amount_out, profit_usd, gas_usd))
        found.sort(key=lambda o: o.net_usd, reverse=True)
        return found

    # ----- execution --------------------------------------------------------

    def _fmt_amount(self, opp: Opportunity, amount: int) -> str:
        sym = self.symbols.get(opp.route.start, "?")
        return f"{amount / 10 ** self.chain.decimals[opp.route.start]:.6g} {sym}"

    def _log_opportunity(self, opp: Opportunity, decision: str) -> None:
        self.journal.opportunity(
            block=self.last_block, route=opp.route.describe(self.symbols),
            amount_in=self._fmt_amount(opp, opp.amount_in), profit_usd=round(opp.profit_usd, 4),
            gas_usd=round(opp.gas_usd, 4), net_usd=round(opp.net_usd, 4), decision=decision)

    def _simulate(self, opp: Opportunity, min_profit_raw: int):
        """eth_call the real transaction; on Unprofitable retry at half size (V3 ticks
        can make large sizes worse than the virtual-reserve estimate)."""
        amount = opp.amount_in
        result = None
        for _ in range(3):
            result = self.executor.simulate(opp.route, amount, min_profit_raw)
            if result.ok:
                return amount, result
            if result.error != "Unprofitable":
                break
            amount //= 2
            if amount <= 0:
                break
        return None, result

    def _act(self, opp: Opportunity, prices: Dict[str, float]) -> bool:
        """Returns True when a transaction was attempted (one per block)."""
        dec = self.chain.decimals
        # On-chain floor: the trade must clear estimated gas plus the minimum profit.
        min_profit_raw = from_usd(opp.gas_usd + self.cfg.risk.min_profit_usd, opp.route.start, prices, dec)
        amount, sim = self._simulate(opp, min_profit_raw)
        self.last_sim_failed = amount is None
        if amount is None:
            self.stats.sim_failed += 1
            self._log_opportunity(opp, f"simulation failed: {sim.error}")
            return False
        self.stats.sim_passed += 1
        gas_usd = sim.gas_used * self.chain.last_gas_price_wei / 1e18 * prices.get(self.native, 0.0)

        if self.cfg.mode == "simulate":
            est_net = to_usd(opp.route.amount_out(amount) - amount, opp.route.start, prices, dec) - gas_usd
            self.stats.simulated_net_usd += est_net
            self._log_opportunity(opp, f"simulation passed (would send, est net ${est_net:.2f})")
            log.info("SIMULATED %s in=%s est_net=$%.2f", opp.route.describe(self.symbols),
                     self._fmt_amount(opp, amount), est_net)
            return True

        ok, reason = self.risk.can_send()
        if not ok:
            self._log_opportunity(opp, reason)
            return False
        self._log_opportunity(opp, "sent")
        self.stats.sent += 1
        res = self.executor.send(opp.route, amount, min_profit_raw, sim.gas_used)
        gas_usd = res.gas_cost_wei / 1e18 * prices.get(self.native, 0.0)
        profit_usd = to_usd(res.profit_raw, opp.route.start, prices, dec)
        net = profit_usd - gas_usd
        self.stats.gas_spent_usd += gas_usd
        self.risk.record_send(res.success, gas_usd)
        if res.success:
            self.stats.succeeded += 1
            self.stats.realized_net_usd += net
            log.info("PROFIT tx=%s net=$%.2f %s", res.tx_hash, net, opp.route.describe(self.symbols))
        else:
            self.stats.reverted += 1
            self.stats.realized_net_usd -= gas_usd
            log.warning("REVERTED tx=%s gas=$%.4f %s", res.tx_hash, gas_usd, res.error)
        self.journal.trade(
            block=self.last_block, route=opp.route.describe(self.symbols),
            amount_in=self._fmt_amount(opp, amount), tx_hash=res.tx_hash, success=res.success,
            profit_usd=round(profit_usd, 4), gas_usd=round(gas_usd, 4),
            net_usd=round(net if res.success else -gas_usd, 4), error=res.error)
        return True

    # ----- loop -------------------------------------------------------------

    def step(self) -> bool:
        """Process the latest block. Returns False if the block was already seen."""
        block = self.chain.refresh()
        if block == self.last_block:
            return False
        self.last_block = block
        self.stats.blocks += 1

        if self._clock() - self._routes_built_at >= self.cfg.route_rebuild_s:
            self.build_routes()

        gas_price = self.chain.gas_price_wei()
        if not self.risk.gas_price_ok(gas_price):
            log.debug("gas price %.3f gwei above cap; skipping block", gas_price / 1e9)
            return True

        prices = self._prices()
        opps = self.find_opportunities(prices, gas_price)[: self.cfg.max_candidates_per_block]
        self.stats.candidates += len(opps)

        keys = set()
        for opp in opps:
            key = tuple(p.address for p in opp.route.pools) + (opp.route.start,)
            keys.add(key)
            if self.executor is None:
                if key not in self._prev_logged:  # log an opportunity once while it persists
                    self._log_opportunity(opp, "scan only")
                continue
            if self._cooldown.get(key, -1) >= block or key in self._open_simulated:
                continue
            attempted = self._act(opp, prices)
            if self.last_sim_failed:
                self._cooldown[key] = block + self.cfg.sim_cooldown_blocks
            elif attempted and self.cfg.mode == "simulate":
                self._open_simulated.add(key)
            if attempted:
                break  # the chain state changes after a trade; re-evaluate next block
        self._prev_logged = keys
        self._open_simulated &= keys
        return True

    def summary(self) -> str:
        s = self.stats
        hours = max((self._clock() - self._started) / 3600, 1e-9)
        lines = [f"mode={self.cfg.mode} blocks={s.blocks} routes={len(self.routes)} "
                 f"candidates={s.candidates}"]
        if s.best_edge_pct is not None:
            lines.append(f"  closest this period: best edge after pool fees {s.best_edge_pct:+.4f}% "
                         f"({s.best_edge_route})")
        if s.best_net_usd is not None:
            lines.append(f"  best trade after gas ${s.best_net_usd:+.2f} ({s.best_net_route}); "
                         f"needs >= ${self.cfg.risk.min_profit_usd:.2f}")
        else:
            lines.append("  no route had a positive edge after pool fees this period")
        if self.cfg.mode != "scan":
            lines.append(f"  simulations passed={s.sim_passed} failed={s.sim_failed}")
        if self.cfg.mode == "simulate":
            lines.append(f"  would-have-made net=${s.simulated_net_usd:.2f} "
                         f"(${s.simulated_net_usd / hours:.2f}/hr, optimistic)")
        if self.cfg.mode == "live":
            lines.append(f"  sent={s.sent} succeeded={s.succeeded} reverted={s.reverted} "
                         f"gas=${s.gas_spent_usd:.2f}")
            lines.append(f"  realized net=${s.realized_net_usd:.2f} "
                         f"(${s.realized_net_usd / hours:.2f}/hr)")
        return "\n".join(lines)

    def run(self, max_blocks: Optional[int] = None) -> None:
        while max_blocks is None or self.stats.blocks < max_blocks:
            try:
                if not self.step():
                    time.sleep(self.cfg.poll_interval_s)
            except Exception:
                log.exception("step failed")
                time.sleep(max(self.cfg.poll_interval_s, 1.0))
            if self.risk.halted_reason:
                log.error("HALTED: %s", self.risk.halted_reason)
                break
            if self._clock() - self._last_summary >= self.cfg.summary_interval_s:
                log.info("summary\n%s", self.summary())
                self.stats.reset_window()
                self._last_summary = self._clock()
        log.info("final summary\n%s", self.summary())
