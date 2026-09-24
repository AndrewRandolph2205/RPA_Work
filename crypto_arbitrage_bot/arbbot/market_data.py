"""Exchange connectivity through ccxt (imported lazily so tests need no deps).

Two price feeds are supported:

* ``rest``      - request every order book once per cycle (simple, slow).
* ``websocket`` - keep a streaming order book per exchange/symbol in memory
                  via ccxt.pro and wake the bot the moment any book changes.
                  Exchanges without websocket support fall back to REST
                  polling in the background, so one exchange can't block others.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .orderbook import OrderBook

log = logging.getLogger(__name__)

FEEDS = ("rest", "websocket")
_MAX_BACKOFF_S = 30.0


def _build_clients(exchanges: Mapping[str, Mapping], use_credentials: bool, feed: str):
    if feed == "websocket":
        import ccxt.pro as ccxt  # noqa: WPS433 - optional heavy dependency
    else:
        import ccxt.async_support as ccxt  # noqa: WPS433

    clients = {}
    for ex_id in exchanges:
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
        clients[ex_id] = getattr(ccxt, ex_id)(params)
    return clients


def _to_book(ex_id: str, symbol: str, raw: Mapping, depth: int, observed: float) -> OrderBook:
    # Copy out of ccxt's structure: streaming books are mutated in place.
    return OrderBook(
        exchange=ex_id,
        symbol=symbol,
        bids=[(float(p), float(a)) for p, a, *_ in raw["bids"][:depth]],
        asks=[(float(p), float(a)) for p, a, *_ in raw["asks"][:depth]],
        timestamp=observed,
    )


class ExchangeHub:
    reconnect_backoff_s = 1.0  # first retry delay; doubles up to _MAX_BACKOFF_S

    def __init__(
        self,
        exchanges: Mapping[str, Mapping],
        use_credentials: bool,
        feed: str = "websocket",
        depth: int = 20,
        poll_interval_s: float = 1.0,
        min_cycle_interval_s: float = 0.05,
        clients: Optional[Mapping] = None,  # injected by tests
    ):
        if feed not in FEEDS:
            raise ValueError(f"price_feed must be one of {FEEDS}, got {feed!r}")
        self.feed = feed
        self._depth = depth
        self._poll_interval_s = poll_interval_s
        self._min_cycle_interval_s = min_cycle_interval_s
        self._clients = dict(clients) if clients is not None else _build_clients(
            exchanges, use_credentials, feed)
        self._fee_overrides: Dict[str, float] = {
            ex_id: float(opts["taker_fee"]) for ex_id, opts in exchanges.items() if "taker_fee" in opts
        }
        # Streaming state
        self._books: Dict[Tuple[str, str], OrderBook] = {}
        self._tasks: list[asyncio.Task] = []
        self._updated = asyncio.Event()
        self._last_wake = 0.0

    @property
    def exchanges(self) -> Iterable[str]:
        return self._clients.keys()

    def client(self, ex_id: str):
        return self._clients[ex_id]

    async def load(self, symbols: Iterable[str]) -> None:
        symbols = list(symbols)
        await asyncio.gather(*(c.load_markets() for c in self._clients.values()))
        for symbol in symbols:
            listed = [ex for ex, c in self._clients.items() if symbol in c.markets]
            if len(listed) < 2:
                log.warning("%s is listed on fewer than two exchanges (%s)", symbol, listed)
        if self.feed == "websocket":
            self._start_streams(symbols)

    def fee(self, ex_id: str, symbol: str) -> float:
        if ex_id in self._fee_overrides:
            return self._fee_overrides[ex_id]
        market = self._clients[ex_id].markets.get(symbol, {})
        # Fall back to a pessimistic 0.5% if the exchange does not report fees.
        return float(market.get("taker") or 0.005)

    # ----- streaming -------------------------------------------------------

    def _start_streams(self, symbols: Iterable[str]) -> None:
        for ex_id, client in self._clients.items():
            streaming = bool(client.has.get("watchOrderBook"))
            if not streaming:
                log.warning("%s has no websocket order books; polling it every %.1fs",
                            ex_id, self._poll_interval_s)
            for symbol in symbols:
                if symbol in client.markets:
                    self._tasks.append(asyncio.create_task(
                        self._stream(ex_id, symbol, streaming), name=f"book:{ex_id}:{symbol}"))

    async def _stream(self, ex_id: str, symbol: str, streaming: bool) -> None:
        """Keep one order book current forever, reconnecting with backoff."""
        client = self._clients[ex_id]
        backoff = self.reconnect_backoff_s
        while True:
            try:
                if streaming:
                    # No limit argument: several exchanges only accept specific
                    # depths. We slice to our depth locally instead.
                    raw = await client.watch_order_book(symbol)
                else:
                    raw = await client.fetch_order_book(symbol, self._depth)
                self._books[(ex_id, symbol)] = _to_book(ex_id, symbol, raw, self._depth, time.time())
                self._updated.set()
                backoff = self.reconnect_backoff_s
                if not streaming:
                    await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Drop the cached book so a dead connection can't feed us old prices.
                self._books.pop((ex_id, symbol), None)
                log.warning("book feed %s %s error: %s (retry in %.0fs)", ex_id, symbol, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def wait_for_update(self, timeout: float) -> None:
        """REST: sleep until the next poll. Websocket: return as soon as any book changes."""
        if self.feed == "rest":
            await asyncio.sleep(timeout)
            return
        since_last = time.time() - self._last_wake
        if since_last < self._min_cycle_interval_s:
            # Books can update hundreds of times a second; batch them.
            await asyncio.sleep(self._min_cycle_interval_s - since_last)
        try:
            await asyncio.wait_for(self._updated.wait(), timeout=max(timeout, 0.001))
        except asyncio.TimeoutError:
            pass
        self._updated.clear()
        self._last_wake = time.time()

    # ----- snapshots -------------------------------------------------------

    async def _fetch_book(self, ex_id: str, symbol: str) -> Optional[OrderBook]:
        client = self._clients[ex_id]
        if symbol not in client.markets:
            return None
        started = time.time()
        try:
            raw = await client.fetch_order_book(symbol, self._depth)
        except Exception as exc:  # network errors should skip a cycle, not crash the bot
            log.warning("order book %s %s failed: %s", ex_id, symbol, exc)
            return None
        return _to_book(ex_id, symbol, raw, self._depth, started)  # request start: conservative age

    async def fetch_books(self, symbol: str) -> Dict[str, OrderBook]:
        if self.feed == "websocket":
            return {ex: book for (ex, sym), book in self._books.items() if sym == symbol}
        books = await asyncio.gather(*(self._fetch_book(ex, symbol) for ex in self._clients))
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
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)
