"""Flash-loan DEX arbitrage bot.

    python run_flash.py deploy     # compile and deploy the FlashArbitrage contract
    python run_flash.py selftest   # free end-to-end check of the deployed contract
    python run_flash.py run        # scan / simulate / live, per config mode
    python run_flash.py run --live # required in addition to mode = "live"

Secrets come from environment variables (or a .env file next to this script):
    RPC_URL      your node endpoint (name configurable via rpc_url_env)
    PRIVATE_KEY  the bot wallet's key; needed for deploy and live mode only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from flasharb.config import load_config

HERE = Path(__file__).resolve().parent
SOLC_VERSION = "0.8.24"


def load_dotenv(path: Path) -> None:
    """Minimal .env support: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Environment variable {name} is not set (put it in a .env file or export it).")
    return value


def connect(cfg):
    try:
        from flasharb.chain import Chain
        chain = Chain(cfg, need_env(cfg.rpc_url_env))
    except ModuleNotFoundError:
        sys.exit("web3 isn't installed for this Python. Install it with:\n"
                 f"  {sys.executable} -m pip install -r requirements.txt")
    chain.check_network()
    return chain


def cmd_deploy(cfg, args) -> int:
    try:
        import solcx
    except ModuleNotFoundError:
        sys.exit(f"py-solc-x isn't installed. Run: {sys.executable} -m pip install -r requirements.txt")
    chain = connect(cfg)
    w3 = chain.w3
    account = w3.eth.account.from_key(need_env("PRIVATE_KEY"))

    print(f"Compiling contracts/FlashArbitrage.sol with solc {SOLC_VERSION}...")
    solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_files([str(HERE / "contracts" / "FlashArbitrage.sol")],
                              output_values=["abi", "bin"], solc_version=SOLC_VERSION,
                              optimize=True, optimize_runs=200)
    artifact = next(v for k, v in out.items() if k.endswith(":FlashArbitrage"))
    build = HERE / "build"
    build.mkdir(exist_ok=True)
    (build / "FlashArbitrage.json").write_text(json.dumps(artifact, indent=2))

    balance = w3.eth.get_balance(account.address)
    print(f"Deployer {account.address} has {balance / 1e18:.6f} ETH on {cfg.chain_name}")
    if balance == 0:
        sys.exit("The wallet has no ETH for gas. Send a little ETH on this network first.")

    contract = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bin"])
    tx = contract.constructor(chain.cs(cfg.balancer_vault)).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": cfg.chain_id,
    })
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = w3.eth.send_raw_transaction(raw)
    print(f"Sent deployment tx 0x{bytes(tx_hash).hex()}; waiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    if receipt["status"] != 1:
        sys.exit("Deployment reverted.")
    print(f"\nDeployed FlashArbitrage at {receipt['contractAddress']}")
    print(f'Put this in your config:  contract_address = "{receipt["contractAddress"]}"')
    return 0


def cmd_selftest(cfg, args) -> int:
    """Checks every configured address, then eth_calls a deliberately losing round
    trip. Reaching the Unprofitable check proves the flash loan, both swaps and
    the approvals all work, without spending anything."""
    from flasharb.executor import ContractExecutor

    chain = connect(cfg)
    ok = True
    labels = {f"token {s}": a for s, a in cfg.tokens.items()}
    labels["balancer_vault"] = cfg.balancer_vault
    for dex in cfg.dexes.values():
        labels[f"{dex.name} factory"] = dex.factory
        labels[f"{dex.name} router"] = dex.router
    if cfg.contract_address:
        labels["contract_address"] = cfg.contract_address
    missing = chain.missing_code(labels)
    for label in labels:
        print(f"  [{'FAIL' if label in missing else ' OK '}] {label} has contract code")
    ok &= not missing

    chain.load()
    chain.refresh()
    print(f"  [ OK ] discovered {len(chain.pools)} pools")

    if not cfg.contract_address:
        print("\ncontract_address is empty: run `python run_flash.py deploy` first.")
        return 1
    key = os.environ.get("PRIVATE_KEY") or None
    executor = ContractExecutor(chain, cfg.contract_address, cfg.owner_address, key)
    owner, vault = executor.read_owner_and_vault()
    owner_ok = owner == executor.owner.lower()
    vault_ok = vault == cfg.balancer_vault
    print(f"  [{' OK ' if owner_ok else 'FAIL'}] contract owner is {executor.owner}")
    print(f"  [{' OK ' if vault_ok else 'FAIL'}] contract vault matches config")
    ok &= owner_ok and vault_ok

    native = cfg.token(cfg.native_wrapped)
    stables = {cfg.token(s) for s in cfg.stable_tokens}
    pool = max((p for p in chain.pools if p.active and p.has(native) and p.other(native) in stables),
               key=lambda p: p.reserves_for(native)[0], default=None)
    if pool is None:
        print("  [FAIL] no active pool between the native token and a stable token")
        return 1
    from flasharb.routes import Route
    route = Route((pool, pool), (native, pool.other(native), native))
    amount = min(10 ** chain.decimals[native] // 1000, chain.vault_balances.get(native, 0))
    if native not in [cfg.token(s) for s in cfg.flash_tokens] or amount == 0:
        print(f"  [FAIL] {cfg.native_wrapped} must be a flash_token with a vault balance for the self-test")
        return 1
    result = executor.simulate(route, amount, 0)
    passed = result.error == "Unprofitable"
    print(f"  [{' OK ' if passed else 'FAIL'}] round trip through {pool.label} reached the "
          f"profit check (got: {result.error or 'no revert?!'})")
    ok &= passed
    print("\nSELF-TEST PASSED" if ok else "\nSELF-TEST FAILED - fix the items above before running")
    return 0 if ok else 1


def cmd_run(cfg, args) -> int:
    from flasharb.bot import FlashBot
    from flasharb.executor import ContractExecutor
    from flasharb.journal import Journal
    from flasharb.risk import RiskManager

    if cfg.mode == "live" and not args.live:
        sys.exit('Config says mode = "live" but --live was not passed. Refusing to trade.')
    if args.live and cfg.mode != "live":
        sys.exit(f'--live passed but config mode is "{cfg.mode}". Refusing to trade.')

    chain = connect(cfg)
    chain.load()
    executor = None
    if cfg.mode in ("simulate", "live"):
        if not cfg.contract_address:
            sys.exit("contract_address is empty: run `python run_flash.py deploy` first.")
        key = need_env("PRIVATE_KEY") if cfg.mode == "live" else os.environ.get("PRIVATE_KEY") or None
        executor = ContractExecutor(chain, cfg.contract_address, cfg.owner_address, key)
    chain.refresh()
    bot = FlashBot(cfg, chain, executor, RiskManager(cfg.risk), Journal(cfg.log_dir))
    bot.run(max_blocks=args.blocks)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Flash-loan DEX arbitrage bot")
    parser.add_argument("command", choices=["deploy", "selftest", "run"])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--blocks", type=int, default=None, help="run: stop after N blocks")
    parser.add_argument("--live", action="store_true", help="run: confirm real transactions")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(HERE / ".env")
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}\nCreate one from the example first:\n"
              "  cp config.example.toml config.toml")
        return 2
    except ValueError as exc:
        print(f"Problem in {args.config}: {exc}")
        return 2
    return {"deploy": cmd_deploy, "selftest": cmd_selftest, "run": cmd_run}[args.command](cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("stopped")
