"""Push-based pool state: follow the pools' own events instead of re-reading them.

With `state_source = "logs"` the bot keeps one websocket open to the RPC
provider and subscribes (eth_subscribe) to every event that changes a tracked
pool's price:

    Sync                     V2 / Camelot V2: the new reserves
    Swap                     Uniswap V3 / Algebra: new sqrtPrice, liquidity, tick
    Mint, Burn               V3 / Algebra: liquidity added or removed around a tick
    Fee, FeePercentUpdated   Algebra / Camelot V2: dynamic fees changed

The node pushes these as soon as it has executed a block, so the bot no
longer spends a round trip per block (plus retries while the RPC catches up
with the sequencer feed) re-reading ~1,000 pools that mostly didn't change.

The websocket thread only receives and parses. Events are applied to the
pools on the bot's own thread (Chain.refresh), all at once and in log order,
so the bot never sees a half-updated block.

A block counts as complete once the node's newHeads for it has arrived and
`settle_ms` has passed, or as soon as anything from a later block arrives.
State is rebuilt from a normal RPC read after every (re)connect and every
`resync_s`, and each periodic resync compares the event-built state with the
RPC's to measure drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import describe, mask_secrets

log = logging.getLogger(__name__)

EVENT_SIGNATURES = {
    "sync": "Sync(uint112,uint112)",
    # Uniswap V3 and Algebra share these three signatures (same types, other names).
    "swap": "Swap(address,address,int256,int256,uint160,uint128,int24)",
    "mint": "Mint(address,address,int24,int24,uint128,uint256,uint256)",
    "burn": "Burn(address,int24,int24,uint128,uint256,uint256)",
    "fee": "Fee(uint16,uint16)",                                  # Algebra, per-direction fees
    "camelot_fee": "FeePercentUpdated(uint16,uint16)",           # Camelot V2, out of 100,000
}
# Known values, checked against keccak at startup (see event_topics).
KNOWN_TOPICS = {
    "sync": "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1",
    "swap": "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    "mint": "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",
    "burn": "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
}
_ADDRESSES_PER_SUBSCRIPTION = 500
_KEEP_HEADS = 256
LATEST = 1 << 62  # log index meaning "after every log in the block" (state read by RPC)


def event_topics(keccak: Callable[[str], bytes]) -> Dict[str, str]:
    """{topic0 hex: event kind}, from the signatures."""
    topics = {}
    for kind, signature in EVENT_SIGNATURES.items():
        topic = "0x" + bytes(keccak(signature)).hex()
        known = KNOWN_TOPICS.get(kind)
        if known and topic != known:
            raise RuntimeError(f"keccak({signature}) = {topic}, expected {known}")
        topics[topic] = kind
    return topics


# ----- decoding ----------------------------------------------------------------

def _words(data: str) -> List[bytes]:
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return [raw[i:i + 32] for i in range(0, len(raw) - len(raw) % 32, 32)]


def _uint(word: bytes) -> int:
    return int.from_bytes(word, "big")


def _int(word: bytes) -> int:
    return int.from_bytes(word, "big", signed=True)


def _hex_int(value) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


@dataclass
class Event:
    kind: str
    pool: str
    block: int
    index: int
    values: tuple

    @property
    def position(self) -> Tuple[int, int]:
        return self.block, self.index


def decode_log(entry: dict, topics: Dict[str, str]) -> Optional[Event]:
    """One eth_subscribe log -> Event, or None for anything it doesn't know."""
    try:
        raw_topics = entry.get("topics") or []
        kind = topics.get(raw_topics[0].lower()) if raw_topics else None
        if kind is None:
            return None
        words = _words(entry.get("data") or "0x")
        if kind == "sync":
            values = (_uint(words[0]), _uint(words[1]))
        elif kind == "swap":  # amount0, amount1, sqrtPriceX96, liquidity, tick
            values = (_int(words[0]), _int(words[1]), _uint(words[2]), _uint(words[3]), _int(words[4]))
        elif kind == "mint":  # topics: owner, tickLower, tickUpper; data: sender, amount, amount0, amount1
            values = (_int(bytes.fromhex(raw_topics[2][2:])), _int(bytes.fromhex(raw_topics[3][2:])),
                      _uint(words[1]), _uint(words[2]), _uint(words[3]))
        elif kind == "burn":  # topics: owner, tickLower, tickUpper; data: amount, amount0, amount1
            values = (_int(bytes.fromhex(raw_topics[2][2:])), _int(bytes.fromhex(raw_topics[3][2:])),
                      _uint(words[0]), _uint(words[1]), _uint(words[2]))
        else:  # fee / camelot_fee: two uint16
            values = (_uint(words[0]), _uint(words[1]))
        return Event(kind, entry["address"].lower(), _hex_int(entry["blockNumber"]),
                     _hex_int(entry.get("logIndex", 0)), values)
    except (IndexError, KeyError, ValueError, TypeError, AttributeError):
        return None


