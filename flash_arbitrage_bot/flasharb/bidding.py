"""Bidding on chains that order transactions by priority fee.

A fixed share of the expected profit overpays whenever the competition bids
little, and underbids when it bids a lot. BidBook remembers what the
transactions that took gaps actually paid per gas, next to how big each gap
was, and suggests a bid just above what usually wins for a gap that size.

Observations come from paper trading (the winning transaction of every
contested gap, whether we'd have beaten it or not), so the book fills up as
paper mode runs, and past runs' paper_trades.csv files seed it at startup.
The suggestion never exceeds the bot's usual bid (priority_fee_share of the
expected profit above min_profit_usd): learning can make bids cheaper or
closer to the market, never bigger than the trade can afford.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Optional, Tuple

_KEEP = 500   # most recent observations kept


def _percentile(values: List[int], q: float) -> Optional[int]:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))] if ordered else None


class BidBook:
    def __init__(self, percentile: float = 0.75, margin: float = 0.10, min_samples: int = 5):
        self.percentile = percentile
        self.margin = margin
        self.min_samples = min_samples
        self._seen: List[Tuple[float, int]] = []   # (gap_usd, winning tip in wei per gas)

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, gap_usd: float, tip_wei: int) -> None:
        if gap_usd > 0 and tip_wei >= 0:
            self._seen.append((float(gap_usd), int(tip_wei)))
            del self._seen[:-_KEEP]

    def load_paper_csvs(self, log_dir: str) -> int:
        """Seed from earlier paper runs (paper_trades*.csv). Returns observations read."""
        added = 0
        for path in sorted(Path(log_dir).glob("paper_trades*.csv")):
            try:
                with path.open(newline="") as fh:
                    for row in csv.DictReader(fh):
                        try:
                            tip = float(row.get("winner_tip_gwei") or "")
                            gap = float(row.get("est_profit_usd") or 0) - float(row.get("est_flash_fee_usd") or 0)
                        except ValueError:
                            continue
                        self.add(gap, int(tip * 1e9))
                        added += 1
            except OSError:
                continue
        return added

    def suggest(self, gap_usd: float) -> Optional[int]:
        """A bid (wei per gas) just above what usually wins for a gap this size,
        or None until enough winners have been seen."""
        if len(self._seen) < self.min_samples:
            return None
        similar = [tip for gap, tip in self._seen if gap / 2 <= gap_usd <= gap * 2]
        pool = similar if len(similar) >= self.min_samples else [tip for _, tip in self._seen]
        return int(_percentile(pool, self.percentile) * (1 + self.margin)) + 1

    def typical(self) -> Optional[int]:
        return _percentile([tip for _, tip in self._seen], self.percentile)
