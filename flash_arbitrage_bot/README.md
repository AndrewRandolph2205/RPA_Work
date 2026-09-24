# Flash-Loan DEX Arbitrage Bot

An on-chain arbitrage scanner and executor for **Arbitrum**. Every block
(~0.25s) it prices ~1,400 pools across **Uniswap V3, Uniswap V2, SushiSwap,
Camelot V2 and Camelot V3**, searches thousands of 2- and 3-step swap cycles
for one that returns more than it starts with, and can execute a winner in a
single transaction:

1. **borrow** the starting token with a **Balancer V2 flash loan** (no fee, no
   collateral, no capital of your own),
2. **swap** through the cycle, e.g. `WETH -> tBTC -> WBTC -> WETH`,
3. **repay** the loan and send the profit to the owner's wallet.

If the trade wouldn't end in profit, the `FlashArbitrage` contract **reverts
the whole transaction**. The loan never happens and only gas is spent.

## Results: what a day of scanning found

The bot was run in scan mode (free, read-only) for about 17 hours across two
sessions, seeing ~87% of all Arbitrum blocks.

| | Session 1 (~9.5 h) | Session 2 (wider search) |
|---|---|---|
| Tokens watched | 29 core + ~6 discovered | 35 core + 119 discovered |
| Pools / routes | 846 / 2,810 | 1,374 / 4,964 |
| Candidates from the fast estimate | 67 | hundreds (mostly one thin meme-coin pool) |
| **Confirmed by exact on-chain quotes** | **3** | see `logs/gaps.csv` |
| Value of confirmed gaps | $0.68 + $0.36 + $4.67 ≈ **$5.70** | |

**Conclusion:** gaps between Arbitrum DEXes are real but rare and small:
roughly **$0.60/hr in the best case**, assuming this bot won every race.
Professional searchers with co-located nodes and paid sequencer priority
take most of them within the block they appear. The one sizeable find
(tBTC/WBTC, $4.67) came from two tokens that should be worth the same briefly
drifting apart after a large trade, which is the most promising category and
why the config now watches more of them.

What the numbers taught along the way, and what the bot does about it:

- **Most apparent opportunities are illusions.** Uniswap V3-style pools
  concentrate liquidity in narrow price bands, so a quick constant-product
  estimate can report 39%, 115% or $772 "gaps" in pools holding a few dollars
  at the current price. Every candidate is therefore re-priced with the DEXes'
  own Quoter contracts before it counts, and over 90% are rejected.
- **Pool depth, not speed or capital, limits profit.** At 87%+ of blocks seen
  and $250k of flash-loan capacity, real gaps still only supported $10–$300
  trades worth cents, because profit scales with the *square* of the gap.
- **Whether a gap is winnable depends on how long it lasts.** The bot records
  every confirmed gap's lifetime in blocks (`logs/gaps.csv`): gaps gone within
  1–2 blocks belong to faster bots; ones lasting 4+ blocks are realistic.

## Safety

- **Scan mode is free and read-only.** No wallet, no contract, no transactions.
- **Nothing is sent without three gates:** the exact quote check, a full
  `eth_call` simulation of the real transaction (simulate mode), and an
  on-chain `minProfit` check inside the contract.
- **Losses are bounded to gas** on reverted attempts: a few cents each on
  Arbitrum, capped by `max_daily_gas_usd`. The bot halts after
  `max_consecutive_reverts`.
- **The contract** is owner-only, holds no funds (profit is sent out in the
  same transaction), and only accepts the exact flash loan it requested. It has
  not been professionally audited; `selftest` exercises it for free before use.
- **Keys:** use a brand-new wallet holding a few dollars of ETH for gas. Keys
  live only in `.env` (git-ignored) and are masked in all log output.

## How it decides

```
every block (~0.25s):
  one Multicall3 request (pinned to that block) reads the pools on any route
  price every token in USD via its deepest real-liquidity pool to a stablecoin
  pre-filter: sum each route's per-hop log rates (~5,000 routes in ~10ms)
  for routes with an edge after pool fees:
      compose the swaps into out(x) = A·x / (B + C·x)
      optimal size x* = (√(A·B) − B) / C, capped by pool balances,
          Balancer's balance and max_loan_usd
      keep if profit − gas ≥ min_profit_usd
  scan mode:     re-price exactly with Quoter contracts; log verified/rejected;
                 time how many blocks each verified gap stays open
  simulate mode: eth_call the real contract (retry at ½ and ¼ size)
  live mode:     send; the contract enforces min profit on-chain
```

