"""Main loop: new block -> price all pools -> find profitable cycles -> simulate -> send."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from .amm import FEE_DENOMINATOR
from .config import Config
from .errors import describe, http_status
from .journal import Journal
from .risk import RiskManager
from .routes import (Route, find_cycles, from_usd, optimal_input_from, pool_liquidity_usd, to_usd,
                     usd_prices)

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
    skipped_blocks: int = 0  # chain blocks that passed while we were busy
    candidates: int = 0
    sim_passed: int = 0
    sim_failed: int = 0
    sent: int = 0
    succeeded: int = 0
    reverted: int = 0
    realized_net_usd: float = 0.0
    gas_spent_usd: float = 0.0
    simulated_net_usd: float = 0.0
    quoter_verified: int = 0
    quoter_rejected: int = 0
    # "Closest miss" diagnostics, reset after every summary.
    best_edge_pct: Optional[float] = None
    best_edge_route: str = ""
    best_net_usd: Optional[float] = None
    best_net_route: str = ""
    eval_ms_max: float = 0.0
    best_verified_usd: Optional[float] = None
    best_verified_route: str = ""

    def reset_window(self) -> None:
        self.eval_ms_max = 0.0
        self.best_verified_usd, self.best_verified_route = None, ""
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
        self._route_keys: List[Tuple] = []
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
        if self.routes:  # rebuild: untracked pools are stale, re-read everything
            refresh_all = getattr(self.chain, "refresh_all", None)
            if refresh_all:
                refresh_all()
            refresh_balances = getattr(self.chain, "refresh_pool_balances", None)
            if refresh_balances:  # first build: load() just read them
                refresh_balances()
        prices = self._prices()
        liquid = self._liquid_pools(prices)
        pools = [p for p in self.chain.pools if p.address in liquid]
        self.routes = find_cycles(pools, self.flash_tokens, self.cfg.max_hops)
        # (pool address, token in) per hop, precomputed for the fast pre-filter.
        self._route_keys = [tuple((p.address, t_in) for p, t_in, _ in r.hops()) for r in self.routes]
        if hasattr(self.chain, "tracked"):
            # From now on only pools on some route are re-read every block.
            self.chain.tracked = {p.address for r in self.routes for p in r.pools}
        self._routes_built_at = self._clock()
        log.info("%d liquid pools (of %d), %d candidate routes", len(pools),
                 len(self.chain.pools), len(self.routes))

    def _log_rates(self, liquid: Set[str]) -> Dict[Tuple[str, str], float]:
        """log(marginal rate after fee) for each liquid pool in both directions.

        A route's marginal return is the product of its hop rates, so the sum of
        their logs says whether a route has any edge at all. Only routes where
        it's positive get the full sizing math. That keeps every block fast even
        with tens of thousands of routes."""
        rates = {}
        for p in self.chain.pools:
            if p.address not in liquid or not p.active:
                continue
            g0 = math.log((FEE_DENOMINATOR - p.fee_for(p.token0)) / FEE_DENOMINATOR)
            g1 = math.log((FEE_DENOMINATOR - p.fee_for(p.token1)) / FEE_DENOMINATOR)
            ratio = math.log(p.reserve1) - math.log(p.reserve0)
            rates[(p.address, p.token0)] = g0 + ratio
            rates[(p.address, p.token1)] = g1 - ratio
        return rates

    def find_opportunities(self, prices: Dict[str, float], gas_price_wei: int) -> List[Opportunity]:
        started = time.perf_counter()
        dec = self.chain.decimals
        gas_usd = self.cfg.gas_units_estimate * gas_price_wei / 1e18 * prices.get(self.native, 0.0)
        rates = self._log_rates(self._liquid_pools(prices))
        stats = self.stats
        found = []
        best_log, best_route = None, None
        best_net, best_net_route = None, None
        for route, keys in zip(self.routes, self._route_keys):
            try:
                edge_log = sum(rates[k] for k in keys)
            except KeyError:
                continue  # a pool on this route is illiquid or inactive right now
            if best_log is None or edge_log > best_log:
                best_log, best_route = edge_log, route
            if edge_log <= 0:
                continue
            start = route.start
            max_in = min(self.chain.vault_balances.get(start, 0),
                         from_usd(self.cfg.risk.max_loan_usd, start, prices, dec))
            amount_in = optimal_input_from(route.mobius(), max_in)
            if not amount_in:
                continue
            amount_out = route.amount_out(amount_in)
            profit_usd = to_usd(amount_out - amount_in, start, prices, dec)
            net = profit_usd - gas_usd
            if best_net is None or net > best_net:
                best_net, best_net_route = net, route
            if net >= self.cfg.risk.min_profit_usd:
                found.append(Opportunity(route, amount_in, amount_out, profit_usd, gas_usd))

        # Diagnostics for the summary; routes are described only once per block.
        if best_log is not None:
            edge = (math.exp(best_log) - 1) * 100
            if stats.best_edge_pct is None or edge > stats.best_edge_pct:
                stats.best_edge_pct, stats.best_edge_route = edge, best_route.describe(self.symbols)
        if best_net is not None and (stats.best_net_usd is None or best_net > stats.best_net_usd):
            stats.best_net_usd, stats.best_net_route = best_net, best_net_route.describe(self.symbols)
        stats.eval_ms_max = max(stats.eval_ms_max, (time.perf_counter() - started) * 1000)

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

    def _quote_check(self, opp: Opportunity, prices: Dict[str, float]) -> str:
        """Scan mode: re-price a candidate exactly with the dexes' Quoter contracts
        (free eth_calls). The fast model assumes V3 liquidity at the current price
        extends forever; thin pools can make that wildly optimistic."""
        quote = getattr(self.chain, "quote_route", None)
        if quote is None:
            return "scan only (estimate)"
        dec = self.chain.decimals
        best_net, best_amount, verified = None, 0, True
        amount = opp.amount_in
        for _ in range(3):  # full size, then 1/4 and 1/16 in case depth runs out
            out, ok = quote(opp.route, amount)
            verified = verified and ok
            net = to_usd(out - amount, opp.route.start, prices, dec) - opp.gas_usd
            if best_net is None or net > best_net:
                best_net, best_amount = net, amount
            amount //= 4
            if amount <= 0:
                break
        s = self.stats
        if best_net >= self.cfg.risk.min_profit_usd:
            s.quoter_verified += 1
            if s.best_verified_usd is None or best_net > s.best_verified_usd:
                s.best_verified_usd = best_net
                s.best_verified_route = opp.route.describe(self.symbols)
            tag = "quoter-verified" if verified else "partly verified (dex without quoter)"
            return f"{tag}: net ${best_net:.2f} at {self._fmt_amount(opp, best_amount)}"
        s.quoter_rejected += 1
        return f"rejected by quoter: real net ${best_net:.2f} (estimate was ${opp.net_usd:.2f})"

    # ----- loop -------------------------------------------------------------

    def step(self) -> bool:
        """Process the latest block. Returns False if the block was already seen."""
        block = self.chain.refresh()
        if block == self.last_block:
            return False
        if self.last_block is not None and block > self.last_block + 1:
            self.stats.skipped_blocks += block - self.last_block - 1
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
                if key not in self._prev_logged:  # check/log an opportunity once while it persists
                    self._log_opportunity(opp, self._quote_check(opp, prices))
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
        total = s.blocks + s.skipped_blocks
        seen = f" (saw {100 * s.blocks / total:.0f}% of chain blocks)" if total else ""
        lines = [f"mode={self.cfg.mode} blocks={s.blocks}{seen} routes={len(self.routes)} "
                 f"candidates={s.candidates} slowest_block_eval={s.eval_ms_max:.0f}ms"]
        if s.best_edge_pct is not None:
            lines.append(f"  closest this period: best edge after pool fees {s.best_edge_pct:+.4f}% "
                         f"({s.best_edge_route})")
        if s.best_net_usd is not None:
            lines.append(f"  best trade after gas (estimate) ${s.best_net_usd:+.2f} ({s.best_net_route}); "
                         f"needs >= ${self.cfg.risk.min_profit_usd:.2f}")
        else:
            lines.append("  no route had a positive edge after pool fees this period")
        if self.cfg.mode == "scan" and s.quoter_verified + s.quoter_rejected:
            best = (f"best ${s.best_verified_usd:.2f} ({s.best_verified_route})"
                    if s.best_verified_usd is not None else "none real this period")
            lines.append(f"  exact quotes: verified={s.quoter_verified} "
                         f"rejected={s.quoter_rejected}; {best}")
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
        errors_in_a_row = 0
        while max_blocks is None or self.stats.blocks < max_blocks:
            try:
                if not self.step():
                    time.sleep(self.cfg.poll_interval_s)
                errors_in_a_row = 0
            except Exception as exc:
                # Usually a temporary RPC problem (rate limit, network blip). Back off
                # exponentially instead of crashing or hammering the endpoint.
                errors_in_a_row += 1
                wait = min(60.0, 2.0 ** errors_in_a_row)
                status = http_status(exc)
                hint = {403: " (RPC refused access; is this network enabled for your key?)",
                        429: " (rate limited)"}.get(status, "")
                log.warning("block failed%s: %s; retrying in %.0fs", hint, describe(exc), wait)
                if status is None and not isinstance(exc, (ConnectionError, TimeoutError, OSError)):
                    log.debug("details", exc_info=True)
                time.sleep(wait)
            if self.risk.halted_reason:
                log.error("HALTED: %s", self.risk.halted_reason)
                break
            if self._clock() - self._last_summary >= self.cfg.summary_interval_s:
                log.info("summary\n%s", self.summary())
                self.stats.reset_window()
                self._last_summary = self._clock()
        log.info("final summary\n%s", self.summary())
