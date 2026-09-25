"""Paper trading: trade exactly as live mode would, without sending anything.

Wherever live mode would send a transaction, paper mode places a paper order
and works out, from the real chain, what would have happened to it:

1. When it would have landed. The decision's delay after the sequencer feed
   announced the block, plus the trip to the sequencer (paper_send_latency_ms),
   counted in blocks (paper_block_time_ms). While someone controls Timeboost's
   express lane, every other transaction is also held back
   (paper_timeboost_delay_ms, 200ms); with no controller Arbitrum is
   first-come-first-served. The bot decides which applies (see
   FlashBot._timeboost_hold). A trade decided 120ms after block N appeared is
   ready 170ms later with the lane idle (lands in N+1), 370ms later with it in
   use (too late for N+1, so N+2). That delay starts when this bot heard about
   the block, so it can't show the bot itself falling behind (a feed backlog in
   a busy moment). The RPC's latest block at the decision covers that: a trade
   can't land in a block that already exists, so it lands after that head.
2. Whether it would have paid. Once the chain has that block, the same exact
   check scan mode uses (RouteSimulator, one eth_call) runs the trade on the
   state it would have met: the end of the block before it landed.
   FlashArbitrage reverts unless profit >= minProfit, so anything less is a
   revert that still costs gas.
3. Whether another transaction took it first in its own block. The check runs
   again on the state after the landing block. If the gap is gone there,
   someone else took it within that block. The order inside a block can't be
   known, so it counts as lost (paper_same_block_wins = true counts it as won).

Every order is also checked at the block it was spotted: what a bot with no
delay at all would have made. The difference is what latency cost.

Settlement runs on a background thread, like live mode's receipt watcher, so
paper mode decides exactly as fast as live mode would.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import queue
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from .closers import first_taker
from .errors import describe
from .routes import Route, to_usd

log = logging.getLogger(__name__)

FILLED, REVERTED, LOST, FAILED = "filled", "reverted", "lost_race", "check_failed"


def landing_block(detect_block: int, delay_ms: float, block_ms: float) -> int:
    """First block a transaction can land in when it reaches the sequencer
    `delay_ms` after block `detect_block` appeared (never the same block)."""
    return detect_block + max(1, math.ceil(delay_ms / block_ms - 1e-9))


@dataclass
class PaperOrder:
    """A trade live mode would have sent, as it was decided."""
    paper_id: int
    route: Route
    route_desc: str
    amount_in: int
    min_profit_raw: int           # the contract's floor: it reverts unless profit >= this
    detect_block: int             # block the opportunity was spotted on
    decided_at: str               # UTC time of the decision
    decision_ms: Optional[float]  # block appeared -> decision (None: unknown)
    latency_source: str           # "feed" (measured from the announcement) or "poll" (an underestimate)
    send_ms: float                # trip to the sequencer
    timeboost_ms: float           # Timeboost's hold, 0 while nobody uses the express lane
    express_lane: str             # active | idle | unknown | forced on | forced off
    delay_ms: float               # decision + send + Timeboost's hold
    landing_block: int
    prices: Dict[str, float]      # USD prices at the decision, used to value the result
    est_profit_usd: float         # the fast estimate at the decision, for this size
    est_fee_usd: float
    est_gas_usd: float
    gas_units: int
    gas_price_wei: int
    chain_head: Optional[int] = None  # the RPC's latest block at the decision (None: couldn't read it)

    @property
    def blocks_late(self) -> int:
        return self.landing_block - self.detect_block

    @property
    def blocks_behind(self) -> Optional[int]:
        """How far the chain had already moved past the block this was decided on."""
        return None if self.chain_head is None else max(0, self.chain_head - self.detect_block)

    @property
    def est_net_usd(self) -> float:
        return self.est_profit_usd - self.est_fee_usd - self.est_gas_usd


@dataclass
class PaperFill:
    """What would have happened to a paper order."""
    order: PaperOrder
    status: str                                  # filled | reverted | lost_race | check_failed
    cause: str                                   # short reason, for counting
    reason: str                                  # the full explanation
    method: str = ""                             # exact check: "sim" (RouteSimulator) or "quoter"
    amount_out: int = 0                          # what came back on the landing state
    profit_raw: int = 0                          # to the wallet, in the borrowed token (filled only)
    profit_usd: float = 0.0                      # after the flash-loan fee, before gas (filled only)
    flash_fee_usd: float = 0.0
    gas_usd: float = 0.0                         # paid whether it filled or reverted
    detect_profit_usd: Optional[float] = None    # exact, at the block it was spotted
    landing_profit_usd: Optional[float] = None   # exact, on the state it landed on
    open_after_landing: Optional[bool] = None    # did the gap still pay after the landing block?
    zero_delay_net_usd: Optional[float] = None   # net for a bot with no delay at all
    # The transaction that took the gap instead (lost races and gaps closed before
    # landing), from first_taker(). Its position counts user transactions only:
    # every Arbitrum block starts with ArbOS's own transaction at index 0.
    winner: Optional[Dict] = None
    winner_block_txs: Optional[int] = None       # user transactions in the winner's block

    @property
    def winner_position(self) -> Optional[int]:
        """1 = the first user transaction in its block."""
        return None if self.winner is None else max(1, self.winner["index"])

    @property
    def success(self) -> bool:
        return self.status == FILLED

    @property
    def counted(self) -> bool:
        """Settled either way; a check that failed (RPC trouble) counts for nothing."""
        return self.status != FAILED

    @property
    def net_usd(self) -> float:
        if not self.counted:
            return 0.0
        return self.profit_usd - self.gas_usd if self.success else -self.gas_usd

    @property
    def latency_cost_usd(self) -> Optional[float]:
        if self.zero_delay_net_usd is None or not self.counted:
            return None
        return self.zero_delay_net_usd - self.net_usd


class PaperTrader:
    """Places paper orders and settles each one once the chain reaches its landing block."""

    def __init__(self, chain, cfg, background: bool = True):
        self.chain = chain
        self.cfg = cfg
        self.native = cfg.token(cfg.native_wrapped)
        self._background = background
        self._worker = self._new_worker()
        self._next_id = 0
        self._waiting: List[PaperOrder] = []  # landing block not reached yet
        self._open: Set[int] = set()          # placed, result not collected yet
        self._results: "queue.Queue[PaperFill]" = queue.Queue()

    def _new_worker(self) -> Optional[ThreadPoolExecutor]:
        return ThreadPoolExecutor(max_workers=1, thread_name_prefix="paper") if self._background else None

    # ----- orders ---------------------------------------------------------------

    def place(self, route: Route, route_desc: str, amount_in: int, min_profit_raw: int, detect_block: int,
              decision_ms: Optional[float], latency_source: str, prices: Dict[str, float],
              est_profit_usd: float, est_fee_usd: float, est_gas_usd: float,
              timeboost_ms: Optional[float] = None, express_lane: str = "unknown",
              chain_head: Optional[int] = None) -> PaperOrder:
        self._next_id += 1
        send = self.cfg.paper_send_latency_ms
        hold = self.cfg.paper_timeboost_delay_ms if timeboost_ms is None else timeboost_ms
        delay = (decision_ms or 0.0) + send + hold
        landing = landing_block(detect_block, delay, self.cfg.paper_block_time_ms)
        if chain_head is not None:
            landing = max(landing, chain_head + 1)  # blocks up to the chain's head already exist
        order = PaperOrder(
            paper_id=self._next_id, route=route, route_desc=route_desc, amount_in=int(amount_in),
            min_profit_raw=int(min_profit_raw), detect_block=detect_block,
            decided_at=dt.datetime.now(dt.timezone.utc).isoformat(), decision_ms=decision_ms,
            latency_source=latency_source, send_ms=send, timeboost_ms=hold, express_lane=express_lane,
            delay_ms=delay,
            landing_block=landing,
            prices=dict(prices), est_profit_usd=est_profit_usd, est_fee_usd=est_fee_usd, est_gas_usd=est_gas_usd,
            gas_units=int(self.cfg.paper_gas_units or self.cfg.gas_units_estimate),
            gas_price_wei=int(getattr(self.chain, "last_gas_price_wei", 0) or 0), chain_head=chain_head)
        self._waiting.append(order)
        self._open.add(order.paper_id)
        return order

    @property
    def pending(self) -> int:
        """Orders placed whose result hasn't been collected yet."""
        return len(self._open)

    def due(self, block: int) -> None:
        """Start settling every order whose landing block the chain has reached."""
        ready = [o for o in self._waiting if o.landing_block <= block]
        if not ready:
            return
        self._waiting = [o for o in self._waiting if o.landing_block > block]
        for order in ready:
            if self._worker is None:
                self._settle_into_queue(order)
            else:
                self._worker.submit(self._settle_into_queue, order)

    def poll(self) -> List[PaperFill]:
        """Results that are ready (never waits)."""
        out = []
        while True:
            try:
                fill = self._results.get_nowait()
            except queue.Empty:
                return out
            self._open.discard(fill.order.paper_id)
            out.append(fill)

    def wait(self) -> None:
        """Finish settlements already started (shutdown, tests)."""
        if self._worker is not None:
            self._worker.shutdown(wait=True)
            self._worker = self._new_worker()

    # ----- settlement -------------------------------------------------------------

    def _settle_into_queue(self, order: PaperOrder) -> None:
        try:
            fill = self.settle(order)
        except Exception as exc:  # never lose an order: record why it couldn't be settled
            fill = PaperFill(order, FAILED, "check failed", f"exact check failed: {describe(exc)}")
        self._results.put(fill)

    def _usd(self, order: PaperOrder, amount_raw: int) -> float:
        return to_usd(amount_raw, order.route.start, order.prices, self.chain.decimals)

    def _find_winner(self, fill: PaperFill, lo: int, hi: int) -> None:
        """Who took the gap, and how early in its block (best effort)."""
        if not hasattr(self.chain, "logs_for"):
            return
        try:
            winner = first_taker(self.chain, [p.address for p in fill.order.route.pools], lo, hi)
            if winner is None:
                return
            count = getattr(self.chain, "block_tx_count", None)
            total = count(winner["block"]) if count is not None else None
        except Exception as exc:
            log.debug("paper: couldn't look up who took the gap: %s", describe(exc))
            return
        fill.winner = winner
        fill.winner_block_txs = None if total is None else max(1, total - 1)
        of = f" of {fill.winner_block_txs}" if fill.winner_block_txs else ""
        fill.reason += (f"; taken by {winner['tx_hash']} ({winner['kind']}, to {winner['to'] or '?'}"
                        f"{', EXPRESS LANE' if winner['timeboosted'] else ''}), transaction "
                        f"{fill.winner_position}{of} in block {winner['block']}")

    def settle(self, order: PaperOrder) -> PaperFill:
        """Run the order on the chain states around its landing block."""
        spot, land = order.detect_block, order.landing_block
        outcomes = {block: self.chain.verify_route(order.route, [order.amount_in], block)[0]
                    for block in sorted({spot, land - 1, land})}
        spotted, before, after = outcomes[spot], outcomes[land - 1], outcomes[land]
        floor = order.min_profit_raw

        def pays(outcome) -> bool:  # FlashArbitrage's own check: balance >= loan + fee + minProfit
            return outcome.ok and outcome.profit_raw >= floor

        def profit(outcome) -> Optional[float]:
            return self._usd(order, outcome.profit_raw) if outcome.ok else None

        gas_usd = order.gas_units * order.gas_price_wei / 1e18 * order.prices.get(self.native, 0.0)
        fill = PaperFill(order, REVERTED, "", "", method=before.method, amount_out=before.out, gas_usd=gas_usd,
                         detect_profit_usd=profit(spotted), landing_profit_usd=profit(before),
                         open_after_landing=pays(after),
                         zero_delay_net_usd=profit(spotted) - gas_usd if pays(spotted) else -gas_usd)
        floor_usd = self._usd(order, floor)
        if pays(before) and (pays(after) or self.cfg.paper_same_block_wins):
            fill.status, fill.cause = FILLED, "filled"
            fill.profit_raw = before.profit_raw
            fill.profit_usd = self._usd(order, before.profit_raw)
            fill.flash_fee_usd = self._usd(order, before.flash_fee)
            fill.reason = f"paid ${fill.profit_usd:.4f} landing in block {land} (floor ${floor_usd:.4f})"
            if not pays(after):
                fill.reason += ("; another transaction in the same block also took it, counted as won "
                                "(paper_same_block_wins)")
        elif pays(before):
            fill.status, fill.cause = LOST, "lost in landing block"
            fill.reason = (f"still paid ${fill.landing_profit_usd:.4f} going into block {land} but was gone by "
                           f"its end: another transaction in block {land} took it, and the order within a "
                           "block can't be known, so it counts as lost")
        elif not before.ok:
            hop = before.failed_hop
            fill.cause = "swap reverted" if hop is not None and hop >= 0 else "call failed"
            where = f"hop {hop + 1}" if hop is not None and hop >= 0 else "the flash loan or call"
            fill.reason = f"{where} reverted landing in block {land}: {before.reason}"
        elif pays(spotted):
            fill.cause = "closed before landing"
            fill.reason = (f"gap closed before it landed: paid ${fill.detect_profit_usd:.4f} at block {spot}, "
                           f"${fill.landing_profit_usd:.4f} by block {land} (floor ${floor_usd:.4f})")
        else:
            fill.cause = "estimate was off"
            spotted_text = (f"${fill.detect_profit_usd:.4f}" if fill.detect_profit_usd is not None
                            else f"a revert ({spotted.reason})")
            fill.reason = (f"never paid: the exact check at block {spot} gave {spotted_text} against a floor "
                           f"of ${floor_usd:.4f}; the fast estimate was off")
        if fill.status == LOST:
            self._find_winner(fill, land, land)
        elif fill.cause == "closed before landing" and land - 1 > spot:
            self._find_winner(fill, spot + 1, land - 1)
        if order.blocks_behind:
            fill.reason += (f"; the chain was already {order.blocks_behind} block(s) past block {spot} "
                            "when this was decided")
        if fill.method != "sim":
            fill.reason += " [quoter fallback: V2-style hops valued at the latest block, not the landing block]"
        return fill