def apply_event(pool, event: Event) -> bool:
    """Update `pool` in place. Returns False when the event can't be applied
    (then the pool needs an RPC read)."""
    kind, v = event.kind, event.values
    if kind == "sync":
        if pool.kind not in ("v2", "camelot_v2"):
            return True
        pool.update_v2(v[0], v[1])
    elif kind == "camelot_fee":
        if pool.kind == "camelot_v2":
            pool.fee_ppm, pool.fee1_ppm = v[0] * 10, v[1] * 10
    elif kind == "fee":
        if pool.kind == "algebra":
            pool.fee_ppm, pool.fee1_ppm = v[0], v[1]
    elif not pool.concentrated:
        return True
    elif kind == "swap":
        amount0, amount1, sqrt_price, liquidity, tick = v
        pool.update_v3(sqrt_price, liquidity)
        pool.tick = tick
        if pool.balance0 is not None and pool.balance1 is not None:  # positive = paid into the pool
            pool.balance0 = max(0, pool.balance0 + amount0)
            pool.balance1 = max(0, pool.balance1 + amount1)
    else:  # mint / burn
        lower, upper, amount, amount0, amount1 = v
        if pool.tick is None or pool.sqrt_price_x96 is None or pool.liquidity is None:
            return False
        if lower <= pool.tick < upper and amount:
            liquidity = pool.liquidity + amount if kind == "mint" else pool.liquidity - amount
            if liquidity < 0:
                return False
            pool.update_v3(pool.sqrt_price_x96, liquidity)
        if kind == "mint" and pool.balance0 is not None and pool.balance1 is not None:
            # A burn only moves tokens to the owner's tab; they leave on Collect,
            # which isn't followed: balances catch up at the next resync.
            pool.balance0 += amount0
            pool.balance1 += amount1
    return True


# ----- the websocket stream -------------------------------------------------------

@dataclass
class LogStats:
    logs: int = 0
    heads: int = 0
    connects: int = 0
    removed: int = 0          # logs a reorg took back (forces a resync)
    bad_messages: int = 0


