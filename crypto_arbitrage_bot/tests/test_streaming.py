import asyncio
import logging
import tempfile
import time
import unittest

from arbbot.bot import ArbitrageBot
from arbbot.config import Config
from arbbot.executor import PaperExecutor
from arbbot.journal import Journal
from arbbot.market_data import ExchangeHub
from arbbot.risk import RiskLimits, RiskManager

SYMBOL = "BTC/USDT"


class FakeClient:
    """Minimal stand-in for a ccxt.pro exchange."""

    def __init__(self, streaming=True, rest_book=None):
        self.has = {"watchOrderBook": streaming}
        self.markets = {SYMBOL: {"taker": 0.001, "limits": {"amount": {"min": 0.0001}}}}
        self.queue = asyncio.Queue()
        self.rest_book = rest_book
        self.rest_calls = 0
        self.closed = False

    async def load_markets(self):
        return self.markets

    async def watch_order_book(self, symbol):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_order_book(self, symbol, limit=None):
        self.rest_calls += 1
        return self.rest_book

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1e6) / 1e6:.6f}"

    async def close(self):
        self.closed = True


def raw(bids, asks):
    return {"bids": [list(l) for l in bids], "asks": [list(l) for l in asks]}


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class StreamingHubTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def _hub(self, clients, **kw):
        hub = ExchangeHub({ex: {} for ex in clients}, use_credentials=False,
                          feed="websocket", clients=clients, **kw)
        hub.reconnect_backoff_s = 0.01
        return hub

    def test_update_wakes_bot_immediately_and_caches_book(self):
        async def go():
            a = FakeClient()
            hub = self._hub({"a": a}, depth=2, min_cycle_interval_s=0)
            await hub.load([SYMBOL])
            started = time.time()
            waiter = asyncio.create_task(hub.wait_for_update(timeout=5))
            book = raw([(99, 1), (98, 1), (97, 1)], [(100, 1)])
            await a.queue.put(book)
            await waiter
            elapsed = time.time() - started
            book["bids"][0][0] = 1  # ccxt mutates streaming books in place
            books = await hub.fetch_books(SYMBOL)
            await hub.close()
            return elapsed, books, a

        elapsed, books, client = asyncio.run(go())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(books["a"].bids, [(99.0, 1.0), (98.0, 1.0)])  # copied + sliced to depth
        self.assertTrue(client.closed)

    def test_wait_times_out_without_updates(self):
        async def go():
            hub = self._hub({"a": FakeClient()}, min_cycle_interval_s=0)
            await hub.load([SYMBOL])
            started = time.time()
            await hub.wait_for_update(timeout=0.05)
            await hub.close()
            return time.time() - started

        self.assertGreaterEqual(asyncio.run(go()), 0.04)

    def test_error_drops_book_then_reconnects(self):
        async def go():
            a = FakeClient()
            hub = self._hub({"a": a})
            await hub.load([SYMBOL])
            await a.queue.put(raw([(99, 1)], [(100, 1)]))
            await settle()
            before = await hub.fetch_books(SYMBOL)
            await a.queue.put(ConnectionError("socket closed"))
            await settle()
            during = await hub.fetch_books(SYMBOL)
            await a.queue.put(raw([(98, 1)], [(101, 1)]))
            await asyncio.sleep(0.05)  # > reconnect backoff
            after = await hub.fetch_books(SYMBOL)
            await hub.close()
            return before, during, after

        before, during, after = asyncio.run(go())
        self.assertIn("a", before)
        self.assertEqual(during, {})  # never trade on a dead connection's prices
        self.assertEqual(after["a"].best_bid, 98.0)

    def test_exchange_without_websockets_is_polled(self):
        async def go():
            b = FakeClient(streaming=False, rest_book=raw([(99, 1)], [(100, 1)]))
            hub = self._hub({"b": b}, poll_interval_s=0.01)
            await hub.load([SYMBOL])
            await asyncio.sleep(0.05)
            books = await hub.fetch_books(SYMBOL)
            await hub.close()
            return books, b.rest_calls

        books, calls = asyncio.run(go())
        self.assertEqual(books["b"].best_ask, 100.0)
        self.assertGreater(calls, 1)

    def test_bot_trades_off_streamed_books(self):
        async def go():
            a, b = FakeClient(), FakeClient()
            hub = self._hub({"a": a, "b": b}, min_cycle_interval_s=0)
            await hub.load([SYMBOL])
            tmp = tempfile.mkdtemp()
            cfg = Config(mode="paper", symbols=[SYMBOL], exchanges={"a": {}, "b": {}},
                         max_trade_quote=100, slippage_buffer_pct=0, log_dir=tmp)
            executor = PaperExecutor({ex: {"USDT": 1000, "BTC": 1} for ex in ("a", "b")})
            bot = ArbitrageBot(cfg, hub, executor, RiskManager(RiskLimits(min_profit_pct=0.1)),
                               Journal(tmp))
            await a.queue.put(raw([(99, 5)], [(100, 5)]))
            await b.queue.put(raw([(102, 5)], [(103, 5)]))
            await hub.wait_for_update(timeout=1)
            await settle()
            executed = await bot.run_cycle()
            await hub.close()
            return bot, executed

        bot, executed = asyncio.run(go())
        self.assertEqual(len(executed), 1)
        self.assertEqual((executed[0].buy_exchange, executed[0].sell_exchange), ("a", "b"))
        self.assertGreater(bot.total_pnl, 0)
        self.assertIsNotNone(bot.stats[SYMBOL].avg_book_age_ms)

    def test_invalid_feed_rejected(self):
        with self.assertRaises(ValueError):
            ExchangeHub({"a": {}}, use_credentials=False, feed="carrier-pigeon", clients={})


if __name__ == "__main__":
    unittest.main()
