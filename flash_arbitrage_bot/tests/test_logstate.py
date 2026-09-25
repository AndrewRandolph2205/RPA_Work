"""Push-based pool state: event decoding, applying events, stream readiness, Chain integration."""

import json
import logging
import time
import unittest

from flasharb.amm import Q96, Pool
from flasharb.chain import Chain
from flasharb.logstate import (EVENT_SIGNATURES, LATEST, KNOWN_TOPICS, LogStream, apply_event, decode_log,
                               event_topics, ws_url_from_http)
from tests.test_flasharb import E6, E18, USDC, WETH, make_config, pool

TOPICS = {"0x" + f"{i:x}" * 64: kind for i, kind in enumerate(EVENT_SIGNATURES, start=1)}
TOPIC = {kind: topic for topic, kind in TOPICS.items()}


def word(value: int) -> str:
    return (value % (1 << 256)).to_bytes(32, "big").hex()


def log_entry(kind, address, block, index, data=(), topics=(), removed=False):
    return {"address": address, "blockNumber": hex(block), "logIndex": hex(index), "removed": removed,
            "topics": [TOPIC[kind], *("0x" + word(t) for t in topics)],
            "data": "0x" + "".join(word(v) for v in data)}


def v3_pool(name="v3pool", sqrt_price=Q96, liquidity=10 ** 18, tick=0):
    p = Pool(address="0x" + name.encode().hex().ljust(40, "0")[:40], dex=name, kind="v3", token0=WETH,
             token1=USDC, fee_ppm=500, router="0x" + "9" * 40, router_kind="v3_router02")
    p.update_v3(sqrt_price, liquidity)
    p.tick = tick
    return p


class DecodeTests(unittest.TestCase):
    def test_sync(self):
        e = decode_log(log_entry("sync", "0xABC", 10, 3, data=(5, 7)), TOPICS)
        self.assertEqual((e.kind, e.pool, e.block, e.index, e.values), ("sync", "0xabc", 10, 3, (5, 7)))

    def test_swap_signed_values(self):
        e = decode_log(log_entry("swap", "0xa", 1, 0, data=(-5, 9, Q96, 123, -887000),
                                 topics=(1, 2)), TOPICS)
        self.assertEqual(e.values, (-5, 9, Q96, 123, -887000))

    def test_mint_and_burn_ticks_come_from_topics(self):
        mint = decode_log(log_entry("mint", "0xa", 1, 0, data=(0xdead, 50, 3, 4), topics=(0x1, -120, 60)), TOPICS)
        self.assertEqual(mint.values, (-120, 60, 50, 3, 4))
        burn = decode_log(log_entry("burn", "0xa", 1, 0, data=(50, 3, 4), topics=(0x1, -120, 60)), TOPICS)
        self.assertEqual(burn.values, (-120, 60, 50, 3, 4))

    def test_unknown_or_broken_logs(self):
        self.assertIsNone(decode_log({"topics": ["0x" + "0" * 64], "data": "0x"}, TOPICS))
        self.assertIsNone(decode_log(log_entry("sync", "0xa", 1, 0, data=(5,)), TOPICS))  # short data
        self.assertIsNone(decode_log({"topics": []}, TOPICS))

    def test_known_topics_checked(self):
        with self.assertRaises(RuntimeError):
            event_topics(lambda sig: b"\x00" * 32)
        self.assertEqual(set(KNOWN_TOPICS) <= set(EVENT_SIGNATURES), True)

    def test_ws_url(self):
        self.assertEqual(ws_url_from_http("https://arb-mainnet.g.alchemy.com/v2/k"),
                         "wss://arb-mainnet.g.alchemy.com/v2/k")
        self.assertEqual(ws_url_from_http("http://localhost:8545"), "ws://localhost:8545")


