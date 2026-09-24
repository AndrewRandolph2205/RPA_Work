"""Who closed each verified gap?

When a verified gap disappears, the transaction that closed it sits between the
last block the bot saw the gap open and the first block it saw it gone. The
tracer finds it with eth_getLogs on the route's pools and reads its receipt:
Arbitrum receipts say whether it came through Timeboost's express lane
(`timeboosted`). A closer that swapped through 2+ of the route's pools is
almost certainly another arbitrage bot, and its `to` is that bot's contract.

    timeboosted closer            -> you're competing with the express lane
    regular closer, next block    -> a latency race (sequencer feed, co-location)
    no transaction found          -> the estimate moved, not the pools

Runs on a background thread so the main loop never waits for it.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .errors import describe

log = logging.getLogger(__name__)


@dataclass
class GapRecord:
    route: str
    pools: Tuple[str, ...]
    first_block: int
    last_open: int    # last block the bot saw the gap open
    closed_by: int    # first block the bot saw it gone
    net_usd: float


def _flag(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("0x1", "true", "1")
    return None


class CloserTracer:
    def __init__(self, chain, journal, max_blocks: int = 50):
        self.chain = chain
        self.journal = journal
        self.max_blocks = max_blocks  # widest block window searched per gap
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="closers")
        self._lock = threading.Lock()
        self.results: List[Dict] = []

    def submit(self, gap: GapRecord) -> None:
        self._worker.submit(self._run, gap)

    def wait(self) -> None:
        """Finish queued lookups (used at shutdown and by `trace-gaps`)."""
        self._worker.shutdown(wait=True)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="closers")

    def _run(self, gap: GapRecord) -> None:
        try:
            row = self.trace(gap)
        except Exception as exc:
            row = self._base(gap)
            row["kind"] = f"lookup failed: {describe(exc)}"
        try:
            self.journal.closer(**row)
        except Exception as exc:  # pragma: no cover - disk full etc.
            log.warning("couldn't write closers.csv: %s", exc)
        with self._lock:
            self.results.append(row)
        if row.get("tx_hash"):
            log.info("gap closer: %s block +%s idx %s %s %s", row["route"], row["blocks_after_last_open"],
                     row["tx_index"], "EXPRESS LANE" if row["timeboosted"] is True else "regular",
                     row["kind"])

    @staticmethod
    def _base(gap: GapRecord) -> Dict:
        return {"route": gap.route, "first_block": gap.first_block, "last_open_block": gap.last_open,
                "closed_by_block": gap.closed_by, "closer_block": "", "blocks_after_last_open": "",
                "tx_index": "", "tx_hash": "", "from": "", "to": "", "timeboosted": "",
                "pools_touched": "", "txs_in_window": 0, "gas_used": "", "kind": ""}

    def trace(self, gap: GapRecord) -> Dict:
        lo = gap.last_open + 1
        hi = min(max(lo, gap.closed_by), lo + self.max_blocks - 1)
        pools = {p.lower() for p in gap.pools}
        txs: "OrderedDict[str, Dict]" = OrderedDict()
        entries = self.chain.logs_for(sorted(pools), lo, hi)
        order = lambda e: (int(e["blockNumber"], 16), int(e["transactionIndex"], 16),  # noqa: E731
                           int(e.get("logIndex", "0x0"), 16))
        for entry in sorted(entries, key=order):
            info = txs.setdefault(entry["transactionHash"], {"block": int(entry["blockNumber"], 16),
                                                              "index": int(entry["transactionIndex"], 16),
                                                              "pools": set()})
            info["pools"].add(entry["address"].lower())
        row = self._base(gap)
        row["txs_in_window"] = len(txs)
        if not txs:
            row["kind"] = "no transaction touched the route's pools (the estimate moved, not the pools)"
            return row
        # The first transaction through 2+ of the route's pools is the arbitrage
        # that closed it; failing that, the first trade through any of them.
        closer = next((h for h, i in txs.items() if len(i["pools"] & pools) >= 2), next(iter(txs)))
        info = txs[closer]
        receipt = self.chain.receipt_raw(closer) or {}
        touched = len(info["pools"] & pools)
        row.update({
            "closer_block": info["block"], "blocks_after_last_open": info["block"] - gap.last_open,
            "tx_index": info["index"], "tx_hash": closer, "from": receipt.get("from") or "",
            "to": receipt.get("to") or "", "timeboosted": _flag(receipt.get("timeboosted")),
            "pools_touched": f"{touched}/{len(pools)}",
            "gas_used": int(receipt["gasUsed"], 16) if receipt.get("gasUsed") else "",
            "kind": "arbitrage (2+ route pools)" if touched >= 2 else "single-pool trade",
        })
        if row["timeboosted"] is None:
            row["timeboosted"] = ""  # receipt had no timeboosted field
        return row

    def summary(self) -> Optional[str]:
        with self._lock:
            rows = list(self.results)
        if not rows:
            return None
        found = [r for r in rows if r.get("tx_hash")]
        boosted = sum(1 for r in found if r["timeboosted"] is True)
        regular = sum(1 for r in found if r["timeboosted"] is False)
        unknown = len(found) - boosted - regular
        arbs = sum(1 for r in found if r["kind"].startswith("arbitrage"))
        next_block = sum(1 for r in found if r["blocks_after_last_open"] == 1)
        early = sum(1 for r in found if r["blocks_after_last_open"] == 1 and r["tx_index"] != ""
                    and r["tx_index"] <= 2)
        line = (f"closers found: {len(found)} of {len(rows)} gaps (express lane {boosted}, regular {regular}"
                f"{f', unknown {unknown}' if unknown else ''}); arbitrage bots {arbs}; "
                f"landed in the very next block {next_block} ({early} among its first 2 transactions)")
        top = Counter(r["to"].lower() for r in found if r["to"]).most_common(1)
        if top and top[0][1] > 1:
            line += f"; most frequent closer contract {top[0][0]} ({top[0][1]}x)"
        return line
