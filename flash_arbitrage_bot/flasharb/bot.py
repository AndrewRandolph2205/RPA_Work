"""Main loop: new block -> price all pools -> find profitable cycles -> check -> send.

Paper mode runs the live decision path but places paper orders (see paper.py).

Blocks come from the Arbitrum sequencer feed when one is configured (every
block, the moment it's sequenced, read pinned to that block), otherwise from
polling the RPC.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .amm import FEE_DENOMINATOR
from .closers import GapRecord
from .config import Config
from .errors import describe, http_status
from .feed import check_offset
from .journal import Journal
from .paper import PaperFill, PaperTrader
from .risk import RiskManager
from .routes import (Route, cycle_key, find_cycles, flash_fee, from_usd, optimal_input_from, pool_sides_usd,
                     to_usd, usd_prices)
from .simulator import diagnose

log = logging.getLogger(__name__)

# A long-tail token is dropped from all routes once an exact check shows it
# losing at least this much in transfer (a transfer tax / fee-on-transfer token).
TAX_EXCLUDE_MIN = 0.005


@dataclass
class Opportunity:
    route: Route
    amount_in: int
    amount_out: int
    profit_usd: float  # after pool fees, before the flash-loan fee and gas
    gas_usd: float
    fee_usd: float = 0.0  # flash-loan fee

    @property
    def net_usd(self) -> float:
        return self.profit_usd - self.fee_usd - self.gas_usd


def _percentile(values: List[float], q: float) -> Optional[float]:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))] if ordered else None


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
    quoter_verified: int = 0  # scan mode: exact checks that confirmed a gap
    quoter_verified_net_usd: float = 0.0  # scan mode: sum of verified gaps, each counted once
    verified_long_tail: int = 0  # verified by quoters (blind to transfer taxes) and touching a long-tail token
    gap_lifetimes: List[int] = field(default_factory=list)  # blocks each verified gap stayed open
    quoter_rejected: int = 0
    reject_causes: Counter = field(default_factory=Counter)  # "cause @ dex" -> count, since start
    # "Closest miss" diagnostics, reset after every summary.
    best_edge_pct: Optional[float] = None
    best_edge_route: str = ""
    best_net_usd: Optional[float] = None
    best_net_route: str = ""
    best_net_detail: str = ""
    best_net_edge_pct: Optional[float] = None
    eval_ms_max: float = 0.0
    check_ms_max: float = 0.0
    best_verified_usd: Optional[float] = None
    best_verified_route: str = ""
    feed_blocks: int = 0  # blocks this period that came from the sequencer feed
    rpc_lag_ms: List[float] = field(default_factory=list)  # feed announced block -> its state read
    # Paper mode, since start.
    paper_orders: int = 0
    paper_filled: int = 0
    paper_reverted: int = 0
    paper_lost: int = 0                # another tx took it inside the landing block
    paper_failed: int = 0              # couldn't be settled (RPC trouble); left out of the totals
    paper_profit_usd: float = 0.0      # filled trades, after the flash-loan fee
    paper_gas_usd: float = 0.0         # every settled paper trade pays gas, filled or not
    paper_net_usd: float = 0.0
    paper_zero_delay_net_usd: float = 0.0  # the same trades with no delay at all
    paper_would_halt: int = 0          # times live mode's revert limit would have stopped the bot
    paper_causes: Counter = field(default_factory=Counter)  # why paper trades didn't fill
    paper_express: Counter = field(default_factory=Counter)  # express lane's state at each order
    paper_decision_ms: List[float] = field(default_factory=list)
    paper_delay_ms: List[float] = field(default_factory=list)
    paper_blocks_late: List[int] = field(default_factory=list)
    paper_behind: List[int] = field(default_factory=list)  # RPC head minus the block traded on, per order

    def reset_window(self) -> None:
        self.eval_ms_max = self.check_ms_max = 0.0
        self.best_verified_usd, self.best_verified_route = None, ""
        self.best_edge_pct, self.best_edge_route = None, ""
        self.best_net_usd, self.best_net_route, self.best_net_detail = None, "", ""
        self.best_net_edge_pct = None
        self.feed_blocks, self.rpc_lag_ms = 0, []


class FlashBot:
    def __init__(self, cfg: Config, chain, executor, risk: RiskManager, journal: Journal,
                 clock: Callable[[], float] = time.time, feed=None, tracer=None, paper=None):
        self.cfg = cfg
        self.chain = chain
        self.executor = executor  # None in scan mode
        self.risk = risk
        self.journal = journal
        self.feed = feed      # SequencerFeed, or None to poll the RPC
        self.tracer = tracer  # CloserTracer, or None
        if paper is None and cfg.mode == "paper":
            paper = PaperTrader(chain, cfg)
        self.paper: Optional[PaperTrader] = paper  # paper mode only
        self._clock = clock
        self.symbols = {addr: sym for sym, addr in cfg.tokens.items()}
        self.native = cfg.token(cfg.native_wrapped)
        self.stables = {cfg.token(s) for s in cfg.stable_tokens}
        self.flash_tokens = [cfg.token(s) for s in cfg.flash_tokens]
        self.core = set(cfg.tokens.values()) - set(getattr(chain, "discovered", set()))
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
        # Scan mode: verified gaps still open -> [first_block, last_block, route, net_usd, pools]
        self._open_gaps: Dict[Tuple, list] = {}
        # Live mode: sent, not yet mined -> (opportunity, amount, block it was sent on).
        # Paper mode: "paper-<id>" for orders not yet settled.
        self._inflight: Dict[str, Tuple[Opportunity, int, Optional[int]]] = {}
        # Paper mode: gaps a filled paper trade would have closed (counted once while
        # they persist, since the real chain still shows them), and each order's gap.
        self._paper_open: Set[Tuple] = set()
        self._paper_key: Dict[int, Tuple] = {}
        self._step_started = time.perf_counter()
        # Tokens an exact check caught taking a cut in transfer; never routed again.
        self._excluded_file = Path(cfg.log_dir) / "excluded_tokens.json"
        self.excluded: Dict[str, str] = self._load_excluded()
        # Feed block numbers are trusted only once they match the RPC's.
        self._feed_ok = False
        self._feed_checked_at = float("-inf")
        self._feed_given_up = False

    # ----- excluded (transfer-tax) tokens -----------------------------------------

    def _load_excluded(self) -> Dict[str, str]:
        try:
            data = json.loads(self._excluded_file.read_text())
        except (OSError, ValueError):
            return {}
        return {str(k).lower(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _exclude_token(self, token: str, reason: str) -> None:
        if token in self.excluded or token in self.core:
            return
        self.excluded[token] = reason
        try:
            self._excluded_file.write_text(json.dumps(self.excluded, indent=1))
        except OSError:
            pass
        keep = [i for i, r in enumerate(self.routes) if token not in r.path]
        dropped = len(self.routes) - len(keep)
        self.routes = [self.routes[i] for i in keep]
        self._route_keys = [self._route_keys[i] for i in keep]
        log.warning("excluding %s from all routes (%d dropped): %s", self.symbols.get(token, token), dropped,
                    reason)

    # ----- analysis ---------------------------------------------------------

    def _prices(self) -> Dict[str, float]:
        return usd_prices(self.chain.pools, self.chain.decimals, self.stables)

    def _liquid_pools(self, prices) -> Set[str]:
        """Pools deep enough in total AND on each side, without excluded tokens."""
        risk, dec, excluded = self.cfg.risk, self.chain.decimals, self.excluded
        liquid = set()
        for p in self.chain.pools:
            if p.token0 in excluded or p.token1 in excluded:
                continue
            side0, side1 = pool_sides_usd(p, prices, dec)
            if side0 + side1 >= risk.min_pool_liquidity_usd and min(side0, side1) >= risk.min_pool_reserve_usd:
                liquid.add(p.address)
        return liquid

    def build_routes(self) -> Optional[int]:
        """(Re)build the candidate routes. Returns the block the pools were re-read
        at, if they were (a rebuild re-reads every pool at the RPC's latest block)."""
        fresh = None
        if self.routes:  # rebuild: untracked pools are stale, re-read everything
            refresh_all = getattr(self.chain, "refresh_all", None)
            if refresh_all:
                fresh = refresh_all()
            refresh_balances = getattr(self.chain, "refresh_pool_balances", None)
            if refresh_balances:  # first build: load() just read them
                refresh_balances()
        prices = self._prices()
        liquid = self._liquid_pools(prices)
        pools = [p for p in self.chain.pools if p.address in liquid]
        self.routes = find_cycles(pools, self.flash_tokens, self.cfg.max_hops)
        # (pool address, token in) per hop, precomputed for the fast pre-filter.
        self._route_keys = [tuple((p.address, t_in) for p, t_in, _ in r.hops()) for r in self.routes]
        if not self.routes or self._routes_built_at == float("-inf"):
            capacity = ", ".join(
                f"{self.symbols.get(t, t)} ${to_usd(self.chain.vault_balances.get(t, 0), t, prices, self.chain.decimals):,.0f}"
                for t in self.flash_tokens if t in self.chain.decimals)
            log.info("flash loan capacity (Balancer vault): %s", capacity or "unknown")
        if hasattr(self.chain, "tracked"):
            # From now on only pools on some route are re-read every block.
            self.chain.tracked = {p.address for r in self.routes for p in r.pools}
        self._routes_built_at = self._clock()
        log.info("%d liquid pools (of %d), %d candidate routes", len(pools),
                 len(self.chain.pools), len(self.routes))
        return fresh

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
        fee_rate = getattr(self.chain, "flash_fee_rate", 0.0) or 0.0
        fee_log = math.log1p(fee_rate)  # a route must beat the flash-loan fee too
        gas_usd = self.cfg.gas_units_estimate * gas_price_wei / 1e18 * prices.get(self.native, 0.0)
        rates = self._log_rates(self._liquid_pools(prices))
        stats = self.stats
        found = []
        best_log, best_route = None, None
        best_net, best_net_route, best_net_log, best_detail = None, None, 0.0, ""
        for route, keys in zip(self.routes, self._route_keys):
            try:
                edge_log = sum(rates[k] for k in keys) - fee_log
            except KeyError:
                continue  # a pool on this route is illiquid or inactive right now
            if best_log is None or edge_log > best_log:
                best_log, best_route = edge_log, route
            if edge_log <= 0:
                continue
            start = route.start
            vault_cap = self.chain.vault_balances.get(start, 0)
            config_cap = from_usd(self.cfg.risk.max_loan_usd, start, prices, dec)
            coeffs = route.mobius()
            amount_in = optimal_input_from(coeffs, min(vault_cap, config_cap), fee_rate)
            if not amount_in:
                continue
            amount_out = route.amount_out(amount_in)
            profit_usd = to_usd(amount_out - amount_in, start, prices, dec)
            fee_usd = to_usd(flash_fee(amount_in, fee_rate), start, prices, dec)
            net = profit_usd - fee_usd - gas_usd
            # Ranked by dollars at the best size, not by percent: a thin or
            # one-sided pool can show a huge percentage edge worth nothing.
            if best_net is None or net > best_net:
                ideal = optimal_input_from(coeffs, 10 ** 40, fee_rate) or 0
                limit = ("" if ideal <= min(vault_cap, config_cap) else
                         "capped by Balancer's balance" if vault_cap < config_cap else "capped by max_loan_usd")
                best_net, best_net_route, best_net_log = net, route, edge_log
                best_detail = (f"size ${to_usd(amount_in, start, prices, dec):,.0f}"
                               f"{' (' + limit + ')' if limit else ''}, "
                               f"profit ${profit_usd:.3f} - flash fee ${fee_usd:.3f} ({fee_rate * 1e4:.1f} bps)"
                               f" - gas ${gas_usd:.3f}")
            if net >= self.cfg.risk.min_profit_usd:
                found.append(Opportunity(route, amount_in, amount_out, profit_usd, gas_usd, fee_usd))

        # Diagnostics for the summary; routes are described only once per block.
        if best_log is not None:
            edge = (math.exp(best_log) - 1) * 100
            if stats.best_edge_pct is None or edge > stats.best_edge_pct:
                stats.best_edge_pct, stats.best_edge_route = edge, best_route.describe(self.symbols)
        if best_net is not None and (stats.best_net_usd is None or best_net > stats.best_net_usd):
            stats.best_net_usd, stats.best_net_route = best_net, best_net_route.describe(self.symbols)
            stats.best_net_detail = best_detail
            stats.best_net_edge_pct = (math.exp(best_net_log) - 1) * 100
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
            flash_fee_usd=round(opp.fee_usd, 4), gas_usd=round(opp.gas_usd, 4), net_usd=round(opp.net_usd, 4),
            decision=decision)

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
        """Simulate mode: dry-run the trade. Live mode: send it. Returns True when a
        transaction was attempted (at most one per block, one in flight)."""
        dec = self.chain.decimals
        start = opp.route.start
        # On-chain floor: the trade must clear estimated gas plus the minimum profit.
        # (The contract must also repay the loan's fee, so that's covered on-chain.)
        min_profit_raw = from_usd(opp.gas_usd + self.cfg.risk.min_profit_usd, start, prices, dec)
        amount = opp.amount_in
        self.last_sim_failed = False
        if self.cfg.mode == "paper":
            return self._paper_order(opp, prices, min_profit_raw)
        if self.cfg.mode == "simulate" or self.cfg.presimulate_live:
            amount, sim = self._simulate(opp, min_profit_raw)
            self.last_sim_failed = amount is None
            if amount is None:
                self.stats.sim_failed += 1
                self._log_opportunity(opp, f"simulation failed: {sim.error}")
                return False
            self.stats.sim_passed += 1
            if self.cfg.mode == "simulate":
                gas_usd = sim.gas_used * self.chain.last_gas_price_wei / 1e18 * prices.get(self.native, 0.0)
                fee_usd = to_usd(flash_fee(amount, getattr(self.chain, "flash_fee_rate", 0.0) or 0.0),
                                 start, prices, dec)
                est_net = to_usd(opp.route.amount_out(amount) - amount, start, prices, dec) - fee_usd - gas_usd
                self.stats.simulated_net_usd += est_net
                self._log_opportunity(opp, f"simulation passed (would send, est net ${est_net:.2f})")
                log.info("SIMULATED %s in=%s est_net=$%.2f", opp.route.describe(self.symbols),
                         self._fmt_amount(opp, amount), est_net)
                return True

        ok, reason = self.risk.can_send()
        if not ok:
            self._log_opportunity(opp, reason)
            return False
        # Straight out: no dry run (unless presimulate_live). If the gap is gone by
        # the time it lands, the contract's profit check reverts it for ~gas only.
        try:
            tx_hash = self.executor.submit(opp.route, amount, min_profit_raw)
        except Exception as exc:
            self._log_opportunity(opp, f"send failed: {describe(exc)}")
            log.warning("send failed: %s", describe(exc))
            return False
        self.stats.sent += 1
        self._inflight[tx_hash] = (opp, amount, self.last_block)
        self._log_opportunity(opp, f"sent {tx_hash}")
        self._collect_results(prices)
        return True

    def _collect_results(self, prices: Dict[str, float]) -> None:
        """Account for live transactions whose receipts have arrived (never waits)."""
        if self.paper is not None:
            self._collect_paper()
            return
        poll = getattr(self.executor, "poll_results", None)
        if poll is None:
            return
        dec = self.chain.decimals
        for res in poll():
            opp, amount, sent_block = self._inflight.pop(res.tx_hash, (None, 0, None))
            if opp is None:
                continue
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
                block=sent_block, mined_block=res.mined_block, route=opp.route.describe(self.symbols),
                amount_in=self._fmt_amount(opp, amount), tx_hash=res.tx_hash, success=res.success,
                timeboosted="" if res.timeboosted is None else res.timeboosted,
                profit_usd=round(profit_usd, 4), gas_usd=round(gas_usd, 4),
                net_usd=round(net if res.success else -gas_usd, 4), error=res.error)

    # ----- paper mode -------------------------------------------------------

    def _decision_latency(self) -> Tuple[Optional[float], str]:
        """How long after its block appeared this decision was made: measured from
        the sequencer feed's announcement, else from when this step started
        (polling can't tell how old the block already was, so that's a floor)."""
        if self.feed is not None and self.last_block is not None:
            age = self.feed.age_ms(self.last_block)
            if age is not None:
                return age, "feed"
        return (time.perf_counter() - self._step_started) * 1000, "poll"

    def _timeboost_hold(self) -> Tuple[float, str]:
        """Timeboost's hold on a paper trade, and why. Arbitrum holds ordinary
        transactions back only while someone controls the express lane; with no
        controller it's first-come-first-served. With paper_timeboost = "auto" the
        hold applies when the sequencer feed saw an express-lane transaction within
        the last auction round, and also when it can't tell (the cautious choice)."""
        cfg = self.cfg
        if cfg.paper_timeboost == "on":
            return cfg.paper_timeboost_delay_ms, "forced on"
        if cfg.paper_timeboost == "off":
            return 0.0, "forced off"
        state = None
        if self.feed is not None and self.feed.healthy():
            read = getattr(self.feed, "express_lane_state", None)
            state = read() if read is not None else None
        if state is False:
            return 0.0, "idle"
        return cfg.paper_timeboost_delay_ms, "active" if state else "unknown"

    def _chain_head(self) -> Optional[int]:
        """The RPC's latest block. Unlike the feed's timing, it doesn't depend on
        this bot keeping up, so it shows when the bot is acting on an old block."""
        head = getattr(self.chain, "head", None)
        if head is None:
            return None
        try:
            return int(head())
        except Exception as exc:
            log.warning("paper: couldn't read the chain's latest block: %s", describe(exc))
            return None

    def _paper_presimulate(self, opp: Opportunity, min_profit_raw: int) -> Optional[int]:
        """presimulate_live in paper mode: the exact check at this block, at full,
        half and quarter size, as live mode's dry run would. Returns the size that
        passes, or None."""
        verify = getattr(self.chain, "verify_route", None)
        if verify is None:
            return opp.amount_in
        sizes = [a for a in (opp.amount_in, opp.amount_in // 2, opp.amount_in // 4) if a > 0]
        try:
            outcomes = verify(opp.route, sizes, self.last_block)
        except Exception as exc:
            log.warning("paper dry run failed: %s", describe(exc))
            return None
        return next((o.amount_in for o in outcomes if o.ok and o.profit_raw >= min_profit_raw), None)

    def _paper_order(self, opp: Opportunity, prices: Dict[str, float], min_profit_raw: int) -> bool:
        """Paper mode: everything live mode does before sending, then a paper order
        instead of a transaction. Returns True when an order was placed."""
        amount = opp.amount_in
        if self.cfg.presimulate_live:
            amount = self._paper_presimulate(opp, min_profit_raw)
            self.last_sim_failed = amount is None
            if amount is None:
                self.stats.sim_failed += 1
                self._log_opportunity(opp, "paper: dry run failed at this block (live mode wouldn't send)")
                return False
            self.stats.sim_passed += 1
        ok, reason = self.risk.can_send()
        if not ok:
            self._log_opportunity(opp, f"paper: {reason}")
            return False
        dec, start = self.chain.decimals, opp.route.start
        fee_rate = getattr(self.chain, "flash_fee_rate", 0.0) or 0.0
        decision_ms, source = self._decision_latency()
        hold_ms, express = self._timeboost_hold()
        head = self._chain_head()
        order = self.paper.place(
            opp.route, opp.route.describe(self.symbols), amount, min_profit_raw, self.last_block, decision_ms,
            source, prices, est_profit_usd=to_usd(opp.route.amount_out(amount) - amount, start, prices, dec),
            est_fee_usd=to_usd(flash_fee(amount, fee_rate), start, prices, dec), est_gas_usd=opp.gas_usd,
            timeboost_ms=hold_ms, express_lane=express, chain_head=head)
        key = cycle_key(opp.route)
        self._paper_open.add(key)
        self._paper_key[order.paper_id] = key
        self._inflight[f"paper-{order.paper_id}"] = (opp, amount, self.last_block)
        s = self.stats
        s.paper_orders += 1
        s.paper_express[express] += 1
        if decision_ms is not None:
            s.paper_decision_ms.append(decision_ms)
        s.paper_delay_ms.append(order.delay_ms)
        s.paper_blocks_late.append(order.blocks_late)
        behind = order.blocks_behind
        if behind is not None:
            s.paper_behind.append(behind)
        lag = f"; chain already {behind} block(s) past block {order.detect_block}" if behind else ""
        self._log_opportunity(opp, f"paper order #{order.paper_id}: would land in block {order.landing_block} "
                                   f"(+{order.blocks_late}, {order.delay_ms:.0f}ms after its block appeared; "
                                   f"express lane {express}{lag})")
        log.info("PAPER #%d %s in=%s est_net=$%.2f would land in block %d (+%d%s)", order.paper_id,
                 order.route_desc, self._fmt_amount(opp, amount), order.est_net_usd, order.landing_block,
                 order.blocks_late, lag)
        return True

    def _collect_paper(self) -> None:
        """Paper mode: start settling orders whose landing block has arrived, and
        account for the ones the settlement thread finished (never waits)."""
        if self.last_block is not None:
            self.paper.due(self.last_block)
        for fill in self.paper.poll():
            order, s = fill.order, self.stats
            self._inflight.pop(f"paper-{order.paper_id}", None)
            key = self._paper_key.pop(order.paper_id, None)
            if not fill.success and key is not None:
                self._paper_open.discard(key)  # nothing was taken: the gap is fair game again
            if fill.counted:
                s.paper_filled += int(fill.success)
                s.paper_reverted += int(fill.status == "reverted")
                s.paper_lost += int(fill.status == "lost_race")
                s.paper_profit_usd += fill.profit_usd
                s.paper_gas_usd += fill.gas_usd
                s.paper_net_usd += fill.net_usd
                s.paper_zero_delay_net_usd += fill.zero_delay_net_usd or 0.0
                if not fill.success:
                    s.paper_causes[fill.cause] += 1
                # Live mode's limits see paper results too. Its revert limit would stop
                # the bot; paper mode notes that and carries on collecting data.
                self.risk.record_send(fill.success, fill.gas_usd)
                if self.risk.halted_reason:
                    s.paper_would_halt += 1
                    log.warning("paper: live mode would have HALTED here (%s); paper trading continues",
                                self.risk.halted_reason)
                    self.risk.halted_reason = None
                    self.risk.consecutive_reverts = 0
            else:
                s.paper_failed += 1
            self.journal.paper_trade(**self._paper_row(fill))
            log.log(logging.INFO if fill.counted else logging.WARNING, "PAPER #%d %s net=$%.4f: %s",
                    order.paper_id, fill.status.upper(), fill.net_usd, fill.reason)

    def _paper_row(self, fill: PaperFill) -> dict:
        o, s = fill.order, self.stats
        dec, start = self.chain.decimals, o.route.start
        sym = self.symbols.get(start, "?")
        amount = lambda raw: f"{raw / 10 ** dec[start]:.6g} {sym}"  # noqa: E731
        usd = lambda raw: to_usd(raw, start, o.prices, dec)  # noqa: E731
        rnd = lambda v, n=4: "" if v is None else round(v, n)  # noqa: E731
        settled = s.paper_filled + s.paper_reverted + s.paper_lost
        return dict(
            paper_id=o.paper_id, status=fill.status, cause=fill.cause, route=o.route_desc,
            pools=" ".join(p.address for p in o.route.pools), amount_in=amount(o.amount_in),
            amount_in_usd=rnd(usd(o.amount_in), 2), decided_at=o.decided_at, detect_block=o.detect_block,
            chain_head_block="" if o.chain_head is None else o.chain_head,
            blocks_behind="" if o.blocks_behind is None else o.blocks_behind,
            decision_ms=rnd(o.decision_ms, 1), latency_source=o.latency_source,
            send_latency_ms=o.send_ms, timeboost_delay_ms=o.timeboost_ms, express_lane=o.express_lane,
            total_delay_ms=rnd(o.delay_ms, 1), landing_block=o.landing_block, blocks_late=o.blocks_late,
            est_profit_usd=rnd(o.est_profit_usd), est_flash_fee_usd=rnd(o.est_fee_usd),
            est_gas_usd=rnd(o.est_gas_usd), est_net_usd=rnd(o.est_net_usd), min_profit_usd=rnd(usd(o.min_profit_raw)),
            exact_profit_at_detect_usd=rnd(fill.detect_profit_usd),
            exact_profit_at_landing_usd=rnd(fill.landing_profit_usd),
            open_after_landing="" if fill.open_after_landing is None else fill.open_after_landing,
            amount_out=amount(fill.amount_out) if fill.amount_out else "", profit_usd=rnd(fill.profit_usd),
            flash_fee_usd=rnd(fill.flash_fee_usd), gas_units=o.gas_units, gas_price_gwei=rnd(o.gas_price_wei / 1e9, 6),
            gas_usd=rnd(fill.gas_usd), net_usd=rnd(fill.net_usd), zero_delay_net_usd=rnd(fill.zero_delay_net_usd),
            latency_cost_usd=rnd(fill.latency_cost_usd), check_method=fill.method, reason=fill.reason,
            cum_orders=s.paper_orders, cum_settled=settled, cum_filled=s.paper_filled,
            cum_net_usd=rnd(s.paper_net_usd), cum_gas_usd=rnd(s.paper_gas_usd),
            fill_rate=rnd(s.paper_filled / settled, 3) if settled else "")

    def _paper_lines(self, hours: float) -> List[str]:
        s, cfg = self.stats, self.cfg
        settled = s.paper_filled + s.paper_reverted + s.paper_lost
        rate = f"; fill rate {100 * s.paper_filled / settled:.0f}%" if settled else ""
        failed = f" check_failed={s.paper_failed}" if s.paper_failed else ""
        lines = [f"  paper orders={s.paper_orders} filled={s.paper_filled} reverted={s.paper_reverted} "
                 f"lost_race={s.paper_lost}{failed} pending={self.paper.pending}{rate}"]
        if settled:
            lines.append(f"    net ${s.paper_net_usd:+.2f} (${s.paper_net_usd / hours:+.2f}/hr) = profit "
                         f"${s.paper_profit_usd:.2f} - gas ${s.paper_gas_usd:.2f}; the same trades with no delay: "
                         f"${s.paper_zero_delay_net_usd:+.2f} (latency cost "
                         f"${s.paper_zero_delay_net_usd - s.paper_net_usd:.2f})")
        if s.paper_delay_ms:
            decision = _percentile(s.paper_decision_ms, 0.5)
            parts = f"decision {decision:.0f}ms + " if decision is not None else ""
            held = sum(n for state, n in s.paper_express.items() if state not in ("idle", "forced off"))
            states = ", ".join(f"{state} {n}" for state, n in s.paper_express.most_common())
            lines.append(f"    median delay {_percentile(s.paper_delay_ms, 0.5):.0f}ms ({parts}send "
                         f"{cfg.paper_send_latency_ms:.0f}ms, plus Timeboost's {cfg.paper_timeboost_delay_ms:.0f}ms "
                         f"on {held} of {s.paper_orders} orders; express lane {states}): landed "
                         f"{_percentile(s.paper_blocks_late, 0.5)} block(s) after the gap was spotted")
        if s.paper_behind and max(s.paper_behind) > 0:
            lines.append(f"    bot behind the chain when ordering: median {_percentile(s.paper_behind, 0.5)} "
                         f"block(s), worst {max(s.paper_behind)} (RPC's latest block vs the block traded on)")
        if s.paper_causes:
            causes = ", ".join(f"{cause} {n}" for cause, n in s.paper_causes.most_common(5))
            lines.append(f"    why paper trades didn't fill (since start): {causes}")
        if s.paper_would_halt:
            lines.append(f"    live mode would have halted {s.paper_would_halt}x "
                         f"({cfg.risk.max_consecutive_reverts} reverts in a row); paper mode kept going")
        return lines

    def _verify(self, opp: Opportunity, prices: Dict[str, float]) -> Tuple[str, bool, float]:
        """Scan mode: check a candidate exactly, at the same block it was estimated
        on, so any shortfall is the model's error rather than a moved market.
        Returns (decision, verified?, best real net USD)."""
        verify = getattr(self.chain, "verify_route", None)
        if verify is None:
            return "scan only (estimate)", False, opp.net_usd
        dec, route, block = self.chain.decimals, opp.route, self.last_block
        base = opp.amount_in
        sizes = [a for a in (base, base // 4, base // 16) if a > 0]  # full size, then smaller if depth runs out
        tiny = max(1, base // 1000)  # tells a depth problem (fine when tiny) from a fee/price one
        started = time.perf_counter()
        try:
            *outcomes, tiny_outcome = verify(route, sizes + [tiny], block)
        except Exception as exc:  # RPC trouble: don't lose the block over one check
            log.warning("exact check failed: %s", describe(exc))
            return f"check failed: {describe(exc)}", False, opp.net_usd
        s = self.stats
        s.check_ms_max = max(s.check_ms_max, (time.perf_counter() - started) * 1000)
        net = lambda o: to_usd(o.profit_raw, route.start, prices, dec) - opp.gas_usd  # noqa: E731
        best = max(outcomes, key=net)
        best_net = net(best)
        desc = route.describe(self.symbols)
        row = dict(est_block=block, check_block=best.block, method=best.method, route=desc,
                   amount_in=self._fmt_amount(opp, best.amount_in), est_net_usd=round(opp.net_usd, 4),
                   real_net_usd=round(best_net, 4))
        if best_net >= self.cfg.risk.min_profit_usd:
            s.quoter_verified += 1
            s.quoter_verified_net_usd += best_net
            if s.best_verified_usd is None or best_net > s.best_verified_usd:
                s.best_verified_usd, s.best_verified_route = best_net, desc
            tag = f"verified ({best.method}, block {best.block})"
            if best.method != "sim" and set(route.path) & getattr(self.chain, "discovered", set()):
                # Quoters can't see transfer taxes; only real swaps (RouteSimulator) can.
                s.verified_long_tail += 1
                tag += ", long-tail token: quoters can't see transfer taxes"
            self.journal.check(**row, result="verified", cause="", hop="", detail="")
            return f"{tag}: net ${best_net:.2f} at {self._fmt_amount(opp, best.amount_in)}", True, best_net
        s.quoter_rejected += 1
        d = diagnose(route, outcomes[0], tiny_outcome, self.symbols, self.core)
        s.reject_causes[f"{d.cause} @ {route.pools[d.hop].dex}" if d.hop is not None else d.cause] += 1
        if d.taxed_token and d.shortfall >= TAX_EXCLUDE_MIN and \
                d.taxed_token in getattr(self.chain, "discovered", set()):
            self._exclude_token(d.taxed_token, d.detail)
        self.journal.check(**row, result="model error", cause=d.cause,
                           hop="" if d.hop is None else d.hop + 1, detail=d.detail)
        return (f"rejected ({d.cause}): real net ${best_net:.2f} (estimate was ${opp.net_usd:.2f}); {d.detail}",
                False, best_net)

    # ----- loop -------------------------------------------------------------

    def step(self, target: Optional[int] = None) -> bool:
        """Process the latest block, or `target` (a block the feed announced).
        Returns False if the block was already seen."""
        self._step_started = time.perf_counter()
        block = self.chain.refresh() if target is None else self.chain.refresh(block=target)
        if block == self.last_block:
            return False
        if target is not None and self.feed is not None:
            self.stats.feed_blocks += 1
            age = self.feed.age_ms(block)
            if age is not None:
                self.stats.rpc_lag_ms.append(age)
        if self.last_block is not None and block > self.last_block + 1:
            self.stats.skipped_blocks += block - self.last_block - 1
        self.last_block = block
        self.stats.blocks += 1

        if self._clock() - self._routes_built_at >= self.cfg.route_rebuild_s:
            fresh = self.build_routes()
            if isinstance(fresh, int) and fresh > block:
                self.last_block = block = fresh  # pools now reflect this block; check at the same one

        gas_price = self.chain.gas_price_wei()
        if not self.risk.gas_price_ok(gas_price):
            log.debug("gas price %.3f gwei above cap; skipping block", gas_price / 1e9)
            return True

        prices = self._prices()
        self._collect_results(prices)
        found = self.find_opportunities(prices, gas_price)
        # Every trade still showing a gap, ranked or not (for gap lifetimes).
        open_keys = {cycle_key(o.route) for o in found}
        opps = found[: self.cfg.max_candidates_per_block]
        self.stats.candidates += len(opps)

        keys = set()
        for opp in opps:
            # A cycle is the same trade whichever token it starts from
            # (A->B->C->A == B->C->A->B); the opposite direction is another trade.
            key = cycle_key(opp.route)
            if key in keys:
                continue  # another rotation of a cycle already handled this block
            keys.add(key)
            if self.executor is None and self.paper is None:
                if key not in self._open_gaps and key not in self._prev_logged:
                    # Check and log an opportunity once while it persists.
                    decision, verified, real_net = self._verify(opp, prices)
                    self._log_opportunity(opp, decision)
                    if verified:
                        self._open_gaps[key] = [block, block, opp.route.describe(self.symbols), real_net,
                                                tuple(p.address for p in opp.route.pools)]
                continue
            if self.cfg.mode in ("live", "paper") and self._inflight:
                break  # one transaction in flight at a time: a second would chase the same gap
            if self._cooldown.get(key, -1) >= block or key in self._open_simulated or key in self._paper_open:
                continue
            attempted = self._act(opp, prices)
            if self.last_sim_failed:
                self._cooldown[key] = block + self.cfg.sim_cooldown_blocks
            elif attempted and self.cfg.mode == "simulate":
                self._open_simulated.add(key)
            if attempted:
                break  # the chain state changes after a trade; re-evaluate next block
        for key in open_keys & self._open_gaps.keys():
            self._open_gaps[key][1] = block  # a verified gap is still there
        self._prev_logged = (self._prev_logged | keys) & open_keys
        self._open_simulated &= keys
        self._paper_open &= open_keys
        self._close_gaps(open_keys, block)
        return True

    def _close_gaps(self, still_open: Set, block: int) -> None:
        """Record how long each verified gap lasted once it's gone, and hand it to
        the closer tracer. Gaps that last only a block or two are being taken by
        faster bots; ones that sit for many blocks are ones a slower bot could win."""
        for key in [k for k in self._open_gaps if k not in still_open]:
            first, last, route, net, pools = self._open_gaps.pop(key)
            lifetime = last - first + 1
            self.stats.gap_lifetimes.append(lifetime)
            self.journal.gap(route=route, first_block=first, last_block=last, closed_by_block=block,
                             blocks_open=lifetime, net_usd=round(net, 4), pools=" ".join(pools))
            log.info("gap closed after %d block(s) (~%.1fs; seen gone at block +%d): %s", lifetime,
                     lifetime * 0.25, block - first, route)
            if self.tracer is not None:
                self.tracer.submit(GapRecord(route, pools, first, last, block, net))

    def summary(self) -> str:
        s = self.stats
        hours = max((self._clock() - self._started) / 3600, 1e-9)
        total = s.blocks + s.skipped_blocks
        seen = f" (saw {100 * s.blocks / total:.0f}% of chain blocks)" if total else ""
        source = ""
        if self.feed is not None:
            source = " source=feed" if self._feed_ok and not self._feed_given_up else " source=rpc-poll"
        lines = [f"mode={self.cfg.mode} blocks={s.blocks}{seen}{source} routes={len(self.routes)} "
                 f"candidates={s.candidates} slowest_block_eval={s.eval_ms_max:.0f}ms"]
        if self.feed is not None:
            lines.append("  " + self._feed_line())
        if s.best_net_usd is not None:
            edge = f", marginal edge {s.best_net_edge_pct:+.3f}%" if s.best_net_edge_pct is not None else ""
            lines.append(f"  best trade after gas (estimate) ${s.best_net_usd:+.2f} ({s.best_net_route}){edge}; "
                         f"needs >= ${self.cfg.risk.min_profit_usd:.2f}")
            lines.append(f"    {s.best_net_detail}")
        else:
            if s.best_edge_pct is not None:
                lines.append(f"  closest miss: best edge after pool fees {s.best_edge_pct:+.4f}% "
                             f"({s.best_edge_route})")
            lines.append("  no route had a positive edge after pool fees this period")
        if self.cfg.mode == "scan" and s.quoter_verified + s.quoter_rejected:
            best = (f"best ${s.best_verified_usd:.2f} ({s.best_verified_route})"
                    if s.best_verified_usd is not None else "none real this period")
            lines.append(f"  exact checks (at the estimate's own block): verified={s.quoter_verified} "
                         f"rejected={s.quoter_rejected}; {best}; slowest check {s.check_ms_max:.0f}ms")
            if s.reject_causes:
                causes = ", ".join(f"{cause} {n}" for cause, n in s.reject_causes.most_common(6))
                lines.append(f"    why rejected estimates missed (since start): {causes}")
            if self.excluded:
                names = ", ".join(self.symbols.get(t, t[:10]) for t in self.excluded)
                lines.append(f"    excluded transfer-tax tokens: {names}")
            if s.verified_long_tail:
                lines.append(f"    {s.verified_long_tail} of the verified gaps involve long-tail tokens that only "
                             f"quoters checked; some may be transfer-tax tokens")
            if s.gap_lifetimes:
                life = sorted(s.gap_lifetimes)
                quick = sum(1 for n in life if n <= 2)
                lines.append(f"  verified gaps closed: {len(life)}; gone within 2 blocks: {quick}; "
                             f"lasted 4+ blocks (1s+): {sum(1 for n in life if n >= 4)}; "
                             f"median {life[len(life) // 2]} blocks")
            traced = self.tracer.summary() if self.tracer is not None else None
            if traced:
                lines.append(f"  {traced}")
            lines.append(f"  verified total since start ${s.quoter_verified_net_usd:.2f} "
                         f"(${s.quoter_verified_net_usd / hours:.2f}/hr if the bot had won every one; "
                         f"it wouldn't)")
        if self.cfg.mode != "scan" and (self.cfg.mode == "simulate" or self.cfg.presimulate_live):
            lines.append(f"  simulations passed={s.sim_passed} failed={s.sim_failed}")
        if self.cfg.mode == "simulate":
            lines.append(f"  would-have-made net=${s.simulated_net_usd:.2f} "
                         f"(${s.simulated_net_usd / hours:.2f}/hr, optimistic)")
        if self.cfg.mode == "paper" and self.paper is not None:
            lines += self._paper_lines(hours)
        if self.cfg.mode == "live":
            lines.append(f"  sent={s.sent} succeeded={s.succeeded} reverted={s.reverted} "
                         f"in_flight={len(self._inflight)} gas=${s.gas_spent_usd:.2f}")
            lines.append(f"  realized net=${s.realized_net_usd:.2f} "
                         f"(${s.realized_net_usd / hours:.2f}/hr)")
        return "\n".join(lines)

    def _feed_line(self) -> str:
        feed, s = self.feed, self.stats
        parts = [f"feed: {'connected' if feed.healthy() else 'DOWN (polling the RPC)'}"]
        if s.rpc_lag_ms:
            parts.append(f"pools read {_percentile(s.rpc_lag_ms, 0.5):.0f}ms after the feed announced each block "
                         f"(median; p90 {_percentile(s.rpc_lag_ms, 0.9):.0f}ms)")
        fs = feed.stats
        if fs.blocks:
            parts.append(f"express-lane txs in {100 * fs.express_blocks / fs.blocks:.0f}% of blocks")
        behind = getattr(self.chain, "rpc_behind_timeouts", 0)
        if behind:
            parts.append(f"RPC fell >0.75s behind {behind}x since start")
        fs.reset()
        return "; ".join(parts)

    def _feed_usable(self) -> bool:
        """True when blocks should come from the feed. Its block numbers are checked
        against the RPC's (by block hash) before the bot relies on them."""
        feed = self.feed
        if feed is None or self._feed_given_up or not feed.healthy():
            return False
        if self._feed_ok:
            return True
        now = self._clock()
        if now - self._feed_checked_at < 2.0:
            return False
        self._feed_checked_at = now
        try:
            verdict = check_offset(feed, self.chain)
        except Exception as exc:
            log.debug("feed check: %s", describe(exc))
            verdict = None
        if verdict is not None:
            self._feed_ok = True
            log.info("sequencer feed block numbers match the RPC's (%s); following the feed", verdict)
        elif now - self._started > 120:
            self._feed_given_up = True
            log.warning("couldn't match the sequencer feed's block numbers to the RPC's; polling the RPC")
        return self._feed_ok

    def run(self, max_blocks: Optional[int] = None) -> None:
        errors_in_a_row = 0
        while max_blocks is None or self.stats.blocks < max_blocks:
            try:
                if self._feed_usable():
                    target = self.feed.wait_for_block_after(self.last_block, timeout=0.5)
                    if target is not None:
                        self.step(target)
                elif not self.step():
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
        if self.tracer is not None:
            self.tracer.wait()  # include closers still being looked up
        if self.paper is not None:  # settle orders whose landing block already arrived
            if self.last_block is not None:
                self.paper.due(self.last_block)
            self.paper.wait()
            self._collect_paper()
        log.info("final summary\n%s", self.summary())
