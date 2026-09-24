"""Sequencer feed, closer tracing, the live fast path, and pinned/fallback RPC reads."""

import base64
import csv
import json
import logging
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from flasharb.bot import FlashBot
from flasharb.closers import CloserTracer, GapRecord

from flasharb.feed import SequencerFeed, check_offset, express_count
from flasharb.journal import Journal
from flasharb.risk import RiskManager
from tests.test_flasharb import (E6, E18, USDC, WETH, FakeChain, FakeExecutor, exact_checker, make_config,
                                 pool)

OFFSET = 22207817


def feed_message(*seqs, meta=None, hashes=True):
    return json.dumps({"version": 1, "messages": [
        {"sequenceNumber": s, "message": {}, "signature": None,
         **({"blockHash": f"0x{s:064x}"} if hashes else {}),
         **({"blockMetadata": meta} if meta is not None else {})} for s in seqs]})


class FeedTests(unittest.TestCase):
    def test_express_lane_bitmap(self):
        self.assertEqual(express_count(base64.b64encode(bytes([0, 0b01100000])).decode()), 2)
        self.assertEqual(express_count("0x000100"), 1)
        self.assertEqual(express_count([0, 255]), 8)
        self.assertEqual(express_count(base64.b64encode(bytes([1, 255])).decode()), 0)  # unknown version
        self.assertEqual(express_count(None), 0)
        self.assertEqual(express_count("not base64!"), 0)

    def test_messages_become_block_numbers(self):
        feed = SequencerFeed("wss://x", OFFSET)
        meta = base64.b64encode(bytes([0, 0b10000000])).decode()
        self.assertEqual(feed.handle_message(feed_message(100, 101, meta=meta)), 2)
        self.assertEqual(feed.latest_block(), 101 + OFFSET)
        self.assertEqual(feed.handle_message(feed_message(101)), 0)  # replay after reconnect: ignored
        feed.handle_message(feed_message(105))
        self.assertEqual(feed.stats.sequence_gaps, 3)
        self.assertEqual((feed.stats.blocks, feed.stats.express_blocks, feed.stats.express_txs), (3, 2, 2))
        self.assertIsNotNone(feed.age_ms(105 + OFFSET))
        self.assertEqual(feed.recent_hashes()[0], (105 + OFFSET, f"0x{105:064x}"))
        self.assertEqual(feed.handle_message("{not json"), 0)
        self.assertEqual(feed.stats.bad_messages, 1)

    def test_wait_for_block_after(self):
        feed = SequencerFeed("wss://x", 0)
        self.assertIsNone(feed.wait_for_block_after(None, timeout=0.01))
        feed.handle_message(feed_message(10))
        self.assertEqual(feed.wait_for_block_after(9, timeout=0), 10)
        self.assertIsNone(feed.wait_for_block_after(10, timeout=0.01))
        threading.Timer(0.05, feed.handle_message, [feed_message(11)]).start()
        started = time.monotonic()
        self.assertEqual(feed.wait_for_block_after(10, timeout=2), 11)  # woken by the new message
        self.assertLess(time.monotonic() - started, 1)

    def test_offset_check(self):
        class Rpc:
            def __init__(self, offset):
                self.offset = offset

            def head(self):
                return 10 ** 9

            def block_hash(self, number):
                return f"0x{number - self.offset:064x}"

            def block_number_by_hash(self, h):
                return int(h, 16) + self.offset

        feed = SequencerFeed("wss://x", OFFSET)
        feed.handle_message(feed_message(500))
        self.assertEqual(check_offset(feed, Rpc(OFFSET)), "ok")
        self.assertEqual(check_offset(feed, Rpc(OFFSET + 1)), "corrected")
        self.assertEqual(feed.block_offset, OFFSET + 1)
        self.assertEqual(feed.latest_block(), 500 + OFFSET + 1)
        no_hashes = SequencerFeed("wss://x", 0)
        no_hashes.handle_message(feed_message(10 ** 9 + 3, hashes=False))
        self.assertEqual(check_offset(no_hashes, Rpc(0)), "ok")  # 3 blocks ahead of the RPC: plausible


