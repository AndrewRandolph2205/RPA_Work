"""Simulates and sends FlashArbitrage.execute transactions.

Live sends take the short path: the transaction is built and signed locally
(nonce tracked here, fixed gas limit, cached gas price) and sent in a single
request, optionally straight to the sequencer. No eth_call first: the
contract's own profit check reverts a trade that no longer pays, which costs
only that transaction's gas. Receipts are collected on a background thread so
the main loop never waits for one.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .errors import describe
from .routes import Route
from .simulator import route_steps

log = logging.getLogger(__name__)

EXECUTE_SIGNATURE = "execute(address,uint256,(uint8,address,address,address,uint24)[],uint256)"
EXECUTE_TYPES = ["address", "uint256", "(uint8,address,address,address,uint24)[]", "uint256"]
CONTRACT_ABI = [
    {"type": "function", "name": "execute", "stateMutability": "nonpayable", "outputs": [], "inputs": [
        {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "steps", "type": "tuple[]", "components": [
            {"name": "kind", "type": "uint8"},
            {"name": "router", "type": "address"},
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"}]},
        {"name": "minProfit", "type": "uint256"}]},
    {"type": "function", "name": "owner", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "vault", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
]
ERROR_SIGNATURES = ["NotOwner()", "NotVault()", "UnexpectedLoan()", "BadRoute()",
                    "Unprofitable(uint256,uint256)", "TokenCallFailed(address)"]
ARBITRAGE_EVENT = "Arbitrage(address,uint256,uint256)"
_NONCE_ERRORS = ("nonce too low", "nonce too high", "already known", "invalid nonce")


@dataclass
class SimResult:
    ok: bool
    gas_used: int = 0
    error: str = ""


@dataclass
class SendResult:
    success: bool
    tx_hash: str
    profit_raw: int
    gas_cost_wei: int
    error: str = ""
    mined_block: Optional[int] = None
    timeboosted: Optional[bool] = None


def _hex(value) -> str:
    return bytes(value).hex()


class ContractExecutor:
    def __init__(self, chain, contract_address: str, owner_address: str = "",
                 private_key: Optional[str] = None, send_url: str = "", gas_limit: int = 2_000_000):
        self.chain = chain
        w3 = chain.w3
        self.contract = w3.eth.contract(address=chain.cs(contract_address), abi=CONTRACT_ABI)
        self.account = w3.eth.account.from_key(private_key) if private_key else None
        owner = self.account.address if self.account else owner_address
        if not owner:
            raise ValueError("need PRIVATE_KEY or owner_address to call the contract")
        self.owner = chain.cs(owner)
        self.gas_limit = gas_limit
        keccak = chain.Web3.keccak
        self._errors = {_hex(keccak(text=sig)[:4]): sig.split("(")[0] for sig in ERROR_SIGNATURES}
        self._event_topic = _hex(keccak(text=ARBITRAGE_EVENT))
        self._execute_selector = bytes(keccak(text=EXECUTE_SIGNATURE)[:4])
        self._send_w3 = w3
        if send_url:  # e.g. the sequencer's endpoint, which accepts only eth_sendRawTransaction
            self._send_w3 = chain.Web3(chain.Web3.HTTPProvider(send_url, request_kwargs={"timeout": 5}))
            try:
                self._send_w3.middleware_onion.remove("validation")  # it would call eth_chainId first
            except Exception:
                pass
        self.nonce: Optional[int] = None
        self._watcher: Optional[_ReceiptWatcher] = None

    def steps(self, route: Route) -> List[Tuple[int, str, str, str, int]]:
        return route_steps(route, self.chain.cs)

    def _fn(self, route: Route, amount: int, min_profit: int):
        return self.contract.functions.execute(
            self.chain.cs(route.start), int(amount), self.steps(route), int(min_profit))

    def calldata(self, route: Route, amount: int, min_profit: int) -> bytes:
        return self._execute_selector + self.chain.encode(
            EXECUTE_TYPES, [self.chain.cs(route.start), int(amount), self.steps(route), int(min_profit)])

    def decode_error(self, exc: Exception) -> str:
        text = " ".join(str(a) for a in getattr(exc, "args", ())) + " " + str(getattr(exc, "data", "") or "")
        text = text.lower()
        for selector, name in self._errors.items():
            if selector in text:
                return name
        return (str(exc) or type(exc).__name__)[:200]

    def simulate(self, route: Route, amount: int, min_profit: int) -> SimResult:
        """estimate_gas runs the full transaction against the current chain state."""
        try:
            gas = self._fn(route, amount, min_profit).estimate_gas({"from": self.owner})
            return SimResult(True, int(gas))
        except Exception as exc:
            return SimResult(False, 0, self.decode_error(exc))

    # ----- live: fast path ----------------------------------------------------

    def prepare(self) -> None:
        """Read the nonce once and start the receipt watcher (live mode)."""
        if self.account is None:
            raise RuntimeError("PRIVATE_KEY is required to send transactions")
        self._sync_nonce()
        self._watcher = _ReceiptWatcher(self.chain.receipt_raw, self._to_result)
        self._watcher.start()

    def _sync_nonce(self) -> None:
        self.nonce = int(self.chain.w3.eth.get_transaction_count(self.owner, "pending"))

    def submit(self, route: Route, amount: int, min_profit: int) -> str:
        """Sign and send one execute() transaction; returns its hash without waiting."""
        if self.account is None:
            raise RuntimeError("PRIVATE_KEY is required to send transactions")
        if self.nonce is None or self._watcher is None:
            self.prepare()
        gas_price = self.chain.gas_price_wei()
        tx = {
            "to": self.contract.address, "data": "0x" + self.calldata(route, amount, min_profit).hex(),
            "value": 0, "gas": self.gas_limit, "nonce": self.nonce, "chainId": self.chain.cfg.chain_id,
            "type": 2, "maxPriorityFeePerGas": 0,  # Arbitrum orders by arrival, not tips
            "maxFeePerGas": max(2 * gas_price, gas_price + 10 ** 7),
        }
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        try:
            tx_hash = "0x" + _hex(self._send_w3.eth.send_raw_transaction(raw))
        except Exception as exc:
            if any(marker in str(exc).lower() for marker in _NONCE_ERRORS):
                self._sync_nonce()
            raise
        self.nonce += 1
        self._watcher.add(tx_hash)
        return tx_hash

    def poll_results(self) -> List[SendResult]:
        """Receipts that arrived since the last call (never blocks)."""
        return self._watcher.drain() if self._watcher else []

    def _to_result(self, tx_hash: str, receipt: Optional[dict]) -> SendResult:
        if receipt is None:
            return SendResult(False, tx_hash, 0, 0, "no receipt after 60s (dropped?)")
        gas_cost = int(receipt.get("gasUsed", "0x0"), 16) * int(receipt.get("effectiveGasPrice", "0x0"), 16)
        block = int(receipt["blockNumber"], 16) if receipt.get("blockNumber") else None
        boosted = receipt.get("timeboosted")
        boosted = boosted if isinstance(boosted, bool) else None
        if int(receipt.get("status", "0x0"), 16) != 1:
            return SendResult(False, tx_hash, 0, gas_cost, "reverted on-chain (beaten to it or price moved)",
                              block, boosted)
        profit = 0
        contract = self.contract.address.lower()
        for entry in receipt.get("logs") or []:
            topics = entry.get("topics") or []
            if entry.get("address", "").lower() == contract and topics and \
                    topics[0].lower().removeprefix("0x") == self._event_topic:
                _, profit = self.chain.decode(["uint256", "uint256"], bytes.fromhex(entry["data"][2:]))
        return SendResult(True, tx_hash, int(profit), gas_cost, "", block, boosted)

    def read_owner_and_vault(self) -> Tuple[str, str]:
        return (self.contract.functions.owner().call().lower(),
                self.contract.functions.vault().call().lower())


class _ReceiptWatcher(threading.Thread):
    """Polls receipts for sent transactions and queues the results."""

    def __init__(self, fetch, convert, timeout_s: float = 60.0, poll_s: float = 0.1):
        super().__init__(name="receipts", daemon=True)
        self._fetch, self._convert = fetch, convert
        self._timeout_s, self._poll_s = timeout_s, poll_s
        self._pending: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._done: "queue.Queue[SendResult]" = queue.Queue()

    def add(self, tx_hash: str) -> None:
        with self._lock:
            self._pending[tx_hash] = time.monotonic()

    def drain(self) -> List[SendResult]:
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    def run(self) -> None:
        while True:
            with self._lock:
                pending = list(self._pending.items())
            for tx_hash, sent_at in pending:
                try:
                    receipt = self._fetch(tx_hash)
                except Exception as exc:
                    log.debug("receipt %s: %s", tx_hash, describe(exc))
                    continue
                if receipt is None and time.monotonic() - sent_at < self._timeout_s:
                    continue
                with self._lock:
                    self._pending.pop(tx_hash, None)
                self._done.put(self._convert(tx_hash, receipt))
            time.sleep(self._poll_s)
