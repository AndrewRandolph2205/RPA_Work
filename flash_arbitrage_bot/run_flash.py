"""Flash-loan DEX arbitrage bot.

    python run_flash.py deploy          # compile and deploy the FlashArbitrage contract
    python run_flash.py selftest        # free end-to-end check (RPC, pools, simulator, contract)
    python run_flash.py run             # scan / paper / simulate / live, per config mode
    python run_flash.py run --paper     # paper-trade: live mode's decisions, nothing sent
    python run_flash.py run --live      # required in addition to mode = "live"
    python run_flash.py trace-gaps      # who closed the gaps already in logs/gaps.csv?
    python run_flash.py build-simulator # recompile RouteSimulator.sol after editing it

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
    try:
        chain.check_network()
    except RuntimeError as exc:  # wrong chain
        sys.exit(str(exc))
    except Exception as exc:
        sys.exit(explain_rpc_error(exc, cfg))
    return chain


def load_with_retries(chain, cfg, attempts: int = 4) -> None:
    """Pool discovery sends a burst of requests; ride out a temporary refusal."""
    import time
    from flasharb.errors import describe
    for attempt in range(1, attempts + 1):
        try:
            chain.load()
            return
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt == attempts:
                sys.exit(explain_rpc_error(exc, cfg))
            wait = 5 * attempt
            print(f"Loading pools failed ({describe(exc)}); retrying in {wait}s...")
            time.sleep(wait)


def mask_url(url: str) -> str:
    """Hide the API key (usually the last path segment) when printing an RPC URL."""
    head, _, tail = url.rstrip("/").rpartition("/")
    return f"{head}/{tail[:4]}..." if head and len(tail) > 8 else url


def explain_rpc_error(exc: Exception, cfg) -> str:
    url = os.environ.get(cfg.rpc_url_env, "")
    from flasharb.errors import mask_secrets
    text = mask_secrets(str(exc).replace(url, mask_url(url)))
    status = getattr(getattr(exc, "response", None), "status_code", None)
    hints = {
        401: "The RPC rejected the API key. Check it was copied completely.",
        403: (f"The RPC refused access. With Alchemy this usually means {cfg.chain_name} isn't "
              "enabled for your app: open the app in the Alchemy dashboard, turn on "
              f"'{cfg.chain_name.title()} Mainnet' under Networks, and remove any IP/domain "
              "allowlist restrictions."),
        429: "The RPC is rate-limiting you. Use a paid plan or a less busy endpoint.",
    }
    hint = hints.get(status, "Check that RPC_URL in .env is correct and your internet works.")
    return f"Could not connect to the RPC ({mask_url(url)}).\n{hint}\n\nDetails: {text[:300]}"


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
        if dex.quoter:
            labels[f"{dex.name} quoter"] = dex.quoter
    if cfg.contract_address:
        labels["contract_address"] = cfg.contract_address
    missing = chain.missing_code(labels)
    for label in labels:
        print(f"  [{'FAIL' if label in missing else ' OK '}] {label} has contract code")
    ok &= not missing

    load_with_retries(chain, cfg)
    block = chain.refresh()
    print(f"  [ OK ] discovered {len(chain.pools)} pools")
    print(f"  [ OK ] Balancer flash-loan fee is {chain.flash_fee_rate * 1e4:.1f} bps")
    ok &= check_simulator(chain, cfg, block)

    if not cfg.contract_address:
        print("\nScan mode is ready." if ok else "\nSELF-TEST FAILED - fix the items above before running")
        print("contract_address is empty: run `python run_flash.py deploy` before simulate or live mode.")
        return 0 if ok else 1
    key = os.environ.get("PRIVATE_KEY") or None
    executor = ContractExecutor(chain, cfg.contract_address, cfg.owner_address, key)
    owner, vault = executor.read_owner_and_vault()
    owner_ok = owner == executor.owner.lower()
    vault_ok = vault == cfg.balancer_vault
    print(f"  [{' OK ' if owner_ok else 'FAIL'}] contract owner is {executor.owner}")
    print(f"  [{' OK ' if vault_ok else 'FAIL'}] contract vault matches config")
    ok &= owner_ok and vault_ok

    # One deliberately losing round trip per dex, through its deepest pool that
    # contains a flash token. Reaching the Unprofitable check proves the flash
    # loan, that dex's swap call and the approvals all work.
    from flasharb.routes import Route
    flash = [cfg.token(s) for s in cfg.flash_tokens if cfg.token(s) in chain.decimals]
    for dex in cfg.dexes.values():
        candidates = [(p, t) for p in chain.pools if p.dex == dex.name and p.active
                      for t in flash if p.has(t) and chain.vault_balances.get(t, 0) > 0]
        if not candidates:
            print(f"  [FAIL] {dex.name}: no active pool with a borrowable token to test")
            ok = False
            continue
        pool, token = max(candidates, key=lambda c: c[0].depth()[0 if c[1] == c[0].token0 else 1])
        route = Route((pool, pool), (token, pool.other(token), token))
        amount = max(1, 10 ** chain.decimals[token] // 1000)  # 0.001 of the token
        result = executor.simulate(route, amount, 0)
        passed = result.error == "Unprofitable"
        print(f"  [{' OK ' if passed else 'FAIL'}] {dex.name}: round trip through "
              f"{route.describe({a: s for s, a in cfg.tokens.items()})} reached the profit check "
              f"(got: {result.error or 'no revert?!'})")
        ok &= passed
    print("\nSELF-TEST PASSED" if ok else "\nSELF-TEST FAILED - fix the items above before running")
    return 0 if ok else 1


def check_simulator(chain, cfg, block) -> bool:
    """One exact round trip through the deepest WETH pool, as scan mode checks candidates."""
    from flasharb.routes import Route
    weth = cfg.token(cfg.native_wrapped)
    pools = [p for p in chain.pools if p.active and p.has(weth)]
    if not pools:
        print("  [FAIL] no active pool with the native token to test exact checks on")
        return False
    pool = max(pools, key=lambda p: p.depth()[0 if p.token0 == weth else 1])
    route = Route((pool, pool), (weth, pool.other(weth), weth))
    amount = 10 ** chain.decimals[weth] // 100
    outcome = chain.verify_route(route, [amount], block)[0]
    if not outcome.ok:
        print(f"  [FAIL] exact check of {pool.label} round trip failed: {outcome.reason}")
        return False
    loss = 1 - outcome.out / amount
    how = ("RouteSimulator via eth_call state override" if outcome.method == "sim" else
           "quoter contracts (this RPC doesn't support state overrides; transfer taxes go unseen)")
    print(f"  [ OK ] exact checks use {how}: 0.01 WETH round trip through {pool.label} "
          f"lost {loss:.2%} (pool fees)")
    return True


def check_simulator_source() -> None:
    import hashlib
    from flasharb.simulator_code import SOURCE_SHA256
    source = (HERE / "contracts" / "RouteSimulator.sol").read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        print("warning: contracts/RouteSimulator.sol changed since flasharb/simulator_code.py was built; "
              "run `python run_flash.py build-simulator`")


def cmd_build_simulator(cfg, args) -> int:
    """Recompile contracts/RouteSimulator.sol into flasharb/simulator_code.py."""
    import hashlib
    import textwrap
    try:
        import solcx
    except ModuleNotFoundError:
        sys.exit(f"py-solc-x isn't installed. Run: {sys.executable} -m pip install -r requirements.txt")
    source = HERE / "contracts" / "RouteSimulator.sol"
    solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_files([str(source)], output_values=["bin-runtime"], solc_version=SOLC_VERSION,
                              optimize=True, optimize_runs=200, evm_version="shanghai")
    runtime = next(v for k, v in out.items() if k.endswith(":RouteSimulator"))["bin-runtime"]
    body = "\n".join(f'    "{line}"' for line in textwrap.wrap(runtime, 96))
    (HERE / "flasharb" / "simulator_code.py").write_text(
        '"""Runtime bytecode of contracts/RouteSimulator.sol (generated; don\'t edit by hand).\n\n'
        f"Built with solc {SOLC_VERSION}, optimizer on (200 runs), evmVersion shanghai.\n"
        "If you change RouteSimulator.sol, rebuild this file with:\n"
        '    python run_flash.py build-simulator\n"""\n\n'
        f'SOURCE_SHA256 = "{hashlib.sha256(source.read_bytes()).hexdigest()}"\n\n'
        f"RUNTIME_HEX = (\n{body}\n)\n")
    print(f"wrote flasharb/simulator_code.py ({len(runtime) // 2} bytes of runtime code)")
    return 0


