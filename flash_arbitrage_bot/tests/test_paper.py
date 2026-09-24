"""Paper mode: live mode's decisions, settled against the chain, nothing sent."""

import argparse
import csv
import logging
import tempfile
import unittest

from flasharb.bot import FlashBot
from flasharb.config import validate
from flasharb.feed import FeedStats, SequencerFeed
from flasharb.journal import Journal
from flasharb.paper import PaperTrader, landing_block
from flasharb.risk import RiskManager
from flasharb.simulator import Outcome
from tests.test_flasharb import E6, E18, USDC, WETH, FakeChain, hop_amounts, make_config, pool
from tests.test_realtime import feed_message

IDLE, EXPRESS = "0x0000", "0x0001"   # blockMetadata: version 0, then the express-lane bitmap


class StubFeed:
    """A healthy feed reporting a fixed express-lane state."""

    def __init__(self, express_state):
        self.state = express_state
        self.stats = FeedStats()

    def healthy(self):
        return True

    def express_lane_state(self):
        return self.state

    def age_ms(self, block):
        return 10.0


def verify_by_block(gone_from=None, calls=None, error=None):
    """Stand-in for Chain.verify_route: the model's own per-hop amounts (the gap
    pays) at blocks before `gone_from`; from then on the trade comes back 1% short."""
    def verify(route, sizes, block):
        if calls is not None:
            calls.append(block)
        if error is not None:
            raise error
        outcomes = []
        for amount in sizes:
            hops = hop_amounts(route, amount)
            if gone_from is not None and block >= gone_from:
                hops[-1] = amount * 99 // 100
            outcomes.append(Outcome(amount, block, "sim", hops, [0] * len(hops)))
        return outcomes
    return verify


class LandingBlockTests(unittest.TestCase):
    def test_counts_whole_blocks_and_never_lands_in_the_same_block(self):
        self.assertEqual(landing_block(100, 0, 250), 101)
        self.assertEqual(landing_block(100, 230, 250), 101)
        self.assertEqual(landing_block(100, 250, 250), 101)
        self.assertEqual(landing_block(100, 251, 250), 102)
        self.assertEqual(landing_block(100, 370, 250), 102)   # 120ms decision + 50ms send + 200ms Timeboost
        self.assertEqual(landing_block(100, 1760, 250), 108)


class PaperModeTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.tmp = tempfile.mkdtemp()
        self.cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def bot(self, verify, decision_ms=10.0, send_ms=20.0, background=False, **settings):
        cfg = make_config("paper", self.tmp, **settings.pop("risk", {}))
        cfg.paper_send_latency_ms = send_ms
        for key, value in settings.items():
            setattr(cfg, key, value)
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = verify
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(self.tmp),
                       paper=PaperTrader(chain, cfg, background=background))
        bot._decision_latency = lambda: (decision_ms, "feed")  # fixed, so landing blocks are exact
        return bot

    def rows(self, name="paper_trades.csv"):
        with open(f"{self.tmp}/{name}") as fh:
            return list(csv.DictReader(fh))

    def test_fills_when_the_gap_is_still_there_when_it_lands(self):
        bot = self.bot(verify_by_block())      # 10 + 20 + 200 = 230ms: lands in the next block
        bot.step()                             # block 101: paper order
        self.assertEqual(bot.stats.paper_orders, 1)
        self.assertIn("paper order #1: would land in block 102", self.rows("opportunities.csv")[0]["decision"])
        bot.step()                             # block 102: it lands and settles
        row = self.rows()[0]
        self.assertEqual((row["status"], row["detect_block"], row["landing_block"], row["blocks_late"]),
                         ("filled", "101", "102", "1"))
        self.assertEqual((row["open_after_landing"], row["latency_source"], row["check_method"]),
                         ("True", "feed", "sim"))
        self.assertEqual((row["express_lane"], row["timeboost_delay_ms"]), ("unknown", "200.0"))  # no feed: held
        profit, gas, net = float(row["profit_usd"]), float(row["gas_usd"]), float(row["net_usd"])
        self.assertGreater(profit, 0)
        self.assertGreater(gas, 0)
        self.assertAlmostEqual(net, profit - gas, places=3)
        self.assertEqual((row["cum_filled"], row["fill_rate"]), ("1", "1.0"))
        self.assertAlmostEqual(float(row["total_delay_ms"]), 230.0)
        self.assertEqual(bot.stats.paper_filled, 1)
        self.assertAlmostEqual(bot.stats.paper_net_usd, net, places=3)
        self.assertFalse(bot._inflight)

    def test_gap_that_closes_before_landing_reverts_and_costs_gas(self):
        # 10 + 100 + 200 = 310ms: lands two blocks later (103); the gap is gone from block 102.
        bot = self.bot(verify_by_block(gone_from=102), send_ms=100.0)
        for _ in range(3):
            bot.step()
        row = self.rows()[0]
        self.assertEqual((row["status"], row["cause"], row["landing_block"]),
                         ("reverted", "closed before landing", "103"))
        self.assertEqual(float(row["profit_usd"]), 0.0)
        self.assertAlmostEqual(float(row["net_usd"]), -float(row["gas_usd"]), places=4)
        self.assertGreater(float(row["zero_delay_net_usd"]), 0)       # a bot with no delay would have won
        self.assertGreater(float(row["latency_cost_usd"]), float(row["zero_delay_net_usd"]))
        self.assertIn("gap closed before it landed", row["reason"])
        self.assertEqual(bot.stats.paper_causes["closed before landing"], 1)

    def test_taken_inside_the_landing_block_counts_as_lost(self):
        bot = self.bot(verify_by_block(gone_from=102))   # pays going into 102, gone by its end
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual((row["status"], row["open_after_landing"]), ("lost_race", "False"))
        self.assertLess(float(row["net_usd"]), 0)

    def test_same_block_can_count_as_won(self):
        bot = self.bot(verify_by_block(gone_from=102), paper_same_block_wins=True)
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual(row["status"], "filled")
        self.assertIn("counted as won", row["reason"])

    def test_estimate_that_never_paid(self):
        bot = self.bot(verify_by_block(gone_from=0))
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual((row["status"], row["cause"]), ("reverted", "estimate was off"))
        self.assertAlmostEqual(float(row["latency_cost_usd"]), 0.0, places=6)   # no delay would have lost too

    def test_one_order_in_flight_and_a_filled_gap_is_not_traded_twice(self):
        bot = self.bot(verify_by_block(), send_ms=390.0)   # 600ms: lands 3 blocks later
        bot.step()                                         # 101: order, lands in 104
        bot.step()                                         # 102: still in flight
        bot.step()                                         # 103: still in flight
        self.assertEqual(bot.stats.paper_orders, 1)
        bot.step()                                         # 104: settles as filled
        bot.step()                                         # 105: same gap still showing: already taken
        self.assertEqual((bot.stats.paper_orders, bot.stats.paper_filled), (1, 1))
        self.dear.update_v2(1000 * E18, 2_000_000 * E6)    # the gap goes away...
        bot.step()
        self.dear.update_v2(1000 * E18, 2_050_000 * E6)    # ...and a new one opens
        bot.step()
        self.assertEqual(bot.stats.paper_orders, 2)

    def test_a_reverted_gap_can_be_tried_again(self):
        bot = self.bot(verify_by_block(gone_from=0))
        bot.step()   # order 1
        bot.step()   # settles reverted, and the gap still shows: order 2
        self.assertEqual(bot.stats.paper_orders, 2)

    def test_revert_streak_is_reported_but_does_not_stop_paper_mode(self):
        bot = self.bot(verify_by_block(gone_from=0), risk={"max_consecutive_reverts": 2})
        bot.run(max_blocks=8)
        s = bot.stats
        self.assertGreaterEqual(s.paper_reverted, 4)
        self.assertGreaterEqual(s.paper_would_halt, 2)
        self.assertIsNone(bot.risk.halted_reason)
        self.assertIn("live mode would have halted", bot.summary())
        self.assertLess(s.paper_net_usd, 0)

    def test_failed_check_is_logged_but_not_counted(self):
        bot = self.bot(verify_by_block(error=ConnectionError("rpc down")))
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual((row["status"], row["net_usd"], row["cum_settled"]), ("check_failed", "0.0", "0"))
        self.assertIn("rpc down", row["reason"])
        self.assertEqual((bot.stats.paper_failed, bot.stats.paper_net_usd), (1, 0.0))

    def test_checks_the_spotted_block_and_both_sides_of_the_landing_block(self):
        calls = []
        bot = self.bot(verify_by_block(calls=calls), send_ms=100.0)   # lands in 103
        for _ in range(3):
            bot.step()
        self.assertEqual(sorted(calls), [101, 102, 103])

    def test_decision_delay_comes_from_the_feed(self):
        class Feed:
            def age_ms(self, block):
                return 120.0
        cfg = make_config("paper", self.tmp)
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = verify_by_block()
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(self.tmp),
                       paper=PaperTrader(chain, cfg, background=False))
        bot.feed = Feed()
        bot.last_block = 101
        self.assertEqual(bot._decision_latency(), (120.0, "feed"))
        bot.feed = None
        latency, source = bot._decision_latency()
        self.assertEqual(source, "poll")
        self.assertGreaterEqual(latency, 0.0)

    def test_follows_the_sequencer_feed_and_measures_the_delay_from_it(self):
        from tests.test_realtime import FakeFeed
        cfg = make_config("paper", self.tmp)      # default 50ms send + 200ms Timeboost
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = verify_by_block()
        feed = FakeFeed(500)
        chain.head = lambda: feed.next            # the RPC keeps up with the feed
        chain.block_hash = lambda n: "0xabc"
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(self.tmp), feed=feed)
        bot.run(max_blocks=6)                     # its own background settlement thread, as in a real run
        row = self.rows()[0]
        self.assertEqual((row["latency_source"], row["decision_ms"], row["total_delay_ms"]),
                         ("feed", "42.0", "292.0"))   # the feed says the block is 42ms old
        self.assertEqual(int(row["landing_block"]) - int(row["detect_block"]), 2)
        self.assertEqual(row["status"], "filled")
        self.assertIn("source=feed", bot.summary())

    def test_presimulate_live_skips_trades_that_fail_the_dry_run(self):
        bot = self.bot(verify_by_block(gone_from=0), presimulate_live=True)
        bot.step()
        self.assertEqual((bot.stats.paper_orders, bot.stats.sim_failed), (0, 1))
        self.assertIn("dry run failed", self.rows("opportunities.csv")[0]["decision"])

    def test_settles_on_a_background_thread(self):
        bot = self.bot(verify_by_block(), background=True)
        bot.step()
        bot.step()           # landing block reached: settlement starts on the paper thread
        bot.paper.wait()
        bot.step()           # result collected
        self.assertEqual(bot.stats.paper_filled, 1)
        self.assertEqual(self.rows()[0]["status"], "filled")

    def test_summary_reports_paper_results(self):
        bot = self.bot(verify_by_block(gone_from=103), send_ms=100.0)
        for _ in range(6):
            bot.step()
        text = bot.summary()
        self.assertIn("mode=paper", text)
        self.assertIn("paper orders=", text)
        self.assertIn("latency cost", text)
        self.assertIn("median delay 310ms", text)

    def test_every_field_is_written(self):
        bot = self.bot(verify_by_block())
        bot.chain.head = lambda: bot.chain.block
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual(list(row), Journal.PAPER_FIELDS)
        blank = [k for k, v in row.items() if v == ""]
        self.assertEqual(blank, [])   # a filled trade has every column

    def test_a_bot_behind_the_chain_lands_after_the_chains_head(self):
        # The feed timing says the block just appeared, but the RPC is already 10
        # blocks further on (the bot fell behind): the trade can't land before 112.
        bot = self.bot(verify_by_block(gone_from=104))
        bot.chain.head = lambda: bot.chain.block + 10
        bot.step()                                   # block 101: paper order, RPC at 111
        self.assertIn("chain already 10 block(s) past block 101", self.rows("opportunities.csv")[0]["decision"])
        for _ in range(11):                          # the bot reaches 112, where it settles
            bot.step()
        row = self.rows()[0]
        self.assertEqual((row["landing_block"], row["chain_head_block"], row["blocks_behind"], row["blocks_late"]),
                         ("112", "111", "10", "11"))
        self.assertEqual((row["status"], row["cause"]), ("reverted", "closed before landing"))
        self.assertIn("already 10 block(s) past block 101", row["reason"])
        self.assertIn("bot behind the chain when ordering: median 10 block(s), worst 10", bot.summary())

    def test_chain_head_never_moves_a_landing_earlier(self):
        bot = self.bot(verify_by_block())
        bot.chain.head = lambda: bot.chain.block     # the RPC is where the bot is
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual((row["landing_block"], row["chain_head_block"], row["blocks_behind"], row["status"]),
                         ("102", "101", "0", "filled"))
        self.assertNotIn("bot behind the chain", bot.summary())

    def test_unreadable_chain_head_leaves_the_landing_to_the_feed_timing(self):
        bot = self.bot(verify_by_block())

        def rpc_down():
            raise ConnectionError("rpc down")
        bot.chain.head = rpc_down
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual((row["landing_block"], row["chain_head_block"], row["blocks_behind"], row["status"]),
                         ("102", "", "", "filled"))


