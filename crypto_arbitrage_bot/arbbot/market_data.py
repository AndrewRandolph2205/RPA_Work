"""Exchange connectivity through ccxt (imported lazily so tests need no deps)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Iterable, Mapping, Optional

from .orderbook import OrderBook

log = logging.getLogger(__name__)


class ExchangeHub:
    def __init__(self, exchanges: Mapping[str, Mapping], use_credentials: bool):
        import ccxt.async_support as ccxt  # noqa: WPS433 - optional heavy dependency

        self._fee_overrides: Dict[str, float] = {}
        self._clients = {}
        for ex_id, opts in exchanges.items():
            params = {"enableRateLimit": True}
            if use_credentials:
                prefix = ex_id.upper()
                params["apiKey"] = os.environ.get(f"{prefix}_API_KEY", "")
                params["secret"] = os.environ.get(f"{prefix}_API_SECRET", "")
                password = os.environ.get(f"{prefix}_API_PASSWORD")
                if password:
                    params["password"] = password
                if not params["apiKey"] or not params["secret"]:
                    raise RuntimeError(f"Missing {prefix}_API_KEY / {prefix}_API_SECRET env vars")
            self._clients[ex_id] = getattr(ccxt, ex_id)(params)
            if "taker_fee" in opts:
                self._fee_overrides[ex_id] = float(opts["taker_fee"])

    @property
    def exchanges(self) -> Iterable[str]:
        return self._clients.keys()

    def client(self, ex_id: str):
        return self._clients[ex_id]

    async def load(self, symbols: Iterable[str]) -> None:
        await asyncio.gather(*(c.load_markets() for c in self._clients.values()))
        for symbol in symbols:
            listed = [ex for ex, c in self._clients.items() if symbol in c.markets]
            if len(listed) < 2:
                log.warning("%s is listed on fewer than two exchanges (%s)", symbol, listed)

    def fee(self, ex_id: str, symbol: str) -> float:
        if ex_id in self._fee_overrides:
            return self._fee_overrides[ex_id]
        market = self._clients[ex_id].markets.get(symbol, {})
        # Fall back to a pessimistic 0.5% if the exchange does not report fees.
        return float(market.get("taker") or 0.005)

    async def _fetch_book(self, ex_id: str, symbol: str, depth: int) -> Optional[OrderBook]:
        client = self._clients[ex_id]
        if symbol not in client.markets:
            return None
        started = time.time()
        try:
            raw = await client.fetch_order_book(symbol, depth)
        except Exception as exc:  # network errors should skip a cycle, not crash the bot
            log.warning("order book %s %s failed: %s", ex_id, symbol, exc)
            return None
        return OrderBook(
            exchange=ex_id,
            symbol=symbol,
            bids=[(float(p), float(a)) for p, a, *_ in raw["bids"]],
            asks=[(float(p), float(a)) for p, a, *_ in raw["asks"]],
            timestamp=started,  # request start: conservative age estimate
        )

    async def fetch_books(self, symbol: str, depth: int) -> Dict[str, OrderBook]:
        books = await asyncio.gather(*(self._fetch_book(ex, symbol, depth) for ex in self._clients))
        return {b.exchange: b for b in books if b is not None}

    async def fetch_balances(self) -> Dict[str, Dict[str, float]]:
        async def one(ex_id):
            bal = await self._clients[ex_id].fetch_balance()
            return ex_id, {k: float(v or 0) for k, v in bal.get("free", {}).items()}

        return dict(await asyncio.gather(*(one(ex) for ex in self._clients)))

    def round_amount(self, symbol: str, amount: float, exchanges: Iterable[str]) -> float:
        """Round down to the coarsest precision among ``exchanges``; 0 if below minimums."""
        rounded = amount
        for ex_id in exchanges:
            client = self._clients[ex_id]
            try:
                rounded = min(rounded, float(client.amount_to_precision(symbol, amount)))
            except Exception:  # ccxt raises when the amount rounds to zero
                return 0.0
            min_amount = (client.markets[symbol].get("limits", {}).get("amount", {}) or {}).get("min")
            if min_amount and rounded < float(min_amount):
                return 0.0
        return rounded

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)
