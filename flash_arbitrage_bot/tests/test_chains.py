"""Other chains: Solidly/Aerodrome pools, fee-ordered sequencers (priority-fee bids), newHeads as the block source."""

import csv
import json
import logging
import tempfile
import unittest

from flasharb.amm import ROUTER_KINDS
from flasharb.bot import FlashBot
from flasharb.closers import first_taker
from flasharb.config import validate
from flasharb.journal import Journal
from flasharb.logstate import HeadFeed, apply_event, decode_log
from flasharb.paper import PaperTrader
from flasharb.risk import RiskManager
from flasharb.simulator import route_steps
from tests.test_flasharb import E6, E18, USDC, WETH, FakeChain, FakeExecutor, make_config, pool
from tests.test_logstate import TOPIC, TOPICS, FakeClock, head, log_entry, logmsg, subscribed_stream
from tests.test_paper import verify_by_block

GWEI = 10 ** 9


class SolidlyPoolTests(unittest.TestCase):
    def test_amount_out_matches_velodrome_get_amount_out(self):
        p = pool("aero", WETH, USDC, 100 * E18, 200_000 * E6, fee=3000, kind="solidly")  # 30 bps
        amount = 12_345_678_901_234_567
        after_fee = amount - amount * 30 // 10_000
        self.assertEqual(p.amount_out(WETH, amount), after_fee * 200_000 * E6 // (100 * E18 + after_fee))
        self.assertEqual(p.label, "aero/0.3%")

    def test_route_step_is_kind_5_without_a_fee(self):
        a = pool("aero", WETH, USDC, 100 * E18, 200_000 * E6, kind="solidly")
        a.router_kind = "solidly"
        b = pool("uni", WETH, USDC, 100 * E18, 201_000 * E6)
        from flasharb.routes import Route
        steps = route_steps(Route((a, b), (WETH, USDC, WETH)))
        self.assertEqual((steps[0][0], steps[0][4]), (ROUTER_KINDS["solidly"], 0))

    def test_solidly_sync_event(self):
        p = pool("aero", WETH, USDC, E18, E18, kind="solidly")
        entry = log_entry("sync_solidly", p.address, 5, 0, data=(3 * E18, 4 * E18))
        self.assertTrue(apply_event(p, decode_log(entry, TOPICS)))
        self.assertEqual((p.reserve0, p.reserve1), (3 * E18, 4 * E18))
        self.assertIn("sync_solidly", TOPIC)


class AlgebraV1Tests(unittest.TestCase):
    """QuickSwap V3 (original Algebra): one dynamic fee for both directions."""

    def test_single_fee_event(self):
        p = pool("quick", WETH, USDC, E18, E18, kind="algebra_v1")
        p.fee1_ppm = 999
        entry = log_entry("fee_single", p.address, 5, 0, data=(450,))
        self.assertTrue(apply_event(p, decode_log(entry, TOPICS)))
        self.assertEqual((p.fee_for(WETH), p.fee_for(USDC)), (450, 450))
        other = pool("camelot", WETH, USDC, E18, E18, kind="algebra")
        apply_event(other, decode_log(log_entry("fee_single", other.address, 5, 0, data=(450,)), TOPICS))
        self.assertNotEqual(other.fee_ppm, 450)   # Camelot's pools send the two-fee event instead

    def test_is_concentrated_with_a_dynamic_fee(self):
        p = pool("quick", WETH, USDC, E18, E18, kind="algebra_v1")
        self.assertTrue(p.concentrated)
        self.assertEqual(p.label, "quick")


class PolygonTests(unittest.TestCase):
    def test_example_config_loads(self):
        from flasharb.config import load_config
        cfg = load_config("config.polygon.example.toml")
        self.assertEqual((cfg.chain_id, cfg.native_wrapped, cfg.ordering), (137, "WPOL", "fee"))
        self.assertEqual(cfg.dexes["quickswap_v3"].type, "algebra_v1")

    def test_bid_never_goes_below_the_network_minimum(self):
        cfg = make_config("live", tempfile.mkdtemp())
        cfg.ordering, cfg.priority_fee_share, cfg.min_priority_fee_gwei = "fee", 0.0, 30.0
        bot = FlashBot(cfg, FakeChain([pool("a", WETH, USDC, E18, E18)]), FakeExecutor(), RiskManager(cfg.risk),
                       Journal(cfg.log_dir))
        opp = type("Opp", (), {"net_usd": 1.0})()
        tip_wei, tip_usd = bot._priority_fee(opp, {WETH: 2000.0})
        self.assertEqual(tip_wei, 30 * GWEI)
        self.assertAlmostEqual(tip_usd, 30 * GWEI * cfg.gas_units_estimate / 1e18 * 2000.0)


class DryRunDiagnosisTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_paper_dry_run_drops_a_tax_token(self):
        from flasharb.simulator import Outcome
        from tests.test_flasharb import ARB
        tmp = tempfile.mkdtemp()
        cfg = make_config("paper", tmp)
        cfg.presimulate_live = True
        a = pool("a", WETH, ARB, 1000 * E18, 1_000_000 * E18, fee=500)
        b = pool("b", WETH, ARB, 1000 * E18, 1_050_000 * E18)
        priced = pool("usd", WETH, USDC, 1000 * E18, 2_000_000 * E6)   # prices WETH in dollars
        chain = FakeChain([a, b, priced])
        chain.discovered = {ARB}

        def verify(route, sizes, block):   # ARB never arrives in full: the next hop reverts with K
            return [Outcome(x, block, "sim", [route.amount_out(x)], failed_hop=1, reason="UniswapV2: K")
                    for x in sizes]
        chain.verify_route = verify
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp),
                       paper=PaperTrader(chain, cfg, background=False))
        bot.step()
        self.assertIn(ARB, bot.excluded)
        self.assertEqual(bot.stats.paper_orders, 0)
        self.assertIn("excluded transfer-tax tokens", bot.summary())