class FakeFeed:
    """Healthy feed that announces one new block per wait."""

    def __init__(self, first):
        self.next = first
        self.stats = SequencerFeed("wss://x", 0).stats

    def healthy(self):
        return True

    def wait_for_block_after(self, block, timeout):
        self.next += 1
        return self.next

    def age_ms(self, block):
        return 42.0

    def recent_hashes(self):
        return [(self.next, "0xabc")]

    def latest_block(self):
        return self.next


class FeedDrivenBotTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_bot_follows_the_feed_after_checking_block_numbers(self):
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        chain = FakeChain([cheap])
        chain.head = lambda: 10 ** 6
        chain.block_hash = lambda n: "0xabc"
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp), feed=FakeFeed(500))
        bot.run(max_blocks=3)
        self.assertEqual(bot.stats.blocks, 3)
        self.assertEqual(bot.stats.skipped_blocks, 0)
        self.assertEqual(chain.block, 503)  # read pinned to exactly the announced blocks
        summary = bot.summary()
        self.assertIn("source=feed", summary)
        self.assertIn("pools ready 42ms after the feed announced each block", summary)


class FakeHistory:
    """logs_for / receipt_raw stand-ins for closer tracing."""

    def __init__(self, logs, receipts):
        self.logs, self.receipts = logs, receipts

    def logs_for(self, addresses, lo, hi):
        return [e for e in self.logs if lo <= int(e["blockNumber"], 16) <= hi]

    def receipt_raw(self, tx_hash):
        return self.receipts.get(tx_hash)


def log_entry(block, index, tx, address, log_index=0):
    return {"blockNumber": hex(block), "transactionIndex": hex(index), "logIndex": hex(log_index),
            "transactionHash": tx, "address": address}


class CloserTests(unittest.TestCase):
    A, B = "0x" + "a" * 40, "0x" + "b" * 40

    def gap(self):
        return GapRecord("WETH -[x]-> T -[y]-> WETH", (self.A, self.B), 100, 101, 103, 2.5)

    def test_finds_the_arbitrage_that_closed_it(self):
        logs = [log_entry(99, 1, "0xold", self.A),                    # before the gap: ignored
                log_entry(102, 3, "0xtrade", self.A),                 # a plain trade through one pool
                log_entry(102, 1, "0xarb", self.A, 0), log_entry(102, 1, "0xarb", self.B, 1)]
        receipts = {"0xarb": {"from": "0xbot", "to": "0xbotcontract", "timeboosted": True, "gasUsed": "0x5208"}}
        tmp = tempfile.mkdtemp()
        tracer = CloserTracer(FakeHistory(logs, receipts), Journal(tmp))
        tracer.submit(self.gap())
        tracer.wait()
        row = tracer.results[0]
        self.assertEqual((row["tx_hash"], row["closer_block"], row["tx_index"]), ("0xarb", 102, 1))
        self.assertEqual((row["timeboosted"], row["pools_touched"], row["txs_in_window"]), (True, "2/2", 2))
        self.assertEqual(row["blocks_after_last_open"], 1)
        self.assertTrue(row["kind"].startswith("arbitrage"))
        self.assertIn("express lane 1, regular 0", tracer.summary())
        self.assertIn("landed in the very next block 1 (1 among its first 2 transactions)", tracer.summary())
        with open(f"{tmp}/closers.csv") as fh:
            self.assertEqual(next(csv.DictReader(fh))["timeboosted"], "True")

    def test_no_transaction_means_the_estimate_moved(self):
        tracer = CloserTracer(FakeHistory([], {}), Journal(tempfile.mkdtemp()))
        row = tracer.trace(self.gap())
        self.assertEqual(row["tx_hash"], "")
        self.assertIn("no transaction touched", row["kind"])

    def test_lookup_errors_are_recorded_not_raised(self):
        class Broken:
            def logs_for(self, *a):
                raise RuntimeError("eth_getLogs: range too large")
        tracer = CloserTracer(Broken(), Journal(tempfile.mkdtemp()))
        tracer.submit(self.gap())
        tracer.wait()
        self.assertIn("lookup failed", tracer.results[0]["kind"])

    def test_bot_hands_closed_gaps_to_the_tracer(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        chain = FakeChain([cheap, dear])
        chain.verify_route = exact_checker()

        class Recorder:
            gaps = []

            def submit(self, gap):
                self.gaps.append(gap)

            def summary(self):
                return None

        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp), tracer=Recorder())
        bot.step()                      # block 101: gap verified
        bot.step()                      # block 102: still open
        dear.update_v2(1000 * E18, 2_000_000 * E6)
        chain.block += 2                # the bot misses two blocks
        bot.step()                      # block 105: gone
        gap = Recorder.gaps[0]
        self.assertEqual((gap.first_block, gap.last_open, gap.closed_by), (101, 102, 105))
        self.assertEqual(set(gap.pools), {cheap.address, dear.address})

    def test_resolve_pools_from_old_gap_rows(self):
        import run_flash
        v3 = pool("uniswap_v3", WETH, USDC, 1, 1, fee=500, kind="v3")
        v2 = pool("sushiswap", WETH, USDC, 1, 1)

        class Chain:
            cfg = make_config("scan", tempfile.mkdtemp())
            pools = [v3, v2]
        desc = "WETH -[uniswap_v3/0.05%]-> USDC -[sushiswap/0.3%]-> WETH"
        self.assertEqual(run_flash.resolve_pools(Chain, desc), (v3.address, v2.address))
        self.assertIsNone(run_flash.resolve_pools(Chain, "WETH -[camelot_v3]-> USDC -[sushiswap/0.3%]-> WETH"))


