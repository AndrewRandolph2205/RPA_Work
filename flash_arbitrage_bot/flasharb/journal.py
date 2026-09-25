"""CSV journals so every opportunity, check and transaction can be audited later."""

from __future__ import annotations

import csv
import datetime as dt
import threading
from pathlib import Path


class Journal:
    OPPORTUNITY_FIELDS = ["time", "block", "route", "amount_in", "profit_usd", "flash_fee_usd", "gas_usd",
                          "net_usd", "decision"]
    TRADE_FIELDS = ["time", "block", "mined_block", "route", "amount_in", "tx_hash", "success", "timeboosted",
                    "profit_usd", "gas_usd", "net_usd", "error"]
    GAP_FIELDS = ["time", "route", "first_block", "last_block", "closed_by_block", "blocks_open", "net_usd",
                  "pools"]
    # One row per exact check of a candidate (scan mode): estimate vs reality at the same block.
    CHECK_FIELDS = ["time", "est_block", "check_block", "method", "route", "amount_in", "est_net_usd",
                    "real_net_usd", "result", "cause", "hop", "detail"]
    CLOSER_FIELDS = ["time", "route", "first_block", "last_open_block", "closed_by_block", "closer_block",
                     "blocks_after_last_open", "tx_index", "tx_hash", "from", "to", "timeboosted",
                     "pools_touched", "txs_in_window", "gas_used", "priority_fee_gwei", "kind"]
    # One row per paper order (paper mode), written once it has settled.
    PAPER_FIELDS = ["time", "paper_id", "status", "cause", "route", "pools", "amount_in", "amount_in_usd",
                    "decided_at", "detect_block", "chain_head_block", "blocks_behind", "decision_ms",
                    "latency_source", "send_latency_ms",
                    "timeboost_delay_ms", "express_lane", "total_delay_ms", "landing_block", "blocks_late",
                    "est_profit_usd", "est_flash_fee_usd", "est_gas_usd", "est_net_usd", "min_profit_usd",
                    "exact_profit_at_detect_usd", "exact_profit_at_landing_usd", "open_after_landing",
                    "amount_out", "profit_usd", "flash_fee_usd", "gas_units", "gas_price_gwei", "gas_usd",
                    "net_usd", "zero_delay_net_usd", "latency_cost_usd", "check_method", "reason",
                    "winner_block", "winner_position", "winner_block_txs", "winner_tx", "winner_to",
                    "winner_timeboosted", "winner_kind", "our_ms_into_block", "our_tip_gwei", "winner_tip_gwei",
                    "cum_orders", "cum_settled", "cum_filled", "cum_net_usd", "cum_gas_usd", "fill_rate"]

    def __init__(self, log_dir: str):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # the closer tracer writes from its own thread
        self._checked = set()

    def _rotate_if_changed(self, path: Path, fields) -> None:
        """A file written by an older version has other columns: keep it, start a new one."""
        if not path.exists():
            return
        with path.open(newline="") as fh:
            header = next(csv.reader(fh), None)
        if header is not None and header != list(fields):
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))

    def _append(self, name: str, fields, row: dict) -> None:
        path = self._dir / name
        with self._lock:
            if name not in self._checked:
                self._rotate_if_changed(path, fields)
                self._checked.add(name)
            new = not path.exists()
            with path.open("a", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
                if new:
                    writer.writeheader()
                writer.writerow({"time": dt.datetime.now(dt.timezone.utc).isoformat(), **row})

    def opportunity(self, **row) -> None:
        self._append("opportunities.csv", self.OPPORTUNITY_FIELDS, row)

    def gap(self, **row) -> None:
        self._append("gaps.csv", self.GAP_FIELDS, row)

    def check(self, **row) -> None:
        self._append("checks.csv", self.CHECK_FIELDS, row)

    def closer(self, **row) -> None:
        self._append("closers.csv", self.CLOSER_FIELDS, row)

    def trade(self, **row) -> None:
        self._append("trades.csv", self.TRADE_FIELDS, row)

    def paper_trade(self, **row) -> None:
        self._append("paper_trades.csv", self.PAPER_FIELDS, row)
