import csv
import logging
import tempfile
import unittest

from flasharb.amm import Q96, Pool
from flasharb.bot import FlashBot
from flasharb.config import Config, DexConfig, RiskLimits, validate
from flasharb.executor import SendResult, SimResult
from flasharb.journal import Journal
from flasharb.risk import RiskManager
from flasharb.routes import Route, find_cycles, optimal_input, usd_prices
from flasharb.simulator import Outcome

WETH = "0x" + "1" * 40
USDC = "0x" + "2" * 40
ARB = "0x" + "3" * 40
DEC = {WETH: 18, USDC: 6, ARB: 18}
E18, E6 = 10 ** 18, 10 ** 6


def pool(name, t0, t1, r0, r1, fee=3000, kind="v2"):
    p = Pool(address="0x" + name.encode().hex().ljust(40, "0")[:40], dex=name, kind=kind,
             token0=t0, token1=t1, fee_ppm=fee, router="0x" + "9" * 40,
             router_kind="v2" if kind == "v2" else "v3_router02")
    p.update_v2(r0, r1)
    return p


class AmmTests(unittest.TestCase):
    def test_v2_matches_uniswap_formula(self):
        p = pool("a", WETH, USDC, 100 * E18, 200_000 * E6)
        amount_in = E18
        expected = amount_in * 997 * 200_000 * E6 // (100 * E18 * 1000 + amount_in * 997)
        self.assertEqual(p.amount_out(WETH, amount_in), expected)

    def test_v3_virtual_reserves_reproduce_price(self):
        p = pool("v3", WETH, USDC, 0, 0, fee=500, kind="v3")
        price = 2000 * E6 / E18  # raw USDC units per raw WETH unit
        sqrt_price_x96 = int(price ** 0.5 * Q96)
        p.update_v3(sqrt_price_x96, 10 ** 15)
        self.assertAlmostEqual(p.reserve1 / p.reserve0 / price, 1.0, places=6)
        p.update_v3(sqrt_price_x96, 0)
        self.assertFalse(p.active)


