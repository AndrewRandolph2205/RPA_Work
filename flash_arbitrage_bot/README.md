# Flash-Loan DEX Arbitrage Bot

An on-chain arbitrage bot for **Arbitrum**, an EVM chain with many
decentralized exchanges and tokens. It watches every pool between your
configured tokens on Uniswap V3, Uniswap V2, SushiSwap and Camelot (V2 and V3). When a cycle of
swaps returns more than it started with, it:

1. **borrows** the starting token with a **Balancer V2 flash loan** (no fee, no
   collateral, no capital of your own),
2. **swaps** through the cycle, e.g. `USDC -> WETH (Uniswap) -> USDC (Sushi)` or
   a triangle `WETH -> ARB -> USDC -> WETH`,
3. **repays** the loan and sends the profit to your wallet.

All three happen inside **one transaction** via the `FlashArbitrage` contract.
If the trade wouldn't end in profit, the contract **reverts the whole thing**:
the loan never happened and only the gas fee is spent.

## Read this first

- **You don't need trading capital, but profit is not guaranteed.** On-chain
  arbitrage is one of the most competitive games in crypto. Professional
  searchers run optimised contracts on dedicated nodes, and on Arbitrum they
  can buy a head start (Timeboost's "express lane"). Most gaps are closed within
  the same block they appear. Expect scan mode to find far fewer real
  opportunities than you'd hope.
- **What you can lose:** gas on transactions that revert because someone got
  there first. On Arbitrum that's typically a few cents each, and the bot caps
  it (`max_daily_gas_usd`, and it halts after `max_consecutive_reverts`). The
  contract never holds your funds.
- **Your private key is the real risk.** Use a **brand-new wallet** holding only
  a few dollars of ETH for gas. Never use your main wallet, never share the key,
  never commit `.env`.
- The contract hasn't been professionally audited. Its design limits damage
  (owner-only, holds no funds, reverts unless profitable), but run the free
  `selftest` and simulate mode before going live.

## How it decides

```
every block (~0.25s on Arbitrum):
  one Multicall3 request reads every pool's reserves/price + Balancer's balances
  price every token in USD via its deepest pool to a stablecoin
  for each 2- and 3-hop cycle through liquid pools:
      compose the swaps into out(x) = A·x / (B + C·x)
      best size x* = (√(A·B) − B) / C, capped by the vault balance and max_loan_usd
      keep it if profit − estimated gas ≥ min_profit_usd
  best candidates -> simulate the real transaction (eth_call, free and exact)
      failed? retry at ½ and ¼ size, then cool the route down for a few blocks
  live mode: send it; the contract enforces min profit on-chain
```

| File | Purpose |
|---|---|
| `contracts/FlashArbitrage.sol` | Flash loan + swaps + profit check, in one transaction |
| `flasharb/amm.py` | Uniswap V2 exact math; V3 via virtual reserves |
| `flasharb/routes.py` | Cycle search, optimal sizing, USD pricing |
| `flasharb/chain.py` | Pool discovery and per-block state via web3 + Multicall3 |
| `flasharb/executor.py` | Simulates (estimate_gas) and sends transactions |
| `flasharb/bot.py` | Main loop, stats, journaling |
| `flasharb/risk.py` | Gas caps, revert budget, halt conditions |
| `run_flash.py` | CLI: `deploy`, `selftest`, `run` |

## Setup

Requires Python 3.11+.

```bash
cd flash_arbitrage_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml
```

**Get an RPC URL** (your connection to the blockchain). Create a free Arbitrum
One endpoint at Alchemy, Infura or QuickNode. The public
`https://arb1.arbitrum.io/rpc` works for a quick try but is rate-limited.
Create a file called `.env` in this folder:

```
RPC_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
```

## Stage 1: scan (free, no wallet)

```bash
python3 run_flash.py run
```

It discovers the pools, then checks every block for gaps. The fast first pass
treats each Uniswap V3 pool as if its liquidity at the current price went on
forever, which can make thin pools look like huge opportunities. So every
candidate is then re-priced exactly with Uniswap's Quoter contract (a free
read-only call) before it's logged to `logs/opportunities.csv` as
`quoter-verified` or `rejected by quoter`. The minute summary shows:

```
mode=scan blocks=240 routes=2904 candidates=52 slowest_block_eval=7ms
  closest this period: best edge after pool fees +0.0200% (...)
  best trade after gas (estimate) $+0.12 (...); needs >= $0.50
  exact quotes: verified=0 rejected=3; none real this period
```

Trust the `exact quotes` line. The estimate lines show how close the fast
pass came. `blocks` should rise by about 240 a minute; far fewer means your
RPC is rate-limiting you, so get a free Alchemy URL.

## Stage 2: deploy, self-test, simulate

1. **Make a new wallet** (e.g. a fresh MetaMask account). Put about $3–5 of
   ETH **on Arbitrum One** in it: withdraw from an exchange directly to the
   Arbitrum network, or bridge.
2. Add its private key to `.env`:
   ```
   PRIVATE_KEY=0x...
   ```
3. Deploy the contract (costs a few cents):
   ```bash
   python3 run_flash.py deploy
   ```
   Copy the printed `contract_address = "0x..."` line into `config.toml`.
4. Run the self-test. It checks every configured address and dry-runs a real
   flash loan through a pool, free of charge:
   ```bash
   python3 run_flash.py selftest
   ```
   Don't continue until it says `SELF-TEST PASSED`.
5. Set `mode = "simulate"` in `config.toml` and run it for a day or more:
   ```bash
   python3 run_flash.py run
   ```
   Every candidate now runs through the real contract against the live chain.
   The summary reports **would-have-made $/hr**. That's an upper bound: it
   assumes nobody else takes the trade first.

## Stage 3: live

Only if simulate mode shows meaningful profit over several days:

```toml
mode = "live"
```
```bash
python3 run_flash.py run --live
```

Profits arrive in your wallet in the borrowed token (WETH, USDC or USDT).
Watch `logs/trades.csv` and the `succeeded` / `reverted` counts. Many
reverts means others are faster; the bot halts itself after
`max_consecutive_reverts`.

## Customising

- **Tokens:** the example config watches 29 Arbitrum tokens: stablecoins,
  BTC, liquid-staked ETH, Arbitrum ecosystem and DeFi tokens. Add or remove
  lines under `[tokens]`; pools between every pair are discovered at startup.
  Each token is verified on-chain when the bot starts. Addresses that aren't
  tokens are skipped with a warning, and a symbol that doesn't match its name
  is flagged. The summary's `slowest_block_eval` shows how long each block's
  analysis takes; keep it well under 250ms (about 40,000 routes take ~25ms).
- **Long-tail discovery** (`[discovery]`, on by default): the bot reads every
  pair listed by the V2-style factories (Sushi, Uniswap V2, Camelot V2), keeps
  up to `max_tokens` tokens with at least `min_liquidity_usd` of real WETH or
  stablecoin paired against them, and compares them across all DEXes. Fewer
  bots watch these tokens, but some charge a hidden transfer tax that makes
  fake gaps: scan mode flags gaps involving them, and only simulate mode can
  confirm one. The first run takes a few minutes and is cached for a day.
- **More DEXes:** any Uniswap V2 fork (factory + router), Uniswap V3 fork whose
  router takes `exactInputSingle`, Camelot-style V2 pairs, or Algebra-based V3
  pools. Set `type` and `router_kind` to match. `selftest` runs a round trip
  through each configured DEX to prove its swap call works.
- **Other EVM chains:** change `chain_id`, the token and DEX addresses, and the
  RPC. The Balancer vault and Multicall3 have the same address on most chains.
  For Base, for example:
  ```toml
  chain_name = "base"
  chain_id = 8453
  # WETH = 0x4200000000000000000000000000000000000006
  # USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
  # Uniswap V3 factory 0x33128a8fC17869897dcE68Ed026d694621f6FDfD, SwapRouter02 0x2626664c2603336E57B271c5C0b26F421741e481
  ```
  Verify every address on the chain's block explorer; `selftest` catches
  addresses with no contract. Avoid Ethereum mainnet: gas is far higher, and its
  public mempool lets other bots copy or sandwich your transactions.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

They cover the pool math, cycle search, sizing, USD pricing and the full
scan/simulate/live decision flow against a fake chain. They need no network
and no packages.
