import asyncio
import csv
import tempfile
import time
import unittest

from arbbot.bot import ArbitrageBot
from arbbot.config import Config
from arbbot.executor import PaperExecutor
from arbbot.journal import Journal
from arbbot.orderbook import OrderBook, simulate_fill
from arbbot.risk import RiskLimits, RiskManager
from arbbot.strategy import evaluate, max_affordable_amount


def book(ex, bids, asks, ts=None, symbol="BTC/USDT"):
    return OrderBook(ex, symbol, bids, asks, time.time() if ts is None else ts)


class FakeHub:
    def __init__(self, books, fees):
        self.books = books
        self.fees = fees

    async def fetch_books(self, symbol):
        return {ex: b for ex, b in self.books.items() if b.symbol == symbol}

    def fee(self, ex, symbol):
        return self.fees[ex]

    def round_amount(self, symbol, amount, exchanges):
        return round(amount - 5e-7, 6)  # truncate to 6 dp like an exchange would


class OrderBookTests(unittest.TestCase):
    def test_walks_multiple_levels(self):
        fill = simulate_fill([(100, 1), (101, 1)], 1.5)
        self.assertAlmostEqual(fill.quote, 150.5)
        self.assertAlmostEqual(fill.avg_price, 150.5 / 1.5)
        self.assertEqual(fill.worst_price, 101)

    def test_insufficient_depth_returns_none(self):
        self.assertIsNone(simulate_fill([(100, 1)], 2))


class StrategyTests(unittest.TestCase):
    def test_profitable_after_fees(self):
        buy = book("a", bids=[(99, 5)], asks=[(100, 5)])
        sell = book("b", bids=[(101, 5)], asks=[(102, 5)])
        opp = evaluate(buy, sell, 1, buy_fee=0.001, sell_fee=0.001)
        self.assertAlmostEqual(opp.buy_cost, 100.1)
        self.assertAlmostEqual(opp.sell_proceeds, 100.899)
        self.assertAlmostEqual(opp.net_profit, 0.799)

    def test_fees_eat_the_spread(self):
        buy = book("a", bids=[(99, 5)], asks=[(100, 5)])
        sell = book("b", bids=[(100.3, 5)], asks=[(101, 5)])
        opp = evaluate(buy, sell, 1, buy_fee=0.0026, sell_fee=0.0026)
        self.assertLess(opp.net_profit, 0)

    def test_depth_reduces_profit(self):
        buy = book("a", bids=[], asks=[(100, 1), (100.8, 5)])
        sell = book("b", bids=[(101, 5)], asks=[])
        small = evaluate(buy, sell, 1, 0, 0)
        big = evaluate(buy, sell, 3, 0, 0)
        self.assertGreater(small.net_profit_pct, big.net_profit_pct)
        self.assertEqual(big.buy_limit_price, 100.8)

    def test_same_exchange_rejected(self):
        b = book("a", bids=[(101, 1)], asks=[(100, 1)])
        self.assertIsNone(evaluate(b, b, 1, 0, 0))

    def test_sizing_respects_both_balances(self):
        buy = book("a", bids=[], asks=[(100, 10)])
        self.assertAlmostEqual(max_affordable_amount(buy, 0, 1000, 0.5, 10_000), 0.5)
        self.assertLess(max_affordable_amount(buy, 0, 50, 10, 10_000), 0.5)
        self.assertLess(max_affordable_amount(buy, 0, 10_000, 10, 50), 0.5)


class RiskTests(unittest.TestCase):
    def _opp(self, profit=1.0, cost=100.0):
        buy = book("a", bids=[], asks=[(cost, 10)])
        sell = book("b", bids=[(cost + profit, 10)], asks=[])
        return evaluate(buy, sell, 1, 0, 0)

    def test_thresholds(self):
        rm = RiskManager(RiskLimits(min_profit_pct=0.5, min_profit_quote=0.1, min_trade_quote=10))
        self.assertTrue(rm.allow(self._opp(1.0))[0])
        self.assertFalse(rm.allow(self._opp(0.2))[0])
        self.assertFalse(rm.allow(self._opp(1.0, cost=5))[0])

    def test_daily_loss_limit_and_reset(self):
        now = [1_700_000_000.0]
        rm = RiskManager(RiskLimits(min_profit_pct=0, max_daily_loss_quote=5), clock=lambda: now[0])
        rm.record(-6, success=True)
        self.assertFalse(rm.allow(self._opp())[0])
        now[0] += 86_400
        self.assertTrue(rm.allow(self._opp())[0])

    def test_consecutive_failures_halt(self):
        rm = RiskManager(RiskLimits(min_profit_pct=0, max_consecutive_failures=2))
        rm.record(0, success=False)
        rm.record(0, success=False)
        ok, reason = rm.allow(self._opp())
        self.assertFalse(ok)
        self.assertIn("halted", reason)

    def test_stale_books(self):
        rm = RiskManager(RiskLimits(max_book_age_s=2))
        self.assertTrue(rm.is_fresh(book("a", [], [], ts=time.time())))
        self.assertFalse(rm.is_fresh(book("a", [], [], ts=time.time() - 5)))


