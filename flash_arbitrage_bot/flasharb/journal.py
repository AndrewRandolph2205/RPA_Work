"""CSV journals so every opportunity and transaction can be audited later."""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path


class Journal:
    OPPORTUNITY_FIELDS = ["time", "block", "route", "amount_in", "profit_usd", "gas_usd",
                          "net_usd", "decision"]
    TRADE_FIELDS = ["time", "block", "route", "amount_in", "tx_hash", "success",
                    "profit_usd", "gas_usd", "net_usd", "error"]
    GAP_FIELDS = ["time", "route", "first_block", "last_block", "blocks_open", "net_usd"]

    def __init__(self, log_dir: str):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _append(self, name: str, fields, row: dict) -> None:
        path = self._dir / name
        new = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerow({"time": dt.datetime.now(dt.timezone.utc).isoformat(), **row})

    def opportunity(self, **row) -> None:
        self._append("opportunities.csv", self.OPPORTUNITY_FIELDS, row)

    def gap(self, **row) -> None:
        self._append("gaps.csv", self.GAP_FIELDS, row)

    def trade(self, **row) -> None:
        self._append("trades.csv", self.TRADE_FIELDS, row)