| File | Purpose |
|---|---|
| `contracts/FlashArbitrage.sol` | Flash loan + swaps + profit check in one transaction (Uniswap V2/V3, Camelot V2, Algebra routers) |
| `flasharb/amm.py` | Pool model: V2 exact math, V3/Algebra virtual reserves, per-direction fees, real-balance caps |
| `flasharb/routes.py` | Cycle search, closed-form optimal sizing, USD pricing |
| `flasharb/chain.py` | web3 + Multicall3: pool discovery, per-block refresh, exact quotes |
| `flasharb/discovery.py` | Long-tail token discovery from V2 factory pair lists |
| `flasharb/executor.py` | Simulates (`estimate_gas`) and sends transactions |
| `flasharb/bot.py` | Main loop, gap tracking, stats and summaries |
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

Create a free **Arbitrum Mainnet** app at Alchemy (or Infura/QuickNode) and put
its URL in `.env`:

```
RPC_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
```

The public `https://arb1.arbitrum.io/rpc` works for a quick look but is
rate-limited to roughly 1 in 10 blocks.

## Stage 1: scan (free, no wallet)

```bash
caffeinate -dis python3 run_flash.py run 2>&1 | tee logs/run.log   # macOS: keep awake + save output
```

Startup verifies every token on-chain, discovers long-tail tokens (a few
minutes the first time, cached for a day) and finds every pool. Each minute:

```
mode=scan blocks=240 (saw 88% of chain blocks) routes=4964 candidates=3 slowest_block_eval=9ms
  closest this period: best edge after pool fees +0.0829% (USDC -[uniswap_v3/0.05%]-> ARB -[camelot_v3]-> USDC)
  best trade after gas (estimate) $+0.06 (...); needs >= $0.05
    size $280, profit $0.082 - gas $0.024
  exact quotes: verified=1 rejected=2; best $0.68 (...)
  verified gaps closed: 1; gone within 2 blocks: 1; lasted 4+ blocks (1s+): 0; median 1 blocks
  verified total since start $0.68 ($0.07/hr if the bot had won every one; it wouldn't)
```

**Trust the `exact quotes`, `verified gaps` and `verified total` lines.** The
estimate lines only show how close the fast pass came. Logs:

- `logs/opportunities.csv`: every candidate and whether the exact quote confirmed it
- `logs/gaps.csv`: every confirmed gap and how many blocks it stayed open

For measurement, a low `min_profit_usd` (e.g. `0.05`) in scan mode captures
the small gaps too; it costs nothing.

## Stage 2: deploy, self-test, simulate

Only worth doing if scan mode shows confirmed gaps that last several blocks.

1. Make a **new** wallet with ~$3–5 of ETH on Arbitrum One and add
   `PRIVATE_KEY=0x...` to `.env`.
2. `python3 run_flash.py deploy` compiles and deploys the contract (well under
   $1); copy the printed `contract_address` into `config.toml`.
3. `python3 run_flash.py selftest` checks every address and dry-runs a flash
   loan through each DEX for free. Continue only on `SELF-TEST PASSED`.
4. Set `mode = "simulate"`. Every candidate now runs through the real contract
   against the live chain, and the summary reports **would-have-made $/hr**
   (an upper bound: it assumes nobody beat you to it). This also rules out
   transfer-tax tokens, which fool price quotes.

## Stage 3: live

Only if simulate mode shows meaningful profit over several days: set
`mode = "live"` and run `python3 run_flash.py run --live`. Both are required.
Watch `logs/trades.csv` and the `succeeded` / `reverted` counts.

## Configuration

- **Tokens** (`[tokens]`): 35 core Arbitrum tokens covering stablecoins,
  BTC wrappers, liquid-staked ETH, the Arbitrum ecosystem and DeFi. Each is
  verified on-chain at startup; bad addresses are skipped, symbol mismatches
  flagged.
- **Discovery** (`[discovery]`): reads every pair the V2-style factories list,
  keeps up to `max_tokens` tokens with at least `min_liquidity_usd` of real
  WETH/stablecoin liquidity, and pairs them with the core tokens across all
  DEXes. Gaps involving these are flagged, because some charge a hidden
  transfer tax that only simulate mode reveals.
- **DEXes** (`[dexes.*]`): any Uniswap V2 fork, Uniswap V3 fork, Camelot-style
  V2 or Algebra-based V3; set `type`, `router_kind` and (for concentrated
  liquidity) `quoter`.
- **Risk** (`[risk]`): `min_profit_usd`, `min_pool_liquidity_usd`,
  `max_loan_usd`, gas price cap, daily reverted-gas budget, consecutive-revert
  halt.
- **Other EVM chains:** change `chain_id`, addresses and RPC. The Balancer
  vault and Multicall3 share addresses across most chains. Avoid Ethereum
  mainnet: gas is high and its public mempool invites front-running.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

41 offline tests cover pool math (V2, V3, directional fees, real-balance caps),
cycle search and rotation de-duplication, optimal sizing, USD pricing,
discovery and caching, gap-lifetime tracking, RPC error handling with key
masking, and the full scan/simulate/live decision flow against a fake chain.
No network or packages needed.