class ActivePoolDiscoveryTests(unittest.TestCase):
    def test_reads_every_block_and_shrinks_chunks_when_the_node_objects(self):
        from flasharb.discovery import scan_log_addresses
        asked = []

        def get_logs(lo, hi, topic):
            asked.append((lo, hi))
            if hi - lo + 1 > 25:
                raise RuntimeError("eth_getLogs: query returned more than 10000 results")
            return [{"address": f"0xPOOL{b % 3}"} for b in range(lo, hi + 1)]
        found, covered = scan_log_addresses(get_logs, "0xtopic", 1000, 1099, chunk=100)
        self.assertEqual(found, {"0xpool0", "0xpool1", "0xpool2"})
        self.assertEqual(covered, 100)
        read = sorted((lo, hi) for lo, hi in asked if hi - lo + 1 <= 25)
        self.assertEqual(sum(hi - lo + 1 for lo, hi in read), 100)   # every block exactly once

    def test_other_errors_are_raised(self):
        from flasharb.discovery import scan_log_addresses

        def get_logs(lo, hi, topic):
            raise RuntimeError("HTTP 403 forbidden")
        with self.assertRaises(RuntimeError):
            scan_log_addresses(get_logs, "0xtopic", 1, 10)


class BidBookTests(unittest.TestCase):
    def test_suggests_nothing_until_it_has_seen_enough(self):
        from flasharb.bidding import BidBook
        book = BidBook(percentile=0.75, margin=0.10, min_samples=3)
        book.add(1.0, 100)
        book.add(1.0, 200)
        self.assertIsNone(book.suggest(1.0))
        book.add(1.0, 300)
        self.assertEqual(book.suggest(1.0), int(300 * 1.1) + 1)

    def test_prefers_gaps_of_a_similar_size(self):
        from flasharb.bidding import BidBook
        book = BidBook(percentile=0.5, margin=0.0, min_samples=2)
        for tip in (10, 12, 11):
            book.add(0.2, tip)          # small gaps: small bids
        for tip in (5000, 6000):
            book.add(5.0, tip)          # big gaps: big bids
        self.assertLess(book.suggest(0.25), 100)
        self.assertGreater(book.suggest(4.0), 4000)

    def test_seeds_from_paper_csv(self):
        from flasharb.bidding import BidBook
        tmp = tempfile.mkdtemp()
        with open(f"{tmp}/paper_trades.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["est_profit_usd", "est_flash_fee_usd", "winner_tip_gwei"])
            w.writeheader()
            w.writerow({"est_profit_usd": "0.5", "est_flash_fee_usd": "0", "winner_tip_gwei": "1819.09"})
            w.writerow({"est_profit_usd": "0.5", "est_flash_fee_usd": "0", "winner_tip_gwei": ""})
        book = BidBook()
        self.assertEqual(book.load_paper_csvs(tmp), 1)
        self.assertEqual(book.typical(), int(1819.09 * 1e9))

    def test_learned_bid_is_cheaper_but_never_above_the_share(self):
        cfg = make_config("live", tempfile.mkdtemp())
        cfg.ordering, cfg.priority_fee_share, cfg.bid_strategy = "fee", 0.5, "learned"
        bot = FlashBot(cfg, FakeChain([pool("a", WETH, USDC, E18, E18)]), FakeExecutor(), RiskManager(cfg.risk),
                       Journal(cfg.log_dir))
        opp = type("Opp", (), {"net_usd": 2.0, "profit_usd": 2.1, "fee_usd": 0.0})()
        cfg.bid_strategy = "share"
        share_wei, share_usd = bot._priority_fee(opp, {WETH: 2000.0})
        cfg.bid_strategy = "learned"
        for _ in range(5):
            bot.bids.add(2.0, share_wei // 10)       # winners bid a tenth of our usual bid
        cheap_wei, cheap_usd = bot._priority_fee(opp, {WETH: 2000.0})
        self.assertLess(cheap_wei, share_wei // 5)
        self.assertLess(cheap_usd, share_usd)
        for _ in range(20):
            bot.bids.add(2.0, share_wei * 10)        # winners bid far more than we can afford
        capped_wei, _ = bot._priority_fee(opp, {WETH: 2000.0})
        self.assertEqual(capped_wei, share_wei)


class InFlightTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        from tests.test_flasharb import ARB
        self.tmp = tempfile.mkdtemp()
        self.usd_cheap = pool("uc", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.usd_dear = pool("ud", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        self.arb_cheap = pool("ac", WETH, ARB, 1000 * E18, 1_000_000 * E18, fee=500)
        self.arb_dear = pool("ad", WETH, ARB, 1000 * E18, 1_050_000 * E18)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def bot(self, max_inflight):
        cfg = make_config("paper", self.tmp)
        cfg.max_inflight, cfg.max_candidates_per_block = max_inflight, 6
        chain = FakeChain([self.usd_cheap, self.usd_dear, self.arb_cheap, self.arb_dear])
        chain.verify_route = verify_by_block()
        return FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(self.tmp),
                        paper=PaperTrader(chain, cfg, background=False))

    def test_one_at_a_time_by_default(self):
        bot = self.bot(1)
        bot.step()
        self.assertEqual(bot.stats.paper_orders, 1)

    def test_trades_through_separate_pools_go_together(self):
        bot = self.bot(3)
        bot.step()
        self.assertEqual(bot.stats.paper_orders, 2)   # the USDC gap and the ARB gap share no pool
        pools = [set(p.address for p in o.route.pools) for o, _, _ in bot._inflight.values()]
        self.assertFalse(pools[0] & pools[1])


class HeadFeedTests(unittest.TestCase):
    def test_announces_the_nodes_heads(self):
        clock = FakeClock()
        stream = subscribed_stream(clock)
        feed = HeadFeed(stream)
        self.assertFalse(feed.healthy())                 # no head yet
        self.assertIsNone(feed.wait_for_block_after(None, timeout=0))
        stream.handle_message(head(100))
        self.assertTrue(feed.healthy())
        self.assertEqual(feed.wait_for_block_after(99, timeout=0), 100)
        self.assertIsNone(feed.wait_for_block_after(100, timeout=0))
        clock.t += 0.05
        self.assertAlmostEqual(feed.age_ms(100), 50.0)
        self.assertIs(feed.express_lane_state(), False)
        clock.t += 30
        self.assertFalse(feed.healthy())                 # stale

    def test_bot_trusts_it_without_a_hash_check(self):
        stream = subscribed_stream(FakeClock())
        stream.handle_message(head(1))
        cfg = make_config("scan", tempfile.mkdtemp())
        bot = FlashBot(cfg, FakeChain([pool("a", WETH, USDC, E18, E18)]), None, RiskManager(cfg.risk),
                       Journal(cfg.log_dir), feed=HeadFeed(stream))
        self.assertTrue(bot._feed_usable())


class FeeOrderingTests(unittest.TestCase):
    """A Base-like chain: the sequencer puts the higher priority fee first."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.tmp = tempfile.mkdtemp()
        self.cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def cfg(self, mode, **settings):
        cfg = make_config(mode, self.tmp)
        cfg.ordering, cfg.priority_fee_share, cfg.extra_tx_cost_usd = "fee", 0.5, 0.01
        for key, value in settings.items():
            setattr(cfg, key, value)
        validate(cfg)
        return cfg

    def paper_bot(self, winner_tip_wei, **settings):
        cfg = self.cfg("paper", paper_send_latency_ms=20.0, paper_timeboost="off", **settings)
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = verify_by_block(gone_from=102)   # someone takes it inside block 102
        a, b = self.cheap.address, self.dear.address
        log = lambda addr: {"blockNumber": hex(102), "transactionIndex": "0x4",  # noqa: E731
                            "logIndex": "0x0", "transactionHash": "0xrival", "address": addr}
        chain.logs_for = lambda pools, lo, hi: [log(a), log(b)]
        base = 10 ** 7
        chain.receipt_raw = lambda h: {"to": "0xbot", "effectiveGasPrice": hex(base + winner_tip_wei)}
        chain.block_base_fee = lambda n: base
        chain.block_tx_count = lambda n: 10
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(self.tmp),
                       paper=PaperTrader(chain, cfg, background=False))
        bot._decision_latency = lambda: (10.0, "feed")
        return bot

    def rows(self):
        with open(f"{self.tmp}/paper_trades.csv") as fh:
            return list(csv.DictReader(fh))

    def test_outbidding_the_winner_fills(self):
        bot = self.paper_bot(winner_tip_wei=1)
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual(row["status"], "filled")
        self.assertIn("outbid the transaction that took it", row["reason"])
        self.assertGreater(float(row["our_tip_gwei"]), float(row["winner_tip_gwei"]))
        # the bid and the L1 fee are part of the cost
        self.assertGreater(float(row["gas_usd"]), 0.01)

    def test_a_higher_bid_wins_instead(self):
        bot = self.paper_bot(winner_tip_wei=10 ** 6 * GWEI)
        bot.step()
        bot.step()
        row = self.rows()[0]
        self.assertEqual((row["status"], row["cause"]), ("lost_race", "outbid in landing block"))
        self.assertEqual(row["winner_tip_gwei"], str(float(10 ** 6)))
        self.assertIn("outbid the other transaction in 0 of 1", bot.summary())

    def test_no_bid_on_arrival_ordered_chains(self):
        cfg = make_config("live", self.tmp)
        ex = FakeExecutor()
        FlashBot(cfg, FakeChain([self.cheap, self.dear]), ex, RiskManager(cfg.risk), Journal(self.tmp)).step()
        self.assertEqual(ex.tips, [0])

    def test_live_bids_a_share_of_the_expected_profit(self):
        cfg = self.cfg("live")
        ex = FakeExecutor()
        bot = FlashBot(cfg, FakeChain([self.cheap, self.dear]), ex, RiskManager(cfg.risk), Journal(self.tmp))
        bot.build_routes()
        opps = bot.find_opportunities(bot._prices(), bot.chain.gas_price_wei())
        tip_wei, tip_usd = bot._priority_fee(opps[0], bot._prices())
        self.assertAlmostEqual(tip_usd, 0.5 * (opps[0].net_usd - cfg.risk.min_profit_usd), places=6)
        bot.step()
        self.assertEqual(len(ex.sent), 1)
        self.assertGreater(ex.tips[0], 0)

    def test_config_checks(self):
        with self.assertRaises(ValueError):
            self.cfg("scan", ordering="gas")
        with self.assertRaises(ValueError):
            self.cfg("scan", priority_fee_share=1.5)


class FirstTakerTests(unittest.TestCase):
    def test_reports_the_winners_priority_fee(self):
        class Chain:
            def logs_for(self, pools, lo, hi):
                return [{"blockNumber": "0x5", "transactionIndex": "0x2", "logIndex": "0x0",
                         "transactionHash": "0xabc", "address": "0xp1"}]

            def receipt_raw(self, h):
                return {"effectiveGasPrice": hex(3 * GWEI), "from": "0xf", "to": "0xt"}

            def block_base_fee(self, n):
                return GWEI

        taker = first_taker(Chain(), ["0xp1", "0xp2"], 5, 5)
        self.assertEqual((taker["index"], taker["tip_wei"], taker["kind"]), (2, 2 * GWEI, "single-pool trade"))


if __name__ == "__main__":
    unittest.main()