class TraceGapsCommandTests(unittest.TestCase):
    def test_traces_new_and_old_gap_rows(self):
        import contextlib
        import io
        from unittest import mock
        import run_flash
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        a = pool("dexa", WETH, USDC, 1, 1)
        b = pool("dexb", WETH, USDC, 1, 1)
        with open(f"{tmp}/gaps.csv", "w") as fh:  # current format: has pool addresses
            fh.write("time,route,first_block,last_block,closed_by_block,blocks_open,net_usd,pools\n"
                     f"t,WETH -[dexa/0.3%]-> USDC -[dexb/0.3%]-> WETH,100,100,101,1,1.5,{a.address} {b.address}\n")
        with open(f"{tmp}/gaps.20260924-000000.csv", "w") as fh:  # older format: pools only by label
            fh.write("time,route,first_block,last_block,blocks_open,net_usd\n"
                     "t,WETH -[dexb/0.3%]-> USDC -[dexa/0.3%]-> WETH,50,50,1,2.0\n")
        logs = [log_entry(101, 1, "0xnew", a.address), log_entry(101, 1, "0xnew", b.address, 1),
                log_entry(53, 2, "0xold", b.address)]
        history = FakeHistory(logs, {"0xnew": {"to": "0xbot", "timeboosted": False}, "0xold": {"to": "0xme"}})
        history.cfg, history.pools = cfg, [a, b]
        out = io.StringIO()
        with mock.patch.object(run_flash, "connect", return_value=history), \
                mock.patch.object(run_flash, "load_with_retries") as load, contextlib.redirect_stdout(out):
            code = run_flash.cmd_trace_gaps(cfg, type("Args", (), {"last": 0})())
        self.assertEqual(code, 0)
        load.assert_called_once()  # the old row needed the pool list
        text = out.getvalue()
        self.assertIn("closed in block +3 at index 2, lane unknown, single-pool trade", text)
        self.assertIn("closed in block +1 at index 1, regular, arbitrage", text)
        with open(f"{tmp}/closers.csv") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 2)


class SlowExecutor(FakeExecutor):
    """Receipts only arrive when release() is called."""

    def __init__(self):
        super().__init__()
        self.held = []

    def poll_results(self):
        return []

    def release(self):
        done, self._done = self._done, []
        return done


class LiveFastPathTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.tmp = tempfile.mkdtemp()
        self.cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def bot(self, executor, **cfg_overrides):
        cfg = make_config("live", self.tmp)
        for key, value in cfg_overrides.items():
            setattr(cfg, key, value)
        return FlashBot(cfg, FakeChain([self.cheap, self.dear]), executor, RiskManager(cfg.risk),
                        Journal(self.tmp))

    def test_sends_without_a_dry_run(self):
        ex = FakeExecutor()
        bot = self.bot(ex)
        bot.step()
        self.assertEqual((len(ex.simulated), len(ex.sent)), (0, 1))
        with open(f"{self.tmp}/trades.csv") as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual((row["success"], row["mined_block"], row["timeboosted"]), ("True", "1", "False"))

    def test_presimulate_option_dry_runs_first(self):
        ex = FakeExecutor()
        bot = self.bot(ex, presimulate_live=True)
        bot.step()
        self.assertEqual((len(ex.simulated), len(ex.sent)), (1, 1))

    def test_one_transaction_in_flight_at_a_time(self):
        ex = SlowExecutor()
        bot = self.bot(ex)
        bot.step()
        bot.step()
        self.assertEqual(len(ex.sent), 1)          # still waiting for the first receipt
        self.assertIn("in_flight=1", bot.summary())
        results = ex.release()
        ex.poll_results = lambda: results
        bot.step()                                 # receipt arrives, then it sends again
        self.assertEqual(bot.stats.succeeded, 1)
        self.assertEqual(len(ex.sent), 2)

    def test_failed_send_is_logged_and_not_counted(self):
        class Refusing(FakeExecutor):
            def submit(self, route, amount, min_profit):
                raise RuntimeError("nonce too low")
        bot = self.bot(Refusing())
        bot.step()
        self.assertEqual(bot.stats.sent, 0)
        with open(f"{self.tmp}/opportunities.csv") as fh:
            self.assertIn("send failed", fh.read())


def bare_chain(cfg, block_number=0):
    """A Chain without web3: only the attributes the tested methods touch."""
    from flasharb.chain import Chain
    chain = Chain.__new__(Chain)
    chain.cfg = cfg
    chain.rpc_behind_timeouts = 0
    chain.sim_supported = None
    chain.flash_fee_rate = 0.0
    chain._verify_executor = ThreadPoolExecutor(max_workers=2)
    chain.cs = lambda a: a
    chain.encode = lambda types, values: b""
    chain.w3 = type("W3", (), {"eth": type("Eth", (), {"block_number": block_number})()})()
    return chain


class ChainReadTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_pinned_read_waits_for_the_rpc_to_catch_up(self):
        chain = bare_chain(make_config("scan", tempfile.mkdtemp()))
        attempts = []

        def call_many(calls, block):
            attempts.append(block)
            if len(attempts) < 3:
                raise ValueError("{'code': -32000, 'message': 'header not found'}")
            return ["ok"]
        chain.call_many = call_many
        self.assertEqual(chain._read_at([], 777, wait_s=1.0), (["ok"], 777))
        self.assertEqual(attempts, [777, 777, 777])

    def test_pinned_read_falls_back_to_latest_when_rpc_lags(self):
        chain = bare_chain(make_config("scan", tempfile.mkdtemp()), block_number=775)

        def call_many(calls, block):
            if block > 775:
                raise ValueError("header not found")
            return [block]
        chain.call_many = call_many
        self.assertEqual(chain._read_at([], 777, wait_s=0.05), ([775], 775))
        self.assertEqual(chain.rpc_behind_timeouts, 1)

    def test_other_errors_are_not_retried(self):
        chain = bare_chain(make_config("scan", tempfile.mkdtemp()))

        def call_many(calls, block):
            raise ValueError("429 Too Many Requests")
        chain.call_many = call_many
        with self.assertRaises(ValueError):
            chain._read_at([], 777, wait_s=1.0)

    def test_falls_back_to_quoters_when_state_overrides_are_ignored(self):
        chain = bare_chain(make_config("scan", tempfile.mkdtemp()))
        chain._raw = lambda method, params: {"jsonrpc": "2.0", "id": 1, "result": "0x"}  # override ignored
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        from flasharb.routes import Route
        route = Route((dear, cheap), (WETH, USDC, WETH))
        outcome = chain.verify_route(route, [E18], 5)[0]
        self.assertEqual(outcome.method, "quoter")
        self.assertIs(chain.sim_supported, False)
        self.assertEqual(outcome.out, route.amount_out(E18))

    def test_sim_revert_data_is_decoded_when_supported(self):
        chain = bare_chain(make_config("scan", tempfile.mkdtemp()))
        chain._raw = lambda m, p: {"error": {"code": 3, "message": "execution reverted", "data": "0xdeadbeef"}}
        chain.decode = lambda types, data: (_ for _ in ()).throw(AssertionError("unexpected decode"))
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        from flasharb.routes import Route
        outcome = chain.verify_route(Route((cheap, cheap), (WETH, USDC, WETH)), [E18], 5)[0]
        self.assertEqual((outcome.method, outcome.failed_hop), ("sim", -1))  # unknown error: not a hop
        self.assertIs(chain.sim_supported, True)


if __name__ == "__main__":
    unittest.main()
