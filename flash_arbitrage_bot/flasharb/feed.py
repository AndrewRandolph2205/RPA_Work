"""Arbitrum sequencer feed: hear about every block the moment it's sequenced.

The feed (wss://arb1.arbitrum.io/feed) is the stream Arbitrum nodes themselves
follow. Each message is one L2 block, numbered sequenceNumber + the chain's
Nitro genesis block (22207817 on Arbitrum One). RPC providers read the same
feed and then execute each block, so the feed announces a block before any RPC
can serve its state, and it never skips one. The bot waits on the feed and then
reads the pools pinned to exactly that block (retrying until the RPC has it),
instead of polling the RPC and sleeping between polls, which missed ~14% of
blocks.

Each message also carries blockMetadata: a version byte (0) followed by a
bitmap marking which transactions in the block came through Timeboost's
express lane.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import describe

log = logging.getLogger(__name__)

_KEEP = 4096  # recent blocks remembered (arrival times, hashes)
# Timeboost sells the express lane one round at a time: 60s, 240 blocks.
ROUND_BLOCKS = 240
# If no block has carried blockMetadata for this many blocks, the relay stopped
# sending it and express-lane activity can't be judged any more.
_META_STALE_BLOCKS = 20


def block_metadata(meta) -> Optional[bytes]:
    """A block's blockMetadata as bytes (version 0, then the express-lane bitmap),
    or None when it's missing or in a version this code doesn't know."""
    if not meta:
        return None
    try:
        if isinstance(meta, str):
            data = bytes.fromhex(meta[2:]) if meta.startswith("0x") else base64.b64decode(meta)
        elif isinstance(meta, list):
            data = bytes(meta)
        else:
            return None
    except (ValueError, TypeError):
        return None
    if len(data) < 2 or data[0] != 0:  # only version 0 is defined
        return None
    return data


def express_count(meta) -> int:
    """Timeboosted transactions in a block, from the feed's blockMetadata."""
    data = block_metadata(meta)
    return sum(bin(b).count("1") for b in data[1:]) if data else 0


@dataclass
class FeedStats:
    blocks: int = 0
    express_blocks: int = 0   # blocks with at least one express-lane transaction
    express_txs: int = 0
    sequence_gaps: int = 0    # blocks the feed skipped (only across reconnects)
    connects: int = 0
    bad_messages: int = 0

    def reset(self) -> None:
        self.blocks = self.express_blocks = self.express_txs = 0


