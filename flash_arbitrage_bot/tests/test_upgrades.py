"""Flash-loan fee, phantom filters, exact-check diagnosis, token exclusion, journals."""

import csv
import hashlib
import json
import logging
import re
import tempfile
import unittest
from pathlib import Path

from flasharb.amm import ROUTER_KINDS
from flasharb.bot import FlashBot
from flasharb.journal import Journal
from flasharb.risk import RiskManager
from flasharb.routes import Route, find_cycles, flash_fee, optimal_input_from
from flasharb.simulator import Outcome, diagnose, route_steps
from tests.test_flasharb import (ARB, E6, E18, USDC, WETH, FakeChain, exact_checker, hop_amounts,
                                 make_config, pool)

ROOT = Path(__file__).resolve().parent.parent
SYMBOLS = {WETH: "WETH", USDC: "USDC", ARB: "ARB"}


def bot_for(chain, mode="scan", **risk):
    tmp = tempfile.mkdtemp()
    cfg = make_config(mode, tmp, **risk)
    return FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp)), tmp


class FlashFeeTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def route(self):
        # Sell WETH where it's dear (2050 USDC), buy it back where it's cheap (2000).
        return next(r for r in find_cycles([self.cheap, self.dear], [WETH], 2) if r.pools[0] is self.dear)

    def test_sizing_maximises_profit_after_fee(self):
        route, fee = self.route(), 0.002
        x = optimal_input_from(route.mobius(), 10 ** 40, fee)
        profit = lambda a: route.amount_out(a) - a - flash_fee(a, fee)  # noqa: E731
        self.assertGreater(profit(x), 0)
        self.assertGreaterEqual(profit(x), profit(int(x * 0.95)))
        self.assertGreaterEqual(profit(x), profit(int(x * 1.05)))
        self.assertLess(x, optimal_input_from(route.mobius(), 10 ** 40))  # a fee means a smaller loan
        self.assertIsNone(optimal_input_from(route.mobius(), 10 ** 40, 0.05))  # fee bigger than the gap

    def test_fee_rounds_up(self):
        self.assertEqual(flash_fee(10, 0.05), 1)
        self.assertEqual(flash_fee(10 ** 18, 0), 0)

    def test_net_and_summary_include_flash_fee(self):
        chain = FakeChain([self.cheap, self.dear])
        chain.flash_fee_rate = 0.0005
        bot, tmp = bot_for(chain)
        bot.step()
        self.assertGreater(bot.stats.candidates, 0)
        with open(f"{tmp}/opportunities.csv") as fh:
            row = next(csv.DictReader(fh))
        fee, profit, gas, net = (float(row[k]) for k in ("flash_fee_usd", "profit_usd", "gas_usd", "net_usd"))
        self.assertGreater(fee, 0)
        self.assertAlmostEqual(net, profit - fee - gas, places=3)
        summary = bot.summary()
        self.assertIn("flash fee $", summary)
        self.assertIn("(5.0 bps)", summary)

    def test_gap_smaller_than_fee_is_not_a_candidate(self):
        chain = FakeChain([self.cheap, self.dear])
        chain.flash_fee_rate = 0.03  # 3% > the ~2.1% gap after pool fees
        bot, _ = bot_for(chain)
        bot.step()
        self.assertEqual(bot.stats.candidates, 0)


class PhantomTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_one_sided_pool_is_not_liquid(self):
        deep = pool("deep", WETH, USDC, 1000 * E18, 2_000_000 * E6)
        # Concentrated pool at a 50% higher price whose WETH side is almost empty:
        # $300k of USDC but only $100 of WETH. Plenty in total, useless for the gap.
        lopsided = pool("lop", WETH, USDC, 1000 * E18, 3_000_000 * E6, fee=500, kind="v3")
        lopsided.balance0, lopsided.balance1 = E18 // 20, 300_000 * E6
        bot, _ = bot_for(FakeChain([deep, lopsided]))
        prices = bot._prices()
        self.assertNotIn(lopsided.address, bot._liquid_pools(prices))
        bot.cfg.risk.min_pool_reserve_usd = 0  # the old total-only rule let it through
        self.assertIn(lopsided.address, bot._liquid_pools(prices))

    def test_best_trade_is_ranked_by_dollars_not_percent(self):
        # Route A: 10% apart but tiny pools. Route B: 2.5% apart, deep pools.
        tiny1 = pool("t1", WETH, ARB, 5 * E18, 10_000 * E18, fee=500)
        tiny2 = pool("t2", WETH, ARB, 5 * E18, 11_000 * E18, fee=500)
        deep1 = pool("d1", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        deep2 = pool("d2", WETH, USDC, 1000 * E18, 2_050_000 * E6, fee=500)
        ref = pool("ref", ARB, USDC, 10 ** 6 * E18, 10 ** 6 * E6)  # prices ARB
        chain = FakeChain([tiny1, tiny2, deep1, deep2, ref])
        bot, _ = bot_for(chain, min_pool_liquidity_usd=1000, min_pool_reserve_usd=0)
        bot.step()
        self.assertGreater(bot.stats.best_edge_pct, 9)                 # the % leader is the tiny route
        self.assertNotIn("t1", bot.stats.best_net_route.replace("WETH", ""))
        self.assertIn("[d", bot.stats.best_net_route)                   # the $ leader is the deep route
        self.assertIn("marginal edge +2.", bot.summary())
        self.assertNotIn("closest miss", bot.summary())


def outcome(route, amount, scale_hop=None, scale=1.0, reported=None, method="sim"):
    hops = []
    x = amount
    for i, (p, t_in, _) in enumerate(route.hops()):
        x = p.amount_out(t_in, x)
        if i == scale_hop:
            x = x * round(scale * 10 ** 6) // 10 ** 6
        hops.append(x)
    return Outcome(amount, 7, method, hops, reported or [0] * len(hops))


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        self.v2 = pool("v2", WETH, ARB, 100 * E18, 100_000 * E18)
        self.v3 = pool("v3", WETH, ARB, 100 * E18, 110_000 * E18, fee=500, kind="v3")
        self.alg = pool("alg", WETH, ARB, 100 * E18, 110_000 * E18, fee=500, kind="algebra")
        self.core = {WETH, USDC}

    def test_v2_shortfall_is_a_transfer_tax(self):
        route = Route((self.v3, self.v2), (WETH, ARB, WETH))
        full = outcome(route, E18, scale_hop=1, scale=0.95)
        d = diagnose(route, full, outcome(route, E18 // 1000), SYMBOLS, self.core)
        self.assertEqual((d.cause, d.hop, d.taxed_token), ("transfer tax", 1, ARB))
        self.assertAlmostEqual(d.shortfall, 0.05, places=3)

    def test_router_sent_more_than_arrived_is_a_tax_on_that_token(self):
        route = Route((self.v2, self.v3), (WETH, ARB, WETH))
        real = outcome(route, E18)
        sent = real.received[0]
        real.received[0] = int(sent * 0.97)
        real.reported = [sent, 0]
        d = diagnose(route, real, None, SYMBOLS, self.core)
        self.assertEqual((d.cause, d.hop, d.taxed_token), ("transfer tax", 0, ARB))

    def test_concentrated_hop_fine_when_tiny_is_depth(self):
        route = Route((self.v3, self.v2), (WETH, ARB, WETH))
        full = outcome(route, 10 * E18, scale_hop=0, scale=0.9)
        tiny = outcome(route, 10 * E18 // 1000)
        d = diagnose(route, full, tiny, SYMBOLS, self.core)
        self.assertEqual((d.cause, d.hop, d.taxed_token), ("depth", 0, None))

    def test_algebra_hop_off_even_when_tiny_is_fee(self):
        route = Route((self.alg, self.v2), (WETH, ARB, WETH))
        full = outcome(route, E18, scale_hop=0, scale=0.99)
        tiny = outcome(route, E18 // 1000, scale_hop=0, scale=0.99)
        d = diagnose(route, full, tiny, SYMBOLS, self.core)
        self.assertEqual((d.cause, d.hop), ("dynamic fee/price", 0))

    def test_k_revert_blames_the_input_token(self):
        route = Route((self.v3, self.v2), (WETH, ARB, WETH))
        failed = Outcome(E18, 7, "sim", failed_hop=1, reason="UniswapV2: K")
        d = diagnose(route, failed, None, SYMBOLS, self.core)
        self.assertEqual((d.cause, d.hop, d.taxed_token), ("transfer tax", 1, ARB))
        other = diagnose(route, Outcome(E18, 7, "sim", failed_hop=0, reason="SPL"), None, SYMBOLS, self.core)
        self.assertEqual(other.cause, "swap reverted")
        loan = diagnose(route, Outcome(E18, 7, "sim", failed_hop=-1, reason="BAL#528"), None, SYMBOLS, self.core)
        self.assertEqual(loan.cause, "flash loan")

    def test_everything_matching_means_small_deviations(self):
        route = Route((self.v3, self.v2), (WETH, ARB, WETH))
        d = diagnose(route, outcome(route, E18), None, SYMBOLS, self.core)
        self.assertEqual(d.cause, "small deviations")


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_taxed_long_tail_token_is_dropped_and_remembered(self):
        a = pool("a", ARB, USDC, 1_000_000 * E18, 1_000_000 * E6, fee=500)
        b = pool("b", ARB, USDC, 1_000_000 * E18, 1_030_000 * E6, fee=500)
        chain = FakeChain([a, b])
        chain.discovered = {ARB}
        chain.verify_route = exact_checker(scale=0.9)  # final V2 hop 10% short: a transfer tax
        bot, tmp = bot_for(chain)
        bot.cfg.flash_tokens = ["USDC"]
        bot.flash_tokens = [USDC]
        bot.step()
        self.assertIn(ARB, bot.excluded)
        self.assertEqual(bot.routes, [])
        self.assertIn("excluded transfer-tax tokens: ARB", bot.summary())
        self.assertIn("transfer tax @", bot.summary())
        with open(f"{tmp}/checks.csv") as fh:
            check = next(csv.DictReader(fh))
        self.assertEqual((check["result"], check["cause"]), ("model error", "transfer tax"))
        saved = json.loads(Path(tmp, "excluded_tokens.json").read_text())
        self.assertIn(ARB, saved)
        again = FlashBot(bot.cfg, chain, None, RiskManager(bot.cfg.risk), Journal(tmp))
        self.assertIn(ARB, again.excluded)  # remembered across restarts

    def test_core_tokens_are_never_excluded(self):
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        chain = FakeChain([cheap, dear])
        chain.verify_route = exact_checker(scale=0.9)
        bot, _ = bot_for(chain)
        bot.step()
        self.assertEqual(bot.excluded, {})
        self.assertGreater(bot.stats.quoter_rejected, 0)


class JournalTests(unittest.TestCase):
    def test_old_file_with_other_columns_is_set_aside(self):
        tmp = tempfile.mkdtemp()
        old = Path(tmp, "gaps.csv")
        old.write_text("time,route,first_block,last_block,blocks_open,net_usd\nx,r,1,1,1,1.0\n")
        Journal(tmp).gap(route="r", first_block=5, last_block=6, closed_by_block=7, blocks_open=2, net_usd=1,
                         pools="0xa 0xb")
        with old.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["closed_by_block"], "7")
        self.assertEqual(len(list(Path(tmp).glob("gaps.*.csv"))), 1)  # the old file, renamed


class SimulatorCodeTests(unittest.TestCase):
    def test_bytecode_matches_source(self):
        from flasharb.simulator_code import RUNTIME_HEX, SOURCE_SHA256
        source = (ROOT / "contracts" / "RouteSimulator.sol").read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(), SOURCE_SHA256,
                         "RouteSimulator.sol changed: run `python run_flash.py build-simulator`")
        self.assertTrue(re.fullmatch(r"[0-9a-f]+", RUNTIME_HEX))

    def test_step_kinds_match_both_contracts(self):
        for name in ("RouteSimulator.sol", "FlashArbitrage.sol"):
            sol = (ROOT / "contracts" / name).read_text()
            kinds = {int(v) for v in re.findall(r"uint8 internal constant KIND_\w+ = (\d+);", sol)}
            self.assertEqual(kinds, set(ROUTER_KINDS.values()), name)

    def test_route_steps(self):
        v3 = pool("v3", WETH, USDC, 1, 1, fee=500, kind="v3")
        v2 = pool("v2", WETH, USDC, 1, 1)
        steps = route_steps(Route((v3, v2), (WETH, USDC, WETH)))
        self.assertEqual(steps[0], (ROUTER_KINDS["v3_router02"], v3.router, WETH, USDC, 500))
        self.assertEqual(steps[1][4], 0)  # fee only matters to V3 routers


class CycleKeyTests(unittest.TestCase):
    def test_rotations_share_a_key_directions_do_not(self):
        from flasharb.routes import cycle_key
        ab = pool("ab", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        bc = pool("bc", USDC, ARB, 1_000_000 * E6, 1_000_000 * E18, fee=500)
        ca = pool("ca", ARB, WETH, 1_000_000 * E18, 500 * E18, fee=500)
        routes = find_cycles([ab, bc, ca], [WETH, USDC, ARB], 3)
        self.assertEqual(len(routes), 6)                    # 3 rotations x 2 directions
        self.assertEqual(len({cycle_key(r) for r in routes}), 2)
        two = find_cycles([ab, pool("ab2", WETH, USDC, 1, 1)], [WETH], 2)
        self.assertNotEqual(cycle_key(two[0]), cycle_key(two[1]))  # A->B->A both ways


class HopAmountsTests(unittest.TestCase):
    def test_exact_checker_reproduces_the_model(self):
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        route = find_cycles([cheap, dear], [WETH], 2)[0]
        out = exact_checker()(route, [E18], 1)[0]
        self.assertEqual(out.out, route.amount_out(E18))
        self.assertEqual(out.received, hop_amounts(route, E18))


if __name__ == "__main__":
    unittest.main()