def resolve_pools(chain, description: str):
    """Pool addresses for a route description like "WETH -[camelot_v3]-> MAGIC -[sushiswap/0.3%]-> WETH"."""
    import re
    tokens = re.split(r"\s+-\[[^\]]+\]->\s+", description.strip())
    labels = re.findall(r"-\[([^\]]+)\]->", description)
    pools = []
    for a, b, label in zip(tokens, tokens[1:], labels):
        pair = {chain.cfg.tokens.get(a), chain.cfg.tokens.get(b)}
        match = [p for p in chain.pools if p.label == label and {p.token0, p.token1} == pair]
        if len(match) != 1:
            return None
        pools.append(match[0].address)
    return tuple(pools)


def cmd_trace_gaps(cfg, args) -> int:
    """Find the transactions that closed the verified gaps already in logs/gaps.csv."""
    import csv
    from flasharb.closers import CloserTracer, GapRecord
    from flasharb.journal import Journal
    rows = []
    for path in sorted(Path(cfg.log_dir).glob("gaps*.csv")):  # includes files set aside by older versions
        with path.open(newline="") as fh:
            rows += list(csv.DictReader(fh))
    rows.sort(key=lambda r: int(r["first_block"]))
    if args.last:
        rows = rows[-args.last:]
    if not rows:
        print(f"No gaps in {cfg.log_dir}/gaps.csv yet: run scan mode until some verified gaps close.")
        return 1
    chain = connect(cfg)
    if any(not r.get("pools") for r in rows):
        load_with_retries(chain, cfg)  # older rows name pools only by dex label
    tracer = CloserTracer(chain, Journal(cfg.log_dir))
    for r in rows:
        pools = tuple(r["pools"].split()) if r.get("pools") else resolve_pools(chain, r["route"])
        if not pools:
            print(f"  couldn't match the pools of {r['route']}; skipped")
            continue
        last = int(r["last_block"])
        # Older rows lack closed_by_block; the bot skipped some blocks back then, so search a few.
        closed = int(r.get("closed_by_block") or last + 8)
        tracer.submit(GapRecord(r["route"], pools, int(r["first_block"]), last, closed, float(r["net_usd"])))
    tracer.wait()
    for row in tracer.results:
        if row.get("tx_hash"):
            lane = {True: "EXPRESS LANE", False: "regular"}.get(row["timeboosted"], "lane unknown")
            print(f"{row['route']}\n    closed in block +{row['blocks_after_last_open']} at index "
                  f"{row['tx_index']}, {lane}, {row['kind']}, to {row['to']}\n    tx {row['tx_hash']}")
        else:
            print(f"{row['route']}\n    {row['kind']}")
    print("\n" + (tracer.summary() or "nothing traced"))
    print(f"Details appended to {Path(cfg.log_dir) / 'closers.csv'}")
    return 0