class SequencerFeed:
    def __init__(self, url: str, block_offset: int, stale_after_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        self.url = url
        self.block_offset = block_offset
        self.stale_after_s = stale_after_s
        self._clock = clock
        self._cond = threading.Condition()
        self._latest_seq: Optional[int] = None
        self._arrivals: "OrderedDict[int, float]" = OrderedDict()  # seq -> clock() when it arrived
        self._hashes: "OrderedDict[int, str]" = OrderedDict()      # seq -> block hash (newer relays)
        self._last_message_at: Optional[float] = None
        # Express-lane activity, by sequence number: first and latest block that
        # carried blockMetadata, and latest block with an express-lane transaction.
        self._first_meta_seq: Optional[int] = None
        self._last_meta_seq: Optional[int] = None
        self._last_express_seq: Optional[int] = None
        self.connected = False
        self.stats = FeedStats()      # reset with the bot's summary window
        self.total = FeedStats()      # since start
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    # ----- public API ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="sequencer-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def healthy(self) -> bool:
        with self._cond:
            return (self.connected and self._last_message_at is not None
                    and self._clock() - self._last_message_at < self.stale_after_s)

    def latest_block(self) -> Optional[int]:
        with self._cond:
            return None if self._latest_seq is None else self._latest_seq + self.block_offset

    def wait_for_block_after(self, block: Optional[int], timeout: float) -> Optional[int]:
        """Newest announced block above `block`, waiting up to `timeout` seconds for one."""
        deadline = self._clock() + timeout
        with self._cond:
            while True:
                if self._latest_seq is not None:
                    latest = self._latest_seq + self.block_offset
                    if block is None or latest > block:
                        return latest
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def arrival(self, block: int) -> Optional[float]:
        with self._cond:
            return self._arrivals.get(block - self.block_offset)

    def age_ms(self, block: int) -> Optional[float]:
        """Milliseconds since the feed announced `block`."""
        with self._cond:
            arrived = self._arrivals.get(block - self.block_offset)
        return None if arrived is None else (self._clock() - arrived) * 1000

    def express_lane_state(self, window: int = ROUND_BLOCKS) -> Optional[bool]:
        """Is someone using Timeboost's express lane? True if a block within the
        last `window` blocks had an express-lane transaction; False once the feed
        has read that many blocks' metadata without one; None when it can't tell
        (just started, or the relay sends no blockMetadata)."""
        with self._cond:
            latest, first = self._latest_seq, self._first_meta_seq
            last_meta, last_express = self._last_meta_seq, self._last_express_seq
        if latest is None:
            return None
        if last_express is not None and latest - last_express < window:
            return True
        if first is None or last_meta is None or latest - last_meta > _META_STALE_BLOCKS:
            return None
        return False if latest - first >= window else None

    def recent_hashes(self):
        """[(block, hash)] newest first, for checking the block-number offset."""
        with self._cond:
            return [(seq + self.block_offset, h) for seq, h in reversed(self._hashes.items())]

    # ----- messages -----------------------------------------------------------

    def handle_message(self, raw) -> int:
        """Parse one websocket message; returns how many new blocks it announced."""
        try:
            obj = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
            messages = obj.get("messages") or []
        except (ValueError, AttributeError):
            with self._cond:
                self.stats.bad_messages += 1
                self.total.bad_messages += 1
            return 0
        now, new = self._clock(), 0
        with self._cond:
            for msg in messages:
                seq = msg.get("sequenceNumber") if isinstance(msg, dict) else None
                if not isinstance(seq, int):
                    continue
                if self._latest_seq is not None:
                    if seq <= self._latest_seq:
                        continue  # replayed backlog after a reconnect
                    for s in (self.stats, self.total):
                        s.sequence_gaps += seq - self._latest_seq - 1
                self._latest_seq = seq
                self._arrivals[seq] = now
                if msg.get("blockHash"):
                    self._hashes[seq] = msg["blockHash"]
                for store in (self._arrivals, self._hashes):
                    while len(store) > _KEEP:
                        store.popitem(last=False)
                meta = block_metadata(msg.get("blockMetadata"))
                express = sum(bin(b).count("1") for b in meta[1:]) if meta else 0
                if meta is not None:
                    if self._first_meta_seq is None:
                        self._first_meta_seq = seq
                    self._last_meta_seq = seq
                if express:
                    self._last_express_seq = seq
                for s in (self.stats, self.total):
                    s.blocks += 1
                    s.express_txs += express
                    s.express_blocks += 1 if express else 0
                new += 1
            self._last_message_at = now
            self._cond.notify_all()
        return new

    # ----- connection -----------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.error("sequencer feed stopped: %s", describe(exc))
        with self._cond:
            self.connected = False

    async def _run(self) -> None:
        import websockets  # installed with web3; imported here so tests don't need it

        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self.url, max_size=None, open_timeout=10, ping_interval=20,
                                              ping_timeout=20, close_timeout=2) as ws:
                    with self._cond:
                        self.connected = True
                    self.total.connects += 1
                    log.info("sequencer feed connected (%s)", self.url)
                    backoff = 1.0
                    async for raw in ws:
                        self.handle_message(raw)
                        if self._stop:
                            break
            except Exception as exc:
                log.warning("sequencer feed: %s; reconnecting in %.0fs", describe(exc), backoff)
            with self._cond:
                self.connected = False
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def check_offset(feed: SequencerFeed, chain) -> Optional[str]:
    """Confirm feed block numbers match the RPC's by comparing block hashes.

    Returns "ok", "corrected" (offset fixed from the RPC), or None when there's
    nothing to compare yet (the RPC hasn't caught up, or the relay sends no
    hashes)."""
    head = chain.head()
    hashes = feed.recent_hashes()[:20]
    if not hashes:  # relay sends no hashes: the feed should sit at or just ahead of the RPC
        latest = feed.latest_block()
        return "ok" if latest is not None and -4 <= latest - head <= 40 else None
    for block, feed_hash in hashes:
        if block > head:
            continue
        rpc_hash = chain.block_hash(block)
        if not rpc_hash:
            continue
        if rpc_hash.lower() == feed_hash.lower():
            return "ok"
        number = chain.block_number_by_hash(feed_hash)
        if number is None:
            return None
        feed.block_offset += number - block
        log.warning("sequencer feed block numbers were off by %d; corrected (offset now %d)",
                    number - block, feed.block_offset)
        return "corrected"
    return None