class LogStream:
    """eth_subscribe to newHeads and the pools' events over one websocket."""

    def __init__(self, url: str, topics: Dict[str, str], settle_ms: float = 20.0,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url
        self.topics = topics
        self.settle_s = settle_ms / 1000
        self._clock = clock
        self._cond = threading.Condition()
        self._addresses: Tuple[str, ...] = ()
        self._wanted: Optional[Tuple[str, ...]] = None  # set by watch(); the thread picks it up
        self._pending: List[Event] = []
        self._head: Optional[int] = None
        self._head_at: "OrderedDict[int, float]" = OrderedDict()
        self._max_log_block: Optional[int] = None
        self._subscriptions: Dict[str, str] = {}     # subscription id -> "heads" / "logs"
        self._expected = 0                           # subscription acks still missing
        self.subscribed = False
        # Bumped on every (re)subscribe and every reorg: state built from events
        # before that point can't be trusted, so the chain reads everything again.
        self.generation = 0
        self.stats = LogStats()
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    # ----- public API ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="pool-logs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def watch(self, addresses: Iterable[str]) -> None:
        """Follow exactly these pools. A new set means a new subscription."""
        wanted = tuple(sorted(a.lower() for a in addresses))
        with self._cond:
            if wanted != (self._wanted if self._wanted is not None else self._addresses):
                self._wanted = wanted
                self.subscribed = False

    def ready(self) -> bool:
        with self._cond:
            return self.subscribed and self._wanted is None

    def wait_ready(self, block: int, timeout: float) -> bool:
        """Wait until every event of `block` should have arrived."""
        deadline = self._clock() + timeout
        with self._cond:
            while True:
                if not self.subscribed:
                    return False
                if (self._head is not None and self._head > block) or \
                        (self._max_log_block is not None and self._max_log_block > block):
                    return True
                now = self._clock()
                head_at = self._head_at.get(block) if self._head is not None and self._head >= block else None
                if head_at is None and self._head is not None and self._head >= block:
                    head_at = now - self.settle_s  # head too old to be remembered: long since complete
                if head_at is not None and now - head_at >= self.settle_s:
                    return True
                remaining = deadline - now
                if remaining <= 0:
                    return False
                wait = remaining if head_at is None else min(remaining, head_at + self.settle_s - now)
                self._cond.wait(max(wait, 0.001))

    def take(self, upto_block: int) -> List[Event]:
        """Remove and return buffered events up to `upto_block`, in chain order."""
        with self._cond:
            taken = [e for e in self._pending if e.block <= upto_block]
            self._pending = [e for e in self._pending if e.block > upto_block]
        taken.sort(key=lambda e: e.position)
        return taken

    def invalidate(self) -> None:
        with self._cond:
            self.generation += 1

    # ----- messages -----------------------------------------------------------

    def subscribe_requests(self) -> List[dict]:
        """The eth_subscribe calls for the current address set (ids from 1)."""
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}]
        addresses = list(self._addresses)
        topic_list = sorted(self.topics)
        for i in range(0, len(addresses), _ADDRESSES_PER_SUBSCRIPTION):
            requests.append({"jsonrpc": "2.0", "id": len(requests) + 1, "method": "eth_subscribe",
                             "params": ["logs", {"address": addresses[i:i + _ADDRESSES_PER_SUBSCRIPTION],
                                                 "topics": [topic_list]}]})
        return requests

    def begin_subscribing(self) -> List[dict]:
        """Adopt the wanted address set and return the requests to send."""
        with self._cond:
            if self._wanted is not None:
                self._addresses, self._wanted = self._wanted, None
            self._subscriptions = {}
            self.subscribed = False
            requests = self.subscribe_requests()
            self._expected = len(requests)
        return requests

    def handle_message(self, raw) -> None:
        try:
            obj = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, AttributeError):
            self.stats.bad_messages += 1
            return
        if not isinstance(obj, dict):
            self.stats.bad_messages += 1
            return
        if "id" in obj and obj.get("method") is None:  # a subscription ack
            self._handle_ack(obj)
            return
        params = obj.get("params") or {}
        kind = self._subscriptions.get(params.get("subscription"))
        result = params.get("result")
        if kind is None or not isinstance(result, dict):
            return  # e.g. a late notification from a subscription replaced by a reconnect
        now = self._clock()
        with self._cond:
            if kind == "heads":
                try:
                    number = _hex_int(result["number"])
                except (KeyError, ValueError, TypeError):
                    self.stats.bad_messages += 1
                    return
                self.stats.heads += 1
                if self._head is None or number > self._head:
                    self._head = number
                self._head_at.setdefault(number, now)
                while len(self._head_at) > _KEEP_HEADS:
                    self._head_at.popitem(last=False)
            else:
                if result.get("removed"):
                    self.stats.removed += 1
                    self.generation += 1
                    self._cond.notify_all()
                    return
                event = decode_log(result, self.topics)
                if event is None:
                    self.stats.bad_messages += 1
                    return
                self.stats.logs += 1
                self._pending.append(event)
                if self._max_log_block is None or event.block > self._max_log_block:
                    self._max_log_block = event.block
            self._cond.notify_all()

    def _handle_ack(self, obj: dict) -> None:
        if obj.get("error") or not isinstance(obj.get("result"), str):
            raise ConnectionError(f"eth_subscribe refused: {obj.get('error')}")
        with self._cond:
            self._subscriptions[obj["result"]] = "heads" if obj["id"] == 1 else "logs"
            if len(self._subscriptions) == self._expected and self._wanted is None:
                self.subscribed = True
                self.generation += 1
                self._pending = []  # anything before this point is covered by the resync read
                self._cond.notify_all()

    def _disconnected(self) -> None:
        with self._cond:
            self.subscribed = False
            self._subscriptions = {}
            self._cond.notify_all()

    # ----- connection -----------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.error("pool event stream stopped: %s", describe(exc))
        self._disconnected()

    async def _run(self) -> None:
        import websockets  # installed with web3; imported here so tests don't need it

        backoff = 1.0
        while not self._stop:
            with self._cond:
                have_addresses = bool(self._wanted or self._addresses)
            if not have_addresses:
                await asyncio.sleep(0.1)
                continue
            try:
                async with websockets.connect(self.url, max_size=None, open_timeout=10, ping_interval=20,
                                              ping_timeout=20, close_timeout=2) as ws:
                    requests = self.begin_subscribing()
                    for request in requests:
                        await ws.send(json.dumps(request))
                    self.stats.connects += 1
                    log.info("pool event stream connected (%s): following %d pools",
                             mask_secrets(self.url), len(self._addresses))
                    backoff = 1.0
                    while not self._stop:
                        with self._cond:
                            if self._wanted is not None:
                                break  # the pool set changed: subscribe afresh
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                        except asyncio.TimeoutError:
                            continue
                        self.handle_message(raw)
            except Exception as exc:
                log.warning("pool event stream: %s; reconnecting in %.0fs", describe(exc), backoff)
                self._disconnected()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self._disconnected()


def ws_url_from_http(url: str) -> str:
    """Most providers serve websockets on the same URL: https -> wss."""
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def pool_state(pool) -> tuple:
    """What drift checks compare: everything that prices the pool."""
    return (pool.reserve0, pool.reserve1, pool.fee_ppm, pool.fee1_ppm)


def drifted(before: Dict[str, tuple], pools: Sequence) -> List[str]:
    return [p.address for p in pools if p.address in before and before[p.address] != pool_state(p)]