class CamelotTests(unittest.TestCase):
    def camelot(self, fee0, fee1):
        p = pool("camelot", WETH, USDC, 1000 * E18, 2_050_000 * E6, fee=fee0, kind="camelot_v2")
        p.router_kind, p.fee1_ppm = "camelot_v2", fee1
        return p

    def test_directional_fees(self):
        p = self.camelot(3000, 1000)  # selling WETH costs 0.3%, selling USDC 0.1%
        plain = pool("plain", WETH, USDC, 1000 * E18, 2_050_000 * E6, fee=3000)
        self.assertEqual(p.amount_out(WETH, E18), plain.amount_out(WETH, E18))
        self.assertGreater(p.amount_out(USDC, 2000 * E6), plain.amount_out(USDC, 2000 * E6))
        self.assertEqual(p.label, "camelot")  # dynamic fee: no tier in the label

    def test_mobius_and_prefilter_respect_directional_fees(self):
        uni = pool("uni", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        cam = self.camelot(3000, 1000)
        for route in find_cycles([uni, cam], [WETH, USDC], 2):
            a, b, c = route.mobius()
            x = E18 if route.start == WETH else 2000 * E6
            self.assertAlmostEqual(a * x / (b + c * x) / route.amount_out(x), 1.0, places=6)

    def test_bot_finds_gap_between_uniswap_and_camelot(self):
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        uni = pool("uni", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        bot = FlashBot(cfg, FakeChain([uni, self.camelot(3000, 1000)]), None,
                       RiskManager(cfg.risk), Journal(tmp))
        bot.step()
        self.assertGreater(bot.stats.candidates, 0)
        with open(f"{tmp}/opportunities.csv") as fh:
            self.assertIn("[camelot]", fh.read())

    def test_algebra_pools_are_concentrated(self):
        p = pool("alg", WETH, USDC, 0, 0, kind="algebra")
        self.assertTrue(p.concentrated)
        self.assertFalse(pool("v2", WETH, USDC, 1, 1).concentrated)

    def test_router_kinds_match_contract(self):
        import re
        from pathlib import Path
        from flasharb.amm import ROUTER_KINDS
        sol = (Path(__file__).resolve().parent.parent / "contracts" / "FlashArbitrage.sol").read_text()
        contract = {int(v) for v in re.findall(r"uint8 internal constant KIND_\w+ = (\d+);", sol)}
        self.assertEqual(contract, set(ROUTER_KINDS.values()))


class DepthTests(unittest.TestCase):
    """V3 virtual reserves can claim far more depth than a pool really has."""

    def thin_v3(self):
        # Price says 1 WETH = 3000 USDC (vs 2000 elsewhere) with big virtual
        # reserves, but the pool actually holds only ~$50.
        p = pool("thin", WETH, USDC, 1000 * E18, 3_000_000 * E6, fee=10000, kind="v3")
        p.balance0, p.balance1 = E18 // 100, 30 * E6
        return p

    def test_depth_and_output_capped_by_real_balances(self):
        p = self.thin_v3()
        self.assertEqual(p.depth(), (E18 // 100, 30 * E6))
        self.assertEqual(p.amount_out(WETH, 10 * E18), 30 * E6)  # can't pay out more than it holds

    def test_thin_pool_does_not_set_prices(self):
        deep = pool("deep", WETH, USDC, 1000 * E18, 2_000_000 * E6)
        prices = usd_prices([self.thin_v3(), deep], DEC, {USDC})
        self.assertAlmostEqual(prices[WETH], 2000, places=6)

    def test_fake_gap_from_thin_pool_is_filtered(self):
        deep = pool("deep", WETH, USDC, 1000 * E18, 2_000_000 * E6)
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        bot = FlashBot(cfg, FakeChain([deep, self.thin_v3()]), None, RiskManager(cfg.risk), Journal(tmp))
        bot.step()
        self.assertEqual(bot.routes, [])  # thin pool is below min_pool_liquidity_usd
        self.assertEqual(bot.stats.candidates, 0)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.ab1 = pool("ab1", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.ab2 = pool("ab2", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        self.bc = pool("bc", USDC, ARB, 1_000_000 * E6, 1_000_000 * E18)
        self.ca = pool("ca", ARB, WETH, 1_000_000 * E18, 500 * E18)

    def test_cycle_enumeration(self):
        pools = [self.ab1, self.ab2, self.bc, self.ca]
        self.assertEqual(len(find_cycles(pools, [WETH], 2)), 2)  # both directions of ab1/ab2
        self.assertEqual(len(find_cycles(pools, [WETH], 3)), 6)  # + 4 triangles

    def test_mobius_matches_exact_chain(self):
        for route in find_cycles([self.ab1, self.ab2, self.bc, self.ca], [WETH], 3):
            a, b, c = route.mobius()
            x = 3 * E18
            self.assertAlmostEqual(a * x / (b + c * x) / route.amount_out(x), 1.0, places=6)

    def test_optimal_input_maximises_profit(self):
        route = next(r for r in find_cycles([self.ab1, self.ab2], [WETH], 2) if r.pools[0] is self.ab2)
        x = optimal_input(route, 10 ** 30)
        profit = lambda amt: route.amount_out(amt) - amt
        self.assertGreater(profit(x), 0)
        self.assertGreaterEqual(profit(x), profit(int(x * 0.9)))
        self.assertGreaterEqual(profit(x), profit(int(x * 1.1)))
        self.assertEqual(optimal_input(route, E18), E18)  # capped by max input

    def test_no_profit_when_prices_equal(self):
        twin = pool("twin", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        for route in find_cycles([self.ab1, twin], [WETH], 2):
            self.assertIsNone(optimal_input(route, 10 ** 30))

    def test_usd_prices_via_deepest_pool(self):
        shallow = pool("shallow", WETH, USDC, 1 * E18, 5_000 * E6)  # wrong price, tiny depth
        prices = usd_prices([self.ab1, shallow, self.ca], DEC, {USDC})
        self.assertAlmostEqual(prices[WETH], 2000, places=6)
        self.assertAlmostEqual(prices[ARB], 1.0, places=6)  # 500 WETH / 1M ARB at $2000


def hop_amounts(route, amount):
    out, x = [], amount
    for p, t_in, _ in route.hops():
        x = p.amount_out(t_in, x)
        out.append(x)
    return out


def exact_checker(scale=1.0, method="sim", calls=None):
    """Stand-in for Chain.verify_route: the model's own per-hop amounts, with the
    last hop scaled (scale < 1 = reality came up short of the estimate)."""
    def verify(route, sizes, block):
        if calls is not None:
            calls.append(list(sizes))
        outcomes = []
        for amount in sizes:
            hops = hop_amounts(route, amount)
            hops[-1] = hops[-1] * round(scale * 10 ** 6) // 10 ** 6
            outcomes.append(Outcome(amount, block, method, hops, [0] * len(hops)))
        return outcomes
    return verify


class FakeChain:
    def __init__(self, pools, vault=None, gas_price=10 ** 7):
        self.pools = pools
        self.decimals = DEC
        self.vault_balances = vault if vault is not None else {WETH: 10_000 * E18, USDC: 10 ** 8 * E6}
        self.block = 100
        self.gas_price = gas_price
        self.last_gas_price_wei = gas_price

    def refresh(self, block=None):
        self.block = block if block is not None else self.block + 1
        return self.block

    def gas_price_wei(self):
        self.last_gas_price_wei = self.gas_price
        return self.gas_price


class FakeExecutor:
    def __init__(self, max_ok_amount=None, send_success=True):
        self.max_ok_amount = max_ok_amount
        self.send_success = send_success
        self.simulated, self.sent = [], []

        self._done = []

    def simulate(self, route, amount, min_profit):
        self.simulated.append(amount)
        if self.max_ok_amount is not None and amount > self.max_ok_amount:
            return SimResult(False, 0, "Unprofitable")
        return SimResult(True, 400_000)

    def submit(self, route, amount, min_profit):
        self.sent.append(amount)
        tx_hash = f"0x{len(self.sent):064x}"
        gas = 400_000 * 10 ** 7
        self._done.append(SendResult(True, tx_hash, route.amount_out(amount) - amount, gas, "", 1, False)
                          if self.send_success else SendResult(False, tx_hash, 0, gas, "reverted on-chain"))
        return tx_hash

    def poll_results(self):
        done, self._done = self._done, []
        return done


def make_config(mode, tmp, **risk):
    cfg = Config(
        mode=mode, chain_name="test", chain_id=1, balancer_vault="0x" + "b" * 40,
        native_wrapped="WETH", tokens={"WETH": WETH, "USDC": USDC, "ARB": ARB},
        stable_tokens=["USDC"], flash_tokens=["WETH", "USDC"],
        dexes={"d": DexConfig("d", "v2", "0x" + "c" * 40, "0x" + "9" * 40, "v2")},
        max_hops=2, log_dir=tmp, risk=RiskLimits(**risk))
    validate(cfg)
    return cfg


class BotTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.tmp = tempfile.mkdtemp()
        self.cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        self.dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def bot(self, mode, executor=None, pools=None, chain=None, **risk):
        chain = chain or FakeChain(pools or [self.cheap, self.dear])
        cfg = make_config(mode, self.tmp, **risk)
        return FlashBot(cfg, chain, executor, RiskManager(cfg.risk), Journal(self.tmp))

    def rows(self, name):
        with open(f"{self.tmp}/{name}") as fh:
            return list(csv.DictReader(fh))

    def test_scan_finds_gap_and_logs_it_once(self):
        bot = self.bot("scan")
        for _ in range(3):
            bot.step()
        self.assertGreater(bot.stats.candidates, 0)
        rows = self.rows("opportunities.csv")
        self.assertEqual(len({r["route"] for r in rows}), len(rows))  # no repeats
        self.assertTrue(all(float(r["net_usd"]) >= 0.5 for r in rows))

    def test_scan_verifies_candidates_with_quoter(self):
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = exact_checker()
        bot = self.bot("scan", chain=chain)
        bot.step()
        rows = self.rows("opportunities.csv")
        self.assertTrue(rows and all(r["decision"].startswith("verified (sim") for r in rows))
        self.assertGreater(bot.stats.quoter_verified, 0)
        self.assertGreater(bot.stats.quoter_verified_net_usd, 0)
        self.assertIn("exact checks (at the estimate's own block): verified=", bot.summary())
        checks = self.rows("checks.csv")
        self.assertEqual({r["result"] for r in checks}, {"verified"})
        self.assertEqual(checks[0]["est_block"], checks[0]["check_block"])
        self.assertIn("/hr if the bot had won every one", bot.summary())

    def test_scan_rejects_candidates_the_quoter_disagrees_with(self):
        chain = FakeChain([self.cheap, self.dear])
        chain.verify_route = exact_checker(scale=0.97)  # reality: last hop 3% short
        bot = self.bot("scan", chain=chain)
        bot.step()
        rows = self.rows("opportunities.csv")
        self.assertTrue(rows and all(r["decision"].startswith("rejected") for r in rows))
        self.assertEqual(bot.stats.quoter_verified, 0)
        self.assertIn("none real this period", bot.summary())

    def test_no_opportunity_when_fees_exceed_gap(self):
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_004_000 * E6)  # 0.2% gap < 0.35% fees
        bot = self.bot("scan", pools=[self.cheap, dear])
        bot.step()
        self.assertEqual(bot.stats.candidates, 0)
        # The closest miss is still reported: about 0.2% gap minus 0.35% fees.
        self.assertAlmostEqual(bot.stats.best_edge_pct, -0.15, delta=0.01)
        self.assertIn("no route had a positive edge", bot.summary())

    def test_summary_reports_best_trade_below_threshold(self):
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_008_000 * E6)  # small positive edge
        bot = self.bot("scan", pools=[self.cheap, dear], min_profit_usd=10_000)
        bot.step()
        self.assertEqual(bot.stats.candidates, 0)
        self.assertGreater(bot.stats.best_net_usd, 0)
        self.assertIn("needs >= $10000.00", bot.summary())
        self.assertIn("profit $", bot.summary())
        self.assertNotIn("capped", bot.summary())

    def test_summary_says_when_trade_size_is_capped_by_vault(self):
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        chain = FakeChain([self.cheap, dear], vault={WETH: E18 // 10, USDC: 100 * E6})
        bot = self.bot("scan", chain=chain)
        bot.step()
        self.assertIn("capped by Balancer's balance", bot.summary())
        bot.stats.reset_window()
        self.assertIsNone(bot.stats.best_edge_pct)

    def test_thin_pools_ignored(self):
        bot = self.bot("scan", min_pool_liquidity_usd=10 ** 9)
        bot.step()
        self.assertEqual(bot.routes, [])

    def test_gas_price_cap_skips_block(self):
        bot = self.bot("scan", chain=FakeChain([self.cheap, self.dear], gas_price=5 * 10 ** 9))
        bot.step()
        self.assertEqual(bot.stats.candidates, 0)

    def test_loan_capped_by_vault_balance(self):
        chain = FakeChain([self.cheap, self.dear], vault={WETH: E18 // 100, USDC: 10 * E6})
        bot = self.bot("scan", chain=chain)
        bot.step()
        self.assertEqual(bot.stats.candidates, 0)  # tiny loans can't clear min profit

    def test_simulate_mode_never_sends(self):
        ex = FakeExecutor()
        bot = self.bot("simulate", executor=ex)
        bot.step()
        self.assertEqual(len(ex.simulated), 1)
        self.assertEqual(ex.sent, [])
        self.assertGreater(bot.stats.simulated_net_usd, 0)

    def test_simulate_counts_a_persisting_gap_once(self):
        ex = FakeExecutor()
        bot = self.bot("simulate", executor=ex)
        bot.cfg.max_candidates_per_block = 1
        bot.step()
        once = bot.stats.simulated_net_usd
        for _ in range(4):
            bot.step()
        self.assertEqual(len(ex.simulated), 1)
        self.assertEqual(bot.stats.simulated_net_usd, once)

    def test_simulation_retries_smaller_size(self):
        bot = self.bot("simulate", executor=FakeExecutor())
        bot.step()
        first = bot.executor.simulated[0]
        ex = FakeExecutor(max_ok_amount=first // 2)
        bot = self.bot("simulate", executor=ex)
        bot.step()
        self.assertEqual(ex.simulated, [first, first // 2])
        self.assertEqual(bot.stats.sim_passed, 1)

    def test_failed_simulation_puts_route_on_cooldown(self):
        ex = FakeExecutor(max_ok_amount=0)  # every simulation fails
        bot = self.bot("simulate", executor=ex)
        bot.cfg.max_candidates_per_block = 1
        bot.cfg.sim_cooldown_blocks = 5
        for _ in range(6):
            bot.step()
        # 1st block: 3 attempts (full, 1/2, 1/4); blocks 2-6 on cooldown.
        self.assertEqual(len(ex.simulated), 3)
        bot.step()  # cooldown over
        self.assertEqual(len(ex.simulated), 6)

    def test_live_success_records_profit(self):
        ex = FakeExecutor()
        bot = self.bot("live", executor=ex)
        bot.step()
        self.assertEqual(len(ex.sent), 1)
        self.assertEqual(bot.stats.succeeded, 1)
        self.assertGreater(bot.stats.realized_net_usd, 0)
        self.assertEqual(self.rows("trades.csv")[0]["success"], "True")

    def test_live_halts_after_consecutive_reverts(self):
        ex = FakeExecutor(send_success=False)
        bot = self.bot("live", executor=ex, max_consecutive_reverts=3)
        bot.run(max_blocks=10)
        self.assertEqual(len(ex.sent), 3)
        self.assertIsNotNone(bot.risk.halted_reason)
        self.assertLess(bot.stats.realized_net_usd, 0)  # only gas lost


class ErrorHandlingTests(unittest.TestCase):
    def test_api_keys_are_masked(self):
        from flasharb.errors import describe, mask_secrets
        text = "403 Forbidden for url: https://arb-mainnet.g.alchemy.com/v2/alch_SECRET123456"
        self.assertNotIn("SECRET123456", mask_secrets(text))
        self.assertIn("/v2/alch...", mask_secrets(text))
        self.assertNotIn("SECRET", describe(RuntimeError(text)))

    def test_bot_backs_off_on_rpc_errors_instead_of_crashing(self):
        from unittest import mock
        import flasharb.bot as botmod

        class FlakyChain(FakeChain):
            failures = 2

            def refresh(self):
                if self.failures:
                    self.failures -= 1
                    exc = RuntimeError("403 Forbidden for url: https://x.io/v2/alch_SECRETKEY")
                    exc.response = type("R", (), {"status_code": 403})()
                    raise exc
                return super().refresh()

        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        bot = FlashBot(cfg, FlakyChain([cheap]), None, RiskManager(cfg.risk), Journal(tmp))
        sleeps = []
        with mock.patch.object(botmod.time, "sleep", sleeps.append), \
                self.assertLogs("flasharb.bot", level="WARNING") as logs:
            bot.run(max_blocks=1)
        self.assertEqual(bot.stats.blocks, 1)
        self.assertEqual(sleeps[:2], [2.0, 4.0])  # exponential backoff
        self.assertFalse(any("SECRETKEY" in line for line in logs.output))
        self.assertTrue(any("403" in line for line in logs.output))


class TrackingTests(unittest.TestCase):
    def test_only_route_pools_are_tracked_and_skips_are_counted(self):
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        lonely = pool("lonely", ARB, USDC, 10 ** 6 * E18, 10 ** 6 * E6)  # on no 2-hop route
        chain = FakeChain([cheap, dear, lonely])
        chain.tracked = None
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp))
        bot.step()
        self.assertEqual(chain.tracked, {cheap.address, dear.address})
        chain.block += 4  # the chain moved on 4 blocks while we were busy
        bot.step()
        self.assertEqual(bot.stats.skipped_blocks, 4)
        self.assertIn("saw 33% of chain blocks", bot.summary())


class GapLifetimeTests(unittest.TestCase):
    def test_records_how_long_a_verified_gap_stays_open(self):
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        cheap = pool("cheap", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_050_000 * E6)
        chain = FakeChain([cheap, dear])
        calls = []
        chain.verify_route = exact_checker(calls=calls)
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp))
        for _ in range(3):
            bot.step()                      # gap open for 3 blocks
        self.assertEqual(len(calls), 1)     # checked once (all sizes at once), not again each block
        self.assertEqual(len(calls[0]), 4)  # full, 1/4, 1/16 and a tiny diagnostic size
        dear.update_v2(1000 * E18, 2_000_000 * E6)   # someone closes it
        bot.step()
        with open(f"{tmp}/gaps.csv") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["blocks_open"], "3")
        self.assertEqual(rows[0]["closed_by_block"], str(chain.block))
        self.assertEqual(set(rows[0]["pools"].split()), {cheap.address, dear.address})
        self.assertEqual(bot.stats.gap_lifetimes, [3])
        self.assertIn("verified gaps closed: 1", bot.summary())
        self.assertIn("median 3 blocks", bot.summary())


class RotationTests(unittest.TestCase):
    def test_rotations_of_one_triangle_count_once(self):
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        cfg.max_hops = 3
        cfg.flash_tokens = ["WETH", "USDC", "ARB"]
        ab = pool("ab", WETH, USDC, 1000 * E18, 2_000_000 * E6, fee=500)
        bc = pool("bc", USDC, ARB, 1_000_000 * E6, 1_000_000 * E18, fee=500)
        ca = pool("ca", ARB, WETH, 1_000_000 * E18, 530 * E18, fee=500)  # ARB overpriced here
        chain = FakeChain([ab, bc, ca], vault={WETH: 10 ** 30, USDC: 10 ** 30, ARB: 10 ** 30})
        chain.verify_route = exact_checker()
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp))
        bot.step()
        self.assertEqual(bot.stats.quoter_verified, 1)
        with open(f"{tmp}/opportunities.csv") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)


class DiscoveryTests(unittest.TestCase):
    NEW, TINY, SCAM = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40

    def test_selects_liquid_tokens_by_priced_side_only(self):
        from flasharb.discovery import select_tokens
        prices = {USDC: 1.0, WETH: 2000.0}
        records = [
            (self.NEW, USDC, 10 ** 24, 50_000 * E6),       # $100k liquidity: kept
            (WETH, self.TINY, 2 * E18, 10 ** 24),          # $8k: below threshold
            (self.SCAM, ARB, 10 ** 40, 10 ** 30),          # ARB unpriced: can't value, skipped
        ]
        chosen = select_tokens(records, prices, DEC, 20_000, 10, exclude={WETH, USDC})
        self.assertEqual(chosen, [(self.NEW, 100_000.0)])

    def test_max_tokens_keeps_deepest(self):
        from flasharb.discovery import select_tokens
        records = [(self.NEW, USDC, 1, 30_000 * E6), (self.TINY, USDC, 1, 90_000 * E6)]
        chosen = select_tokens(records, {USDC: 1.0}, DEC, 20_000, 1, exclude=set())
        self.assertEqual([t for t, _ in chosen], [self.TINY])

    def test_unique_names(self):
        from flasharb.discovery import unique_name
        taken = {"USDC", "PEPE"}
        self.assertEqual(unique_name("PEPE", "0xabcd1234" + "0" * 32, taken), "PEPE_abcd")
        self.assertEqual(unique_name("NEW", "0x" + "1" * 40, taken), "NEW")
        self.assertEqual(unique_name("bad name\n", "0x" + "1" * 40, taken), "badname")

    def test_cache_round_trip_and_expiry(self):
        from pathlib import Path
        from flasharb.discovery import load_cache, save_cache
        path = Path(tempfile.mkdtemp()) / "tokens.json"
        tokens = [{"address": self.NEW, "symbol": "NEW", "decimals": 18, "liquidity_usd": 1}]
        save_cache(path, 42161, 20_000, tokens)
        self.assertEqual(load_cache(path, 42161, 24, 20_000), tokens)
        self.assertIsNone(load_cache(path, 1, 24, 20_000))          # other chain
        self.assertIsNone(load_cache(path, 42161, 24, 50_000))      # threshold changed
        self.assertIsNone(load_cache(path, 42161, 0, 20_000))       # expired

    def test_long_tail_gaps_are_flagged_in_scan_mode(self):
        tmp = tempfile.mkdtemp()
        cfg = make_config("scan", tmp)
        a = pool("a", ARB, USDC, 1_000_000 * E18, 1_000_000 * E6, fee=500)
        b = pool("b", ARB, USDC, 1_000_000 * E18, 1_030_000 * E6, fee=500)
        cfg.flash_tokens = ["USDC"]
        chain = FakeChain([a, b])
        chain.verify_route = exact_checker(method="quoter")  # quoters can't see transfer taxes
        chain.discovered = {ARB}
        bot = FlashBot(cfg, chain, None, RiskManager(cfg.risk), Journal(tmp))
        bot.step()
        with open(f"{tmp}/opportunities.csv") as fh:
            self.assertIn("long-tail token: quoters can't see transfer taxes", fh.read())
        self.assertIn("involve long-tail tokens that only quoters checked", bot.summary())


class ConfigTests(unittest.TestCase):
    def test_rejects_bad_address_and_unknown_symbol(self):
        cfg = make_config("scan", tempfile.mkdtemp())
        cfg.tokens["BAD"] = "0x123"
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg = make_config("scan", tempfile.mkdtemp())
        cfg.flash_tokens.append("NOPE")
        with self.assertRaises(ValueError):
            validate(cfg)


if __name__ == "__main__":
    unittest.main()
