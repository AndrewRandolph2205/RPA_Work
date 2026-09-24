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
every block (~0.25s on Arbitrum), announced by the sequencer feed:
  one Multicall3 request, pinned to that block, reads every pool + Balancer's balances
  price every token in USD via its deepest pool to a stablecoin
  for each 2- and 3-hop cycle through pools deep enough on BOTH sides:
      compose the swaps into out(x) = A·x / (B + C·x)
      best size x* = (√(A·B/(1+f)) − B) / C  (f = Balancer's flash-loan fee, read at startup)
      keep it if profit − flash fee − estimated gas ≥ min_profit_usd
  scan: check the best candidates exactly at the SAME block (RouteSimulator, below)
        and log why any estimate missed; when a verified gap closes, find who closed it
  simulate: dry-run the real transaction through your deployed contract (eth_call)
  live: send at once, no dry run; the contract reverts unless the profit is there
  paper: decide exactly as live does, but place a paper order and settle it at the
         block it would have landed in
```

| File | Purpose |
|---|---|
| `contracts/FlashArbitrage.sol` | Flash loan + swaps + profit check, in one transaction |
| `contracts/RouteSimulator.sol` | Never deployed: injected into eth_calls to test a whole route for real |
| `flasharb/amm.py` | Uniswap V2 exact math; V3 via virtual reserves |
| `flasharb/routes.py` | Cycle search, optimal sizing, USD pricing |
| `flasharb/chain.py` | Pool discovery, per-block state, exact checks, history lookups |
| `flasharb/feed.py` | Arbitrum sequencer feed client (block announcements, express-lane stats) |
| `flasharb/simulator.py` | RouteSimulator encoding, and diagnosis of why an estimate missed |
| `flasharb/closers.py` | Finds the transaction that closed each verified gap |
| `flasharb/executor.py` | Simulates and sends transactions (fast path, receipt watcher) |
| `flasharb/paper.py` | Paper trading: when each trade would have landed, and what it would have made |
| `flasharb/bot.py` | Main loop, stats, journaling |
| `flasharb/risk.py` | Gas caps, revert budget, halt conditions |
| `run_flash.py` | CLI: `deploy`, `selftest`, `run` (`--paper`, `--live`), `trace-gaps`, `build-simulator` |

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
python3 run_flash.py selftest   # optional: checks the RPC, pools and exact checks
python3 run_flash.py run
```

It discovers the pools, then follows every block. The fast first pass treats
each Uniswap V3 pool as if its liquidity at the current price went on forever,
which can make pools look far better than they are. So every candidate is
then checked **exactly, at the same block the estimate used**: one `eth_call`
that places `RouteSimulator` at an unused address (a state override; nothing
is deployed), flash-borrows from Balancer, swaps through the real routers and
reports what arrived after every hop. That includes real fees, liquidity
depth, transfer taxes and the flash-loan fee. (If your RPC doesn't allow state
overrides, the bot falls back to the dexes' quoter contracts and says so.)

Because the check runs at the estimate's own block, a rejection is always the
model's error, and the bot says which hop and why:

- **depth**: a concentrated-liquidity pool matched at a tiny size but ran out
  of liquidity at full size;
- **fee/price**: the pool charged a different fee or price even at a tiny size
  (Camelot V3 recomputes its dynamic fee on the first swap of each block);
- **transfer tax**: a token arrived short. The token is dropped from all
  routes and remembered in `logs/excluded_tokens.json` (delete a line there to
  undo).

The minute summary looks like:

```
mode=scan blocks=240 (saw 100% of chain blocks) source=feed routes=2904 candidates=52 slowest_block_eval=7ms
  feed: connected; pools read 60ms after the feed announced each block (median; p90 120ms); express-lane txs in 20% of blocks
  best trade after gas (estimate) $+0.12 (...), marginal edge +0.050%; needs >= $0.50
    size $900, profit $0.150 - flash fee $0.000 (0.0 bps) - gas $0.024
  exact checks (at the estimate's own block): verified=1 rejected=3; best $0.61 (...); slowest check 90ms
    why rejected estimates missed (since start): depth @ uniswap_v3 2, dynamic fee/price @ camelot_v3 1
  verified gaps closed: 1; gone within 2 blocks: 1; lasted 4+ blocks (1s+): 0; median 1 blocks
  closers found: 1 of 1 gaps (express lane 1, regular 0); arbitrage bots 1; landed in the very next block 1 (1 among its first 2 transactions)
```

Trust the `exact checks` line. The estimate lines show how close the fast pass
came; "best trade" is ranked by dollars at the best size, so a thin pool with
a huge percentage edge can't crowd out a real one.

**Who closes the gaps?** When a verified gap disappears, the bot looks up the
first transaction through the route's pools between the last block it saw the
gap and the first block it saw it gone, and writes it to `logs/closers.csv`:
block, position in the block, sender, contract, and whether it came through
Timeboost's **express lane** (Arbitrum receipts say so). Mostly express lane
means you're up against the auction winner; mostly regular transactions in the
very next block means it's a latency race. For gaps logged before this
existed, run `python3 run_flash.py trace-gaps`.

**The sequencer feed** (`sequencer_feed_url`) replaces polling, which missed
about one block in seven. The feed announces each block before any RPC can
serve it; the bot then reads the pools pinned to that block, retrying for a
moment until your RPC has it. The summary shows how far behind the feed your
RPC is: that delay is what a node of your own would remove.

## Paper trading (free, no wallet)

Paper mode makes the decisions live mode would (same candidates, same one
trade in flight at a time, same risk limits) but places a **paper order**
instead of sending a transaction, then settles it against the real chain:

```bash
python3 run_flash.py run --paper      # or set mode = "paper" in config.toml
```

For each order it works out:

1. **When it would have landed.** The bot's own decision time after the
   sequencer feed announced the block, plus `paper_send_latency_ms` (the trip to
   the sequencer), counted in 250ms blocks. While someone controls Timeboost's
   express lane, every other transaction is also held back 200ms
   (`paper_timeboost_delay_ms`); with no controller, Arbitrum is
   first-come-first-served and nothing is held. With `paper_timeboost = "auto"`
   the bot adds the hold only when the feed has shown an express-lane
   transaction within the last auction round (240 blocks, one minute), or when
   it can't tell yet (just started, no feed, or no block metadata from the
   feed). `"on"` and `"off"` force it. Decide 120ms after block N appeared: with
   the lane idle the trade lands in N+1; with it in use, N+2. That delay is
   timed from when the bot heard about the block, so a bot that has fallen
   behind (a feed backlog in a busy moment) would look fast. The RPC's latest
   block at the decision catches that: a trade can't land in a block that
   already exists, so it lands after that head.
2. **Whether it paid.** Once the chain has that block, the exact check scan mode
   uses runs the trade on the state it would have met: the end of the previous
   block. The contract reverts unless the profit covers `minProfit`, so anything
   less is a revert that still costs gas.
3. **Whether someone beat it inside its block.** If the gap still paid going into
   the landing block but was gone by its end, another transaction in that block
   took it. The order within a block can't be known, so it counts as a loss
   (`paper_same_block_wins = true` counts it as a win).

Every order is also checked at the block it was spotted, so each row shows what
a bot with no delay would have made and what latency cost. Nothing is signed or
sent, and no contract or private key is needed.

Live mode's risk limits apply: the daily reverted-gas budget stops paper orders
for the day, and when `max_consecutive_reverts` would have halted live mode,
paper mode logs it and keeps going. Settlement runs on a background thread, so
decisions come as fast as they would live. With `presimulate_live = true`, paper
mode runs the same dry run first (an exact check at the spotted block).

The summary adds lines like these:

```
  paper orders=14 filled=3 reverted=9 lost_race=2 pending=1; fill rate 21%
    net $-0.07 ($-0.03/hr) = profit $0.38 - gas $0.45; the same trades with no delay: $+1.20 (latency cost $1.27)
    median delay 230ms (decision 180ms + send 50ms, plus Timeboost's 200ms on 0 of 14 orders; express lane idle 14): landed 1 block(s) after the gap was spotted
    bot behind the chain when ordering: median 0 block(s), worst 3 (RPC's latest block vs the block traded on)
    why paper trades didn't fill (since start): closed before landing 6, estimate was off 3, lost in landing block 2
    live mode would have halted 2x (3 reverts in a row); paper mode kept going
```

Each settled order is one row in `logs/paper_trades.csv`:

| Columns | Meaning |
|---|---|
| `paper_id`, `status`, `cause` | order number; `filled`, `reverted`, `lost_race` or `check_failed`; short reason |
| `route`, `pools`, `amount_in`, `amount_in_usd` | what would have been traded, and the flash-loan size |
| `decided_at`, `detect_block` | when the bot decided, and the block the gap was spotted on |
| `chain_head_block`, `blocks_behind` | the RPC's latest block at the decision, and how far past `detect_block` it was (the bot running behind the chain); the trade lands after it |
| `decision_ms`, `latency_source` | block announced to decision; `feed` = measured, `poll` = a floor (no feed) |
| `send_latency_ms`, `timeboost_delay_ms`, `express_lane`, `total_delay_ms` | the assumed trip; the Timeboost hold applied (0 while the lane was idle); the lane's state (`active`, `idle`, `unknown`, or `forced on`/`forced off` by config); the whole delay |
| `landing_block`, `blocks_late` | the block it would have landed in, and how many blocks after `detect_block` |
| `est_profit_usd`, `est_flash_fee_usd`, `est_gas_usd`, `est_net_usd` | the fast estimate the decision was based on |
| `min_profit_usd` | the contract's floor for this trade (estimated gas + `min_profit_usd`) |
| `exact_profit_at_detect_usd`, `exact_profit_at_landing_usd` | exact profit after the flash-loan fee, at the spotted block and on the state it landed on |
| `open_after_landing` | whether the gap still paid after the landing block (nobody else took it there) |
| `amount_out`, `profit_usd`, `flash_fee_usd` | what came back, and the profit sent to the wallet (filled only) |
| `gas_units`, `gas_price_gwei`, `gas_usd` | gas paid, filled or reverted |
| `net_usd` | profit minus gas if filled, minus gas otherwise |
| `zero_delay_net_usd`, `latency_cost_usd` | what a bot with no delay would have netted, and the difference |
| `check_method`, `reason` | `sim` (RouteSimulator) or `quoter`; the full explanation |
| `cum_orders`, `cum_settled`, `cum_filled`, `cum_net_usd`, `cum_gas_usd`, `fill_rate` | running totals |

Set `paper_send_latency_ms` to your own number: time a request to `send_rpc_url`
from the machine that runs the bot and halve it. From a home connection that's
usually tens of milliseconds; from a server near the sequencer, a few.

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

Live sends take the short path: signed locally (nonce tracked by the bot,
fixed `live_gas_limit`, cached gas price), sent in one request straight to the
sequencer (`send_rpc_url`), with no `eth_call` dry run first. If the gap is
gone when the transaction lands, the contract's profit check reverts it and
only that transaction's gas is lost (a few cents). Receipts are collected in
the background, and only one transaction is in flight at a time. If scan mode
shows many rejected estimates, set `presimulate_live = true` to dry-run each
trade first (slower, but fewer reverts).

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
  fake gaps. The exact check swaps for real, so it catches them and drops the
  token from all routes. The first run takes a few minutes and is cached for a day.
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

They cover the pool math, cycle search, sizing (with the flash-loan fee), USD
pricing, the full scan/paper/simulate/live decision flow, paper-trade settlement, estimate diagnosis, closer
tracing, the sequencer feed and the live fast path, against a fake chain. They
need no network and no packages.
