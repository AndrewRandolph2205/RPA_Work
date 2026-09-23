"""Simulates and sends FlashArbitrage.execute transactions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .amm import ROUTER_KINDS
from .routes import Route

log = logging.getLogger(__name__)

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


def _hex(value) -> str:
    return bytes(value).hex()


class ContractExecutor:
    def __init__(self, chain, contract_address: str, owner_address: str = "",
                 private_key: Optional[str] = None):
        self.chain = chain
        w3 = chain.w3
        self.contract = w3.eth.contract(address=chain.cs(contract_address), abi=CONTRACT_ABI)
        self.account = w3.eth.account.from_key(private_key) if private_key else None
        owner = self.account.address if self.account else owner_address
        if not owner:
            raise ValueError("need PRIVATE_KEY or owner_address to call the contract")
        self.owner = chain.cs(owner)
        keccak = chain.Web3.keccak
        self._errors = {_hex(keccak(text=sig)[:4]): sig.split("(")[0] for sig in ERROR_SIGNATURES}
        self._event_topic = _hex(keccak(text=ARBITRAGE_EVENT))

    def steps(self, route: Route) -> List[Tuple[int, str, str, str, int]]:
        cs = self.chain.cs
        return [(ROUTER_KINDS[pool.router_kind], cs(pool.router), cs(t_in), cs(t_out),
                 pool.fee_ppm if pool.kind == "v3" else 0)
                for pool, t_in, t_out in route.hops()]

    def _fn(self, route: Route, amount: int, min_profit: int):
        return self.contract.functions.execute(
            self.chain.cs(route.start), int(amount), self.steps(route), int(min_profit))

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

    def send(self, route: Route, amount: int, min_profit: int, gas_estimate: int) -> SendResult:
        if self.account is None:
            raise RuntimeError("PRIVATE_KEY is required to send transactions")
        w3 = self.chain.w3
        tx_hash = ""
        try:
            tx = self._fn(route, amount, min_profit).build_transaction({
                "from": self.owner,
                "nonce": w3.eth.get_transaction_count(self.owner, "pending"),
                "gas": int(gas_estimate * 1.3),
                "chainId": self.chain.cfg.chain_id,
            })
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = "0x" + _hex(w3.eth.send_raw_transaction(raw))
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60, poll_latency=0.1)
        except Exception as exc:
            return SendResult(False, tx_hash, 0, 0, self.decode_error(exc))

        gas_cost = int(receipt["gasUsed"]) * int(receipt.get("effectiveGasPrice") or tx.get("maxFeePerGas", 0))
        if receipt["status"] != 1:
            return SendResult(False, tx_hash, 0, gas_cost, "reverted on-chain (beaten to it or price moved)")
        profit = 0
        contract = self.contract.address.lower()
        for entry in receipt["logs"]:
            topics = entry.get("topics") or []
            if entry["address"].lower() == contract and topics and _hex(topics[0]) == self._event_topic:
                _, profit = self.chain.decode(["uint256", "uint256"], bytes(entry["data"]))
        return SendResult(True, tx_hash, int(profit), gas_cost)

    def read_owner_and_vault(self) -> Tuple[str, str]:
        return (self.contract.functions.owner().call().lower(),
                self.contract.functions.vault().call().lower())