class ExpressLaneTests(unittest.TestCase):
    """Timeboost's hold only applies while someone uses the express lane."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.tmp = tempfile.mkdtemp()
        self.cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_feed_says_unknown_until_it_has_watched_a_full_round(self):
        feed = SequencerFeed("wss://x", 0)
        self.assertIsNone(feed.express_lane_state())
        feed.handle_message(feed_message(*range(1, 101), meta=IDLE))
        self.assertIsNone(feed.express_lane_state())          # 100 blocks: not a whole auction round yet
        feed.handle_message(feed_message(*range(101, 260), meta=IDLE))
        self.assertIs(feed.express_lane_state(), False)       # a whole round without an express-lane tx

    def test_feed_says_active_while_express_lane_txs_are_recent(self):
        feed = SequencerFeed("wss://x", 0)
        feed.handle_message(feed_message(*range(1, 300), meta=IDLE))
        feed.handle_message(feed_message(300, meta=EXPRESS))
        self.assertIs(feed.express_lane_state(), True)
        feed.handle_message(feed_message(*range(301, 540), meta=IDLE))
        self.assertIs(feed.express_lane_state(), True)        # 239 blocks later: same round, still in use
        feed.handle_message(feed_message(*range(540, 560), meta=IDLE))
        self.assertIs(feed.express_lane_state(), False)

    def test_feed_without_block_metadata_says_unknown(self):
        feed = SequencerFeed("wss://x", 0)
        feed.handle_message(feed_message(*range(1, 400)))     # relay sends no blockMetadata
        self.assertIsNone(feed.express_lane_state())
        feed.handle_message(feed_message(*range(400, 700), meta=IDLE))
        self.assertIs(feed.express_lane_state(), False)
        feed.handle_message(feed_message(*range(700, 750)))   # metadata stopped arriving
        self.assertIsNone(feed.express_lane_state())

    def bot(self, feed, **settings):
        cfg = make_config("paper", self.tmp)
        cfg.paper_send_latency_ms = 100.0   # with the 10ms decision: 110ms (next block) or 310ms (two blocks)
        for key, value in settings.items():
            setattr(cfg, key, value)
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = verify_by_block()
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(self.tmp),
                       paper=PaperTrader(chain, cfg, background=False))
        bot.feed = feed
        for _ in range(3):
            bot.step()
        with open(f"{self.tmp}/paper_trades.csv") as fh:
            return bot, next(csv.DictReader(fh))

    def test_idle_express_lane_means_no_hold(self):
        bot, row = self.bot(StubFeed(False))
        self.assertEqual((row["express_lane"], row["timeboost_delay_ms"], row["total_delay_ms"], row["blocks_late"]),
                         ("idle", "0.0", "110.0", "1"))
        self.assertIn("plus Timeboost's 200ms on 0 of 1 orders; express lane idle 1", bot.summary())

    def test_active_express_lane_holds_the_trade(self):
        _, row = self.bot(StubFeed(True))
        self.assertEqual((row["express_lane"], row["timeboost_delay_ms"], row["total_delay_ms"], row["blocks_late"]),
                         ("active", "200.0", "310.0", "2"))

    def test_unknown_express_lane_holds_the_trade(self):
        _, row = self.bot(StubFeed(None))
        self.assertEqual((row["express_lane"], row["timeboost_delay_ms"], row["blocks_late"]),
                         ("unknown", "200.0", "2"))

    def test_config_can_force_the_hold_on_or_off(self):
        _, row = self.bot(StubFeed(False), paper_timeboost="on")
        self.assertEqual((row["express_lane"], row["timeboost_delay_ms"], row["blocks_late"]),
                         ("forced on", "200.0", "2"))
        self.tmp = tempfile.mkdtemp()
        _, row = self.bot(StubFeed(True), paper_timeboost="off")
        self.assertEqual((row["express_lane"], row["timeboost_delay_ms"], row["blocks_late"]),
                         ("forced off", "0.0", "1"))


class PaperConfigTests(unittest.TestCase):
    def test_paper_settings_are_validated(self):
        cfg = make_config("paper", tempfile.mkdtemp())
        cfg.paper_block_time_ms = 0
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg.paper_block_time_ms, cfg.paper_send_latency_ms = 250, -1
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg.paper_send_latency_ms, cfg.paper_timeboost = 50, "sometimes"
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_paper_flag_runs_paper_mode_without_a_contract(self):
        import contextlib
        import io
        from unittest import mock
        import run_flash
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        chain = FakeChain([pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500),
                           pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)])
        chain.verify_route = verify_by_block()
        args = argparse.Namespace(paper=True, live=False, blocks=4)
        with mock.patch.object(run_flash, "connect", return_value=chain), \
                mock.patch.object(run_flash, "load_with_retries"), \
                mock.patch.object(run_flash, "check_simulator_source"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_flash.cmd_run(cfg, args), 0)
        self.assertEqual(cfg.mode, "paper")
        self.assertIn("Nothing is sent", out.getvalue())
        with open(f"{tmp}/paper_trades.csv") as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(rows)
        self.assertEqual(rows[0]["latency_source"], "poll")

    def test_paper_and_live_flags_cannot_be_combined(self):
        import run_flash
        cfg = make_config("scan", tempfile.mkdtemp())
        args = argparse.Namespace(paper=True, live=True, blocks=None)
        with self.assertRaises(SystemExit):
            run_flash.cmd_run(cfg, args)


if __name__ == "__main__":
    unittest.main()