class ApplyTests(unittest.TestCase):
    def ev(self, kind, pool_, *values):
        entry = {"sync": lambda: log_entry("sync", pool_.address, 1, 0, data=values),
                 "swap": lambda: log_entry("swap", pool_.address, 1, 0, data=values, topics=(1, 2)),
                 "mint": lambda: log_entry("mint", pool_.address, 1, 0, data=(1, *values[2:]),
                                           topics=(1, values[0], values[1])),
                 "burn": lambda: log_entry("burn", pool_.address, 1, 0, data=values[2:],
                                           topics=(1, values[0], values[1])),
                 "fee": lambda: log_entry("fee", pool_.address, 1, 0, data=values),
                 "camelot_fee": lambda: log_entry("camelot_fee", pool_.address, 1, 0, data=values)}[kind]()
        return decode_log(entry, TOPICS)

    def test_sync_sets_reserves(self):
        p = pool("a", WETH, USDC, E18, 2000 * E6)
        self.assertTrue(apply_event(p, self.ev("sync", p, 2 * E18, 1000 * E6)))
        self.assertEqual((p.reserve0, p.reserve1), (2 * E18, 1000 * E6))

    def test_swap_matches_an_rpc_read(self):
        p, q = v3_pool(), v3_pool()
        p.balance0, p.balance1 = 100, 100
        apply_event(p, self.ev("swap", p, 10, -4, 2 * Q96, 5 * 10 ** 17, 6931))
        q.update_v3(2 * Q96, 5 * 10 ** 17)
        self.assertEqual((p.reserve0, p.reserve1, p.tick), (q.reserve0, q.reserve1, 6931))
        self.assertEqual((p.balance0, p.balance1), (110, 96))

    def test_mint_in_range_adds_liquidity_out_of_range_doesnt(self):
        p = v3_pool(tick=0)
        apply_event(p, self.ev("mint", p, -60, 60, 10 ** 18, 5, 5))
        self.assertEqual(p.liquidity, 2 * 10 ** 18)
        apply_event(p, self.ev("mint", p, 60, 120, 10 ** 18, 0, 5))
        self.assertEqual(p.liquidity, 2 * 10 ** 18)
        apply_event(p, self.ev("burn", p, -60, 60, 10 ** 18, 5, 5))
        self.assertEqual(p.liquidity, 10 ** 18)
        self.assertEqual(p.reserve0, v3_pool().reserve0)
        # the upper tick is exclusive, the lower inclusive (as in the pool contract)
        apply_event(p, self.ev("mint", p, -60, 0, 10 ** 18, 5, 5))
        self.assertEqual(p.liquidity, 10 ** 18)
        apply_event(p, self.ev("mint", p, 0, 60, 10 ** 18, 5, 5))
        self.assertEqual(p.liquidity, 2 * 10 ** 18)

    def test_unknown_tick_cant_apply(self):
        p = v3_pool()
        p.tick = None
        self.assertFalse(apply_event(p, self.ev("mint", p, -60, 60, 1, 0, 0)))

    def test_fees(self):
        algebra = v3_pool()
        algebra.kind = "algebra"
        apply_event(algebra, self.ev("fee", algebra, 150, 3000))
        self.assertEqual((algebra.fee_ppm, algebra.fee1_ppm), (150, 3000))
        camelot = pool("c", WETH, USDC, E18, E18, kind="camelot_v2")
        apply_event(camelot, self.ev("camelot_fee", camelot, 300, 50))
        self.assertEqual((camelot.fee_ppm, camelot.fee1_ppm), (3000, 500))

    def test_events_for_other_pool_kinds_are_ignored(self):
        p = pool("a", WETH, USDC, E18, E18)
        self.assertTrue(apply_event(p, self.ev("swap", p, 1, 1, Q96, 1, 0)))
        self.assertEqual(p.reserve0, E18)


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def subscribed_stream(clock=None, addresses=("0xa",)):
    stream = LogStream("wss://x", TOPICS, settle_ms=20, clock=clock or time.monotonic)
    stream.watch(addresses)
    requests = stream.begin_subscribing()
    for i, _ in enumerate(requests, start=1):
        stream.handle_message(json.dumps({"jsonrpc": "2.0", "id": i, "result": f"0xsub{i}"}))
    return stream


def head(number, sub="0xsub1"):
    return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": sub, "result": {"number": hex(number)}}})


def logmsg(entry, sub="0xsub2"):
    return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": sub, "result": entry}})


