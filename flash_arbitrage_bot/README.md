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
| `contracts/FlashArbitrage.sol` | Flash loan + swaps + profit check in one transaction (Uniswap V2/V3, Camelot V2, Algebra routers) |
| `flasharb/amm.py` | Pool model: V2 exact math, V3/Algebra virtual reserves, per-direction fees, real-balance caps |
| `flasharb/routes.py` | Cycle search, closed-form optimal sizing, USD pricing |
| `flasharb/chain.py` | web3 + Multicall3: pool discovery, per-block refresh, exact quotes |
| `flasharb/discovery.py` | Long-tail token discovery from V2 factory pair lists |
| `flasharb/executor.py` | Simulates (`estimate_gas`) and sends transactions |
| `flasharb/bot.py` | Main loop, gap tracking, stats and summaries |
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

Create a free **Arbitrum Mainnet** app at Alchemy (or Infura/QuickNode) and put
its URL in `.env`:

```
RPC_URL=https://arb-mainnet.g.alchemy.com/v2/YOUR_KEY
```

The public `https://arb1.arbitrum.io/rpc` works for a quick look but is
rate-limited to roughly 1 in 10 blocks.

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
  feed: connected; pools ready 60ms after the feed announced each block (median; p90 120ms); state: 236 blocks from pool events, 4 RPC reads (1 resyncs, 3 event timeouts), last resync drift 0/1400 pools; express-lane txs in 20% of blocks
  best trade after gas (estimate) $+0.12 (...), marginal edge +0.050%; needs >= $0.50
    size $900, profit $0.150 - flash fee $0.000 (0.0 bps) - gas $0.024
  exact checks (at the estimate's own block): verified=1 rejected=3; best $0.61 (...); slowest check 90ms
    why rejected estimates missed (since start): depth @ uniswap_v3 2, dynamic fee/price @ camelot_v3 1
  verified gaps closed: 1; gone within 2 blocks: 1; lasted 4+ blocks (1s+): 0; median 1 blocks
  closers found: 1 of 1 gaps (express lane 1, regular 0); arbitrage bots 1; landed in the very next block 1 (1 among its first 2 transactions)
```

**Pool state from events (`state_source = "logs"`).** Instead of re-reading
every tracked pool each block (one Multicall round trip, retried while the RPC
catches up with the feed), the bot subscribes over a websocket to the pools'
own events (V2 `Sync`, V3/Algebra `Swap`/`Mint`/`Burn`, dynamic-fee changes)
and applies each block's events as soon as the node pushes them. It re-reads
all pools after every reconnect and every `logs_resync_s`, and the summary's
`drift` shows how many pools the events had gotten wrong by then (it should
stay at 0). `pools ready ... after the feed` is the number to compare against
`state_source = "rpc"`. The websocket URL comes from `WS_RPC_URL`, or from
`RPC_URL` with `https://` swapped for `wss://`.

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
- **Other EVM chains:** see [Other chains](#other-chains-base-and-beyond) below.
Only if simulate mode shows meaningful profit over several days: set
`mode = "live"` and run `python3 run_flash.py run --live`. Both are required.
Watch `logs/trades.csv` and the `succeeded` / `reverted` counts.

## Other chains (Base and beyond)

On Arbitrum, paper mode showed who takes the gaps: in 13 of 14 lost races
the winner was the **first or second transaction** of the next block, ahead
of 15–37 others. Those bots react within a few milliseconds; a bot ~90ms
behind (this one on a server near the sequencer) never gets there first.
Other chains decide the race differently, so the bot now runs on them too.

**Base** (`config.base.example.toml`):

- **Ordering by fee, not arrival.** Base's sequencer puts the highest priority
  fee first within a block (`ordering = "fee"`), so trades are won by bidding.
  The bot bids `priority_fee_share` of each trade's expected profit above
  `min_profit_usd`. Paper mode compares that bid with what the transaction
  that actually took the gap paid and records both (`our_tip_gwei`,
  `winner_tip_gwei`); scan mode logs every closer's priority fee.
  Base also builds each 2s block in 200ms "flashblocks", and a competitor a
  whole flashblock earlier wins whatever it bids, so paper results there are
  an upper bound.
- **Aerodrome**, Base's largest DEX, as `type = "solidly"` (Solidly /
  Velodrome V2 volatile pools; fee read from the factory). Its concentrated
  "Slipstream" pools and PancakeSwap V3 aren't supported yet.
- **No sequencer feed**: with `state_source = "logs"`, blocks are announced by
  the node's own `newHeads` over the same websocket that streams pool events.
- **L1 data fee**: every OP-stack transaction pays one (`extra_tx_cost_usd`).

Try it in scan mode first. It's free, and it shows whether gaps last longer
and whether closers pay high fees:

```bash
cp config.base.example.toml config.base.toml
# add to .env: BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/<key>
# (enable Base for the key in Alchemy's dashboard)
python3 run_flash.py selftest --config config.base.toml
python3 run_flash.py run --config config.base.toml
```

Results go to `logs_base/`. The summary's `closers found` line gives each
closer's block position and priority fee, and `gaps.csv` gives gap lifetimes.
Gaps that often last 2+ blocks, or closers bidding small fees, mean room for a
small player. If gaps close in the next block to large bids, Base is as
crowded as Arbitrum. Live mode on Base needs its own deployment of
`FlashArbitrage` (`deploy --config config.base.toml`).

**Optimism** (`config.optimism.example.toml`) is set up the same way: Uniswap
V2/V3 and Velodrome (Aerodrome's original, also `type = "solidly"`), results
in `logs_optimism/`, RPC from `OP_RPC_URL`. It has less trading than Base and
probably fewer bots; a day of scan mode on each compares them.

**Polygon PoS** (`config.polygon.example.toml`): QuickSwap V2/V3, Uniswap V3
and Sushi, results in `logs_polygon/`, RPC from `POLYGON_RPC_URL`. QuickSwap V3
runs the original Algebra (`type = "algebra_v1"`: one dynamic fee for both
directions). Gas is paid in POL, so `native_wrapped = "WPOL"`. Two things set it
apart:

- **A public mempool.** Other bots see a pending trade and can copy it with a
  higher priority fee. The flash-loan contract reverts unless it profits, so a
  copied trade costs only its gas (a fraction of a cent on Polygon), never the
  loan. For live trading, point `send_rpc_url` at a private transaction relay.
- **A minimum tip.** Validators drop transactions tipping under ~25–30 gwei,
  so bids never go below `min_priority_fee_gwei`.

**Adding another EVM chain** (newer chains have fewer bots, for a while):

1. Copy a config and set `chain_id`, `chain_name`, `log_dir`, the tokens and
   the DEXes (any Uniswap V2/V3 fork, Camelot V2, Algebra or Solidly/Velodrome
   V2 pools). Balancer V2's vault and Multicall3 have the same address on most
   chains; check the vault holds the tokens you want to borrow.
2. Set `ordering`: `"arrival"` for first-come-first-served sequencers
   (Arbitrum), `"fee"` for priority-fee ordering (OP-stack chains, most L1s).
   Set `paper_block_time_ms` to the chain's block time, `paper_timeboost = "off"`
   off Arbitrum, and `sequencer_feed_url = ""` unless the chain has one.
3. `selftest`, then a day of scan mode, then paper mode.

Avoid Ethereum mainnet: gas is far higher, and its public mempool lets other
bots copy or sandwich your transactions.

**Not built: liquidations and liquidity provision.** Liquidating unhealthy
loans on lending markets is another speed race against the same kind of firms.
Providing liquidity (earning pool fees) is not a race, but it needs your own
capital at risk from price moves, and returns scale with that capital rather
than with the software. Both are separate products with their own risks, not
settings of this bot.

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
  V2, Algebra-based V3 (Camelot's `algebra` or the original `algebra_v1`, e.g.
  QuickSwap V3) or Solidly/Velodrome V2 (Aerodrome); set `type`,
  `router_kind` and (for concentrated liquidity) `quoter`.
- **Risk** (`[risk]`): `min_profit_usd`, `min_pool_liquidity_usd`,
  `max_loan_usd`, gas price cap, daily reverted-gas budget, consecutive-revert
  halt.
- **Ordering** (`ordering`, `priority_fee_share`, `extra_tx_cost_usd`): how the
  chain orders transactions within a block, the bid on fee-ordered chains,
  and any fixed per-transaction cost such as an L1 data fee.
- **Other EVM chains:** see [Other chains](#other-chains-base-and-beyond).

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

They cover the pool math, cycle search, sizing (with the flash-loan fee), USD
pricing, the full scan/paper/simulate/live decision flow, paper-trade settlement, estimate diagnosis, closer
tracing, the sequencer feed and the live fast path, against a fake chain. They
need no network and no packages.
41 offline tests cover pool math (V2, V3, directional fees, real-balance caps),
cycle search and rotation de-duplication, optimal sizing, USD pricing,
discovery and caching, gap-lifetime tracking, RPC error handling with key
masking, and the full scan/simulate/live decision flow against a fake chain.
No network or packages needed.
