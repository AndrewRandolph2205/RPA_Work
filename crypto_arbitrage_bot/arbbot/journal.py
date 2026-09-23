"""CSV journals so every opportunity and trade can be audited later."""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from .executor import ExecutionResult
from .strategy import Opportunity

_OPP_FIELDS = ["time", "symbol", "buy_exchange", "sell_exchange", "amount", "buy_avg_price",
               "sell_avg_price", "buy_cost", "sell_proceeds", "net_profit", "net_profit_pct"]


class Journal:
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
            writer.writerow(row)

    @staticmethod
    def _opp_row(opp: Opportunity) -> dict:
        row = {f: getattr(opp, f) for f in _OPP_FIELDS[1:]}
        row["time"] = dt.datetime.now(dt.timezone.utc).isoformat()
        return row

    def opportunity(self, opp: Opportunity, decision: str) -> None:
        self._append("opportunities.csv", _OPP_FIELDS + ["decision"],
                     {**self._opp_row(opp), "decision": decision})

    def trade(self, opp: Opportunity, result: ExecutionResult, mode: str) -> None:
        fields = _OPP_FIELDS + ["mode", "success", "realized_pnl", "detail"]
        self._append("trades.csv", fields, {
            **self._opp_row(opp), "mode": mode, "success": result.success,
            "realized_pnl": result.realized_pnl, "detail": result.detail,
        })