class StreamTests(unittest.TestCase):
    def test_subscribe_requests_chunk_addresses(self):
        stream = LogStream("wss://x", TOPICS)
        stream.watch([f"0x{i:040x}" for i in range(1200)])
        requests = stream.begin_subscribing()
        self.assertEqual([r["params"][0] for r in requests], ["newHeads", "logs", "logs", "logs"])
        self.assertEqual(len(requests[1]["params"][1]["address"]), 500)
        self.assertEqual(sorted(requests[1]["params"][1]["topics"][0]), sorted(TOPICS))
        self.assertFalse(stream.ready())
        for r in requests:
            stream.handle_message(json.dumps({"id": r["id"], "result": f"0x{r['id']}"}))
        self.assertTrue(stream.ready())
        self.assertEqual(stream.generation, 1)

    def test_refused_subscription_raises(self):
        stream = LogStream("wss://x", TOPICS)
        stream.watch(["0xa"])
        stream.begin_subscribing()
        with self.assertRaises(ConnectionError):
            stream.handle_message(json.dumps({"id": 1, "error": {"message": "nope"}}))

    def test_new_pool_set_needs_new_subscription(self):
        stream = subscribed_stream()
        stream.watch(["0xa"])
        self.assertTrue(stream.ready())
        stream.watch(["0xa", "0xb"])
        self.assertFalse(stream.ready())

    def test_block_ready_after_head_and_settle(self):
        clock = FakeClock()
        stream = subscribed_stream(clock)
        self.assertFalse(stream.wait_ready(10, timeout=0))
        stream.handle_message(head(10))
        self.assertFalse(stream.wait_ready(10, timeout=0))  # settling
        clock.t += 0.021
        self.assertTrue(stream.wait_ready(10, timeout=0))

    def test_block_ready_when_a_later_block_shows_up(self):
        stream = subscribed_stream(FakeClock())
        stream.handle_message(logmsg(log_entry("sync", "0xa", 11, 0, data=(1, 2))))
        self.assertTrue(stream.wait_ready(10, timeout=0))
        stream.handle_message(head(12))
        self.assertTrue(stream.wait_ready(11, timeout=0))

    def test_take_returns_chain_order_and_keeps_later_blocks(self):
        stream = subscribed_stream()
        for block, index in [(11, 0), (10, 5), (10, 1), (12, 0)]:
            stream.handle_message(logmsg(log_entry("sync", "0xa", block, index, data=(block, index))))
        self.assertEqual([e.position for e in stream.take(11)], [(10, 1), (10, 5), (11, 0)])
        self.assertEqual([e.position for e in stream.take(99)], [(12, 0)])

    def test_reorg_bumps_generation(self):
        stream = subscribed_stream()
        before = stream.generation
        stream.handle_message(logmsg(log_entry("sync", "0xa", 10, 0, data=(1, 2), removed=True)))
        self.assertEqual(stream.generation, before + 1)
        self.assertEqual(stream.take(99), [])

    def test_notifications_from_old_subscriptions_are_ignored(self):
        stream = subscribed_stream()
        stream.handle_message(logmsg(log_entry("sync", "0xa", 10, 0, data=(1, 2)), sub="0xold"))
        self.assertEqual(stream.take(99), [])


class FakeRpcChain(Chain):
    """Chain without web3: RPC reads return whatever `rpc_state` says."""

    def __init__(self, cfg, pools, stream):
        self.cfg, self.pools, self.tracked = cfg, pools, {p.address for p in pools}
        self._last_block, self.logs = None, None
        self._logs_generation, self._logs_synced_at = -1, float("-inf")
        self._marks, self.last_drift = {}, None
        self.state_stats = {k: 0 for k in ("logs", "rpc", "resyncs", "timeouts", "stale_events", "unapplied",
                                           "late_events")}
        self.rpc_state, self.rpc_reads = {}, []
        self.attach_logs(stream)

    def _refresh_rpc(self, full, block, wait_s):
        self.rpc_reads.append(block)
        for p in self.pools:
            if p.address in self.rpc_state:
                p.update_v2(*self.rpc_state[p.address])
            self._marks[p.address] = (block, LATEST)
        self.logs.take(block)
        self._last_block = block
        self.state_stats["rpc"] += 1
        return block


class ChainLogsTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.cfg = make_config("scan", "/tmp")
        self.p = pool("a", WETH, USDC, E18, 2000 * E6)
        self.clock = FakeClock()
        self.stream = subscribed_stream(self.clock, [self.p.address])
        self.chain = FakeRpcChain(self.cfg, [self.p], self.stream)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def sync(self, block, index, r0, r1):
        self.stream.handle_message(logmsg(log_entry("sync", self.p.address, block, index, data=(r0, r1))))

    def test_first_block_is_read_then_events_take_over(self):
        self.stream.handle_message(head(10))
        self.assertEqual(self.chain.refresh(block=10, wait_s=0), 10)
        self.assertEqual(self.chain.rpc_reads, [10])
        self.sync(11, 0, 5, 6)
        self.sync(11, 1, 7, 8)
        self.stream.handle_message(head(11))
        self.clock.t += 0.05
        self.assertEqual(self.chain.refresh(block=11, wait_s=0), 11)
        self.assertEqual(self.chain.rpc_reads, [10])  # no RPC read
        self.assertEqual((self.p.reserve0, self.p.reserve1), (7, 8))
        self.assertEqual(self.chain.state_stats["logs"], 1)

    def test_events_already_in_a_read_are_skipped(self):
        self.stream.handle_message(head(10))
        self.chain.rpc_state[self.p.address] = (1, 2)
        self.chain.refresh(block=10, wait_s=0)
        self.sync(10, 3, 99, 99)  # late event from the block that was read
        self.sync(11, 0, 5, 6)
        self.stream.handle_message(head(12))
        self.chain.refresh(block=11, wait_s=0)
        self.assertEqual((self.p.reserve0, self.p.reserve1), (5, 6))

    def test_events_after_their_block_was_priced_count_as_late(self):
        self.stream.handle_message(head(10))
        self.chain.refresh(block=10, wait_s=0)
        self.sync(11, 0, 5, 6)
        self.stream.handle_message(head(12))
        self.chain.refresh(block=11, wait_s=0)
        self.sync(11, 1, 7, 8)  # block 11 was already priced
        self.sync(12, 0, 9, 9)
        self.stream.handle_message(head(13))
        self.chain.refresh(block=12, wait_s=0)
        self.assertEqual(self.chain.state_stats["late_events"], 1)
        self.assertEqual((self.p.reserve0, self.p.reserve1), (9, 9))

    def test_not_ready_falls_back_to_rpc(self):
        self.stream.handle_message(head(10))
        self.chain.refresh(block=10, wait_s=0)
        self.assertEqual(self.chain.refresh(block=11, wait_s=0), 11)  # the node hasn't sent block 11
        self.assertEqual(self.chain.rpc_reads, [10, 11])
        self.assertEqual(self.chain.state_stats["timeouts"], 1)

    def test_reconnect_forces_a_read(self):
        self.stream.handle_message(head(10))
        self.chain.refresh(block=10, wait_s=0)
        self.stream.invalidate()
        self.stream.handle_message(head(11))
        self.chain.refresh(block=11, wait_s=0)
        self.assertEqual(self.chain.rpc_reads, [10, 11])

    def test_periodic_resync_measures_drift(self):
        self.cfg.logs_resync_s = 0.0001
        self.stream.handle_message(head(10))
        self.chain.refresh(block=10, wait_s=0)
        self.sync(11, 0, 5, 6)
        self.stream.handle_message(head(12))
        time.sleep(0.001)
        self.chain.rpc_state[self.p.address] = (5, 7)  # the RPC disagrees with the events
        self.chain.refresh(block=11, wait_s=0)
        self.assertEqual(self.chain.last_drift, (1, 1))
        self.assertEqual((self.p.reserve0, self.p.reserve1), (5, 7))  # the RPC wins

    def test_no_drift_when_events_match(self):
        self.cfg.logs_resync_s = 0.0001
        self.stream.handle_message(head(10))
        self.chain.refresh(block=10, wait_s=0)
        self.sync(11, 0, 5, 6)
        self.stream.handle_message(head(12))
        time.sleep(0.001)
        self.chain.rpc_state[self.p.address] = (5, 6)
        self.chain.refresh(block=11, wait_s=0)
        self.assertEqual(self.chain.last_drift, (0, 1))


if __name__ == "__main__":
    unittest.main()