class BotTests(unittest.TestCase):
    def _run(self, books, balances, limits=None):
        tmp = tempfile.mkdtemp()
        cfg = Config(mode="paper", symbols=["BTC/USDT"], exchanges={"a": {}, "b": {}},
                     max_trade_quote=100, slippage_buffer_pct=0, log_dir=tmp)
        executor = PaperExecutor(balances)
        bot = ArbitrageBot(cfg, FakeHub(books, {"a": 0.001, "b": 0.001}), executor,
                           RiskManager(limits or RiskLimits(min_profit_pct=0.1)), Journal(tmp))
        executed = asyncio.run(bot.run_cycle())
        return bot, executor, executed, tmp

    def test_paper_trade_moves_inventory_and_books_profit(self):
        books = {"a": book("a", [(99, 5)], [(100, 5)]), "b": book("b", [(102, 5)], [(103, 5)])}
        balances = {"a": {"USDT": 1000, "BTC": 1}, "b": {"USDT": 1000, "BTC": 1}}
        bot, executor, executed, tmp = self._run(books, balances)

        self.assertEqual(len(executed), 1)
        opp = executed[0]
        self.assertEqual((opp.buy_exchange, opp.sell_exchange), ("a", "b"))
        bal = asyncio.run(executor.balances())
        self.assertAlmostEqual(bal["a"]["BTC"], 1 + opp.amount)
        self.assertAlmostEqual(bal["b"]["BTC"], 1 - opp.amount)
        total_usdt = bal["a"]["USDT"] + bal["b"]["USDT"]
        self.assertAlmostEqual(total_usdt - 2000, bot.total_pnl)
        self.assertGreater(bot.total_pnl, 0)
        with open(f"{tmp}/trades.csv") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)

    def test_no_trade_without_inventory_on_sell_side(self):
        books = {"a": book("a", [(99, 5)], [(100, 5)]), "b": book("b", [(102, 5)], [(103, 5)])}
        balances = {"a": {"USDT": 1000}, "b": {"USDT": 1000}}
        bot, _, executed, _ = self._run(books, balances)
        self.assertEqual(executed, [])

    def test_no_trade_when_spread_below_fees(self):
        books = {"a": book("a", [(99, 5)], [(100, 5)]), "b": book("b", [(100.15, 5)], [(101, 5)])}
        balances = {"a": {"USDT": 1000, "BTC": 1}, "b": {"USDT": 1000, "BTC": 1}}
        bot, _, executed, _ = self._run(books, balances)
        self.assertEqual(executed, [])
        self.assertEqual(bot.stats["BTC/USDT"].profitable, 0)

    def test_stale_book_ignored(self):
        books = {"a": book("a", [(99, 5)], [(100, 5)]),
                 "b": book("b", [(102, 5)], [(103, 5)], ts=time.time() - 60)}
        balances = {"a": {"USDT": 1000, "BTC": 1}, "b": {"USDT": 1000, "BTC": 1}}
        _, _, executed, _ = self._run(books, balances)
        self.assertEqual(executed, [])

    def test_scan_mode_logs_a_persisting_gap_once(self):
        tmp = tempfile.mkdtemp()
        cfg = Config(mode="scan", symbols=["BTC/USDT"], exchanges={"a": {}, "b": {}},
                     max_trade_quote=100, slippage_buffer_pct=0, log_dir=tmp)
        books = {"a": book("a", [(99, 5)], [(100, 5)]), "b": book("b", [(102, 5)], [(103, 5)])}
        bot = ArbitrageBot(cfg, FakeHub(books, {"a": 0.001, "b": 0.001}), None,
                           RiskManager(RiskLimits(min_profit_pct=0.1)), Journal(tmp))
        for _ in range(3):
            asyncio.run(bot.run_cycle())
        with open(f"{tmp}/opportunities.csv") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)
        self.assertEqual(bot.stats["BTC/USDT"].profitable, 3)


if __name__ == "__main__":
    unittest.main()
