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


class FakeChain:
    def __init__(self, pools, vault=None, gas_price=10 ** 7):
        self.pools = pools
        self.decimals = DEC
        self.vault_balances = vault if vault is not None else {WETH: 10_000 * E18, USDC: 10 ** 8 * E6}
        self.block = 100
        self.gas_price = gas_price
        self.last_gas_price_wei = gas_price

    def refresh(self):
        self.block += 1
        return self.block

    def gas_price_wei(self):
        self.last_gas_price_wei = self.gas_price
        return self.gas_price


class FakeExecutor:
    def __init__(self, max_ok_amount=None, send_success=True):
        self.max_ok_amount = max_ok_amount
        self.send_success = send_success
        self.simulated, self.sent = [], []

    def simulate(self, route, amount, min_profit):
        self.simulated.append(amount)
        if self.max_ok_amount is not None and amount > self.max_ok_amount:
            return SimResult(False, 0, "Unprofitable")
        return SimResult(True, 400_000)

    def send(self, route, amount, min_profit, gas):
        self.sent.append(amount)
        if self.send_success:
            return SendResult(True, "0xabc", route.amount_out(amount) - amount, gas * 10 ** 7)
        return SendResult(False, "0xdef", 0, gas * 10 ** 7, "reverted on-chain")


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

    def test_no_opportunity_when_fees_exceed_gap(self):
        dear = pool("dear", WETH, USDC, 1000 * E18, 2_004_000 * E6)  # 0.2% gap < 0.35% fees
        bot = self.bot("scan", pools=[self.cheap, dear])
        bot.step()
        self.assertEqual(bot.stats.candidates, 0)

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