def cmd_run(cfg, args) -> int:
    from flasharb.bot import FlashBot
    from flasharb.executor import ContractExecutor
    from flasharb.journal import Journal
    from flasharb.risk import RiskManager

    if getattr(args, "paper", False):
        if args.live:
            sys.exit("--paper and --live can't be combined.")
        cfg.mode = "paper"  # paper-trade whatever the config says; nothing is ever sent
    if cfg.mode == "live" and not args.live:
        sys.exit('Config says mode = "live" but --live was not passed. Refusing to trade.')
    if args.live and cfg.mode != "live":
        sys.exit(f'--live passed but config mode is "{cfg.mode}". Refusing to trade.')

    chain = connect(cfg)
    load_with_retries(chain, cfg)
    executor = None
    if cfg.mode in ("simulate", "live"):
        if not cfg.contract_address:
            sys.exit("contract_address is empty: run `python run_flash.py deploy` first.")
        key = need_env("PRIVATE_KEY") if cfg.mode == "live" else os.environ.get("PRIVATE_KEY") or None
        executor = ContractExecutor(chain, cfg.contract_address, cfg.owner_address, key,
                                    send_url=cfg.send_rpc_url, gas_limit=cfg.live_gas_limit)
        if cfg.mode == "live":
            executor.prepare()  # nonce + receipt watcher, so the first send is one request
    check_simulator_source()
    journal = Journal(cfg.log_dir)
    feed = None
    if cfg.sequencer_feed_url:
        from flasharb.feed import SequencerFeed
        feed = SequencerFeed(cfg.sequencer_feed_url, cfg.feed_block_offset)
        feed.start()
    paper = None
    if cfg.mode == "paper":
        from flasharb.paper import PaperTrader
        paper = PaperTrader(chain, cfg)
        hold = {"auto": f"plus Timeboost's {cfg.paper_timeboost_delay_ms:.0f}ms hold while the sequencer feed "
                        "shows the express lane in use",
                "on": f"plus Timeboost's {cfg.paper_timeboost_delay_ms:.0f}ms hold (always)",
                "off": "no Timeboost hold"}[cfg.paper_timeboost]
        print(f"Paper trading: trades land {cfg.paper_send_latency_ms:.0f}ms (send) after each decision, {hold}; "
              f"results go to {Path(cfg.log_dir) / 'paper_trades.csv'}. Nothing is sent.")
    tracer = None
    if cfg.mode == "scan" and cfg.trace_closers:
        from flasharb.closers import CloserTracer
        tracer = CloserTracer(chain, journal)
    bot = FlashBot(cfg, chain, executor, RiskManager(cfg.risk), journal, feed=feed, tracer=tracer, paper=paper)
    try:
        bot.run(max_blocks=args.blocks)
    finally:
        if feed is not None:
            feed.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Flash-loan DEX arbitrage bot")
    parser.add_argument("command", choices=["deploy", "selftest", "run", "trace-gaps", "build-simulator"])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--blocks", type=int, default=None, help="run: stop after N blocks")
    parser.add_argument("--live", action="store_true", help="run: confirm real transactions")
    parser.add_argument("--paper", action="store_true",
                        help="run: paper-trade (live mode's decisions, nothing sent), whatever the config's mode")
    parser.add_argument("--last", type=int, default=0, help="trace-gaps: only the last N gaps")
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
    commands = {"deploy": cmd_deploy, "selftest": cmd_selftest, "run": cmd_run, "trace-gaps": cmd_trace_gaps,
                "build-simulator": cmd_build_simulator}
    return commands[args.command](cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("stopped")
