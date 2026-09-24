# Crypto Arbitrage Bot

A cross-exchange price arbitrage bot. It watches the same trading pair (e.g.
`BTC/USDT`) on several exchanges and, when one exchange's ask is below
another's bid by more than fees plus a safety buffer, buys on the cheap
exchange and sells on the expensive one at the same time.

## Read this first

- **No profit is guaranteed.** Price gaps between major exchanges are usually
  smaller than taker fees (0.1%–0.6% per side) and are closed within
  milliseconds by professional firms with co-located servers and VIP fee
  tiers. Expect the bot to find few or no profitable trades at retail fee
  levels. Run `scan` mode first to measure what's actually available to *you*.
- **You need inventory on every exchange.** Each trade spends quote currency
  (USDT) on one exchange and sells coins you already hold on another. Over
  time, inventory drifts to one side and the bot stops until you rebalance by
  hand (withdrawal fees apply). The bot does not move funds between exchanges.
- **Leg risk.** If one order fills and the other doesn't, you hold an
  unhedged position. The bot halts immediately when this happens.
- Check that each exchange is legal and available where you live, and keep
  records for taxes. Every trade is a taxable event in many countries.

## How it works

```
stream order books from every exchange (websocket) and wake on any change
  -> drop stale books (> max_book_age_s)
  -> for every (buy exchange, sell exchange) pair:
       size the trade from balances and max_trade_quote
       walk the order book depth to get real average fill prices
       subtract taker fees on both legs and a slippage buffer
  -> best opportunity -> risk checks -> execute both legs at once
  -> journal to logs/opportunities.csv and logs/trades.csv
```

| File | Purpose |
|---|---|
| `arbbot/orderbook.py` | Order book model; simulates fills across multiple price levels |
| `arbbot/strategy.py` | Opportunity pricing (fees, depth, buffer) and position sizing |
| `arbbot/risk.py` | Profit thresholds, stale-data guard, daily loss/trade caps, failure halt |
| `arbbot/executor.py` | `PaperExecutor` (simulated) and `LiveExecutor` (IOC limit orders via ccxt) |
| `arbbot/market_data.py` | ccxt connectivity, streaming order books, fees, balances, precision rounding |
| `arbbot/bot.py` | Main loop and running stats |
| `arbbot/journal.py` | CSV audit logs |

### Price feed

`price_feed = "websocket"` (the default) keeps a live order book for every
exchange and symbol in memory through `ccxt.pro`, which ships with `ccxt`.
The bot reacts as soon as any book changes, typically within milliseconds,
instead of re-downloading every book once a second. Details:

- Updates are batched to at most one evaluation per `min_cycle_interval_s`
  (default 50ms) so a busy market can't peg the CPU.
- Dropped connections reconnect with exponential backoff (1s up to 30s). A
  feed's cached book is discarded the moment its connection errors, so the bot
  never trades on a dead connection's prices.
- Exchanges without websocket order books are polled over REST in the
  background automatically.
- Balances are REST-only, so they're cached and re-read every
  `balance_refresh_s` and after every trade attempt.
- The minute summary shows `avg_price_age`, the average age of the prices the
  bot was deciding on. Compare it against `price_feed = "rest"` to see the
  speed difference on your connection.
- A book only counts as fresh if it updated within `max_book_age_s`. On a
  quiet pair that rarely changes, raise that value or the pair will be skipped.

Live orders are **immediate-or-cancel limit orders** priced at the deepest
book level the simulation used, so a leg can never fill worse than priced. If
the market moves, the order cancels instead of chasing the price.

## Setup

```bash
cd crypto_arbitrage_bot
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp config.example.toml config.toml
```

## Run it in three stages

**1. Scan (no account needed).** Leave `mode = "scan"` and run for a few
hours or days:

```bash
python run_bot.py --config config.toml
```

The summary every minute shows, per symbol, how many scans found a
net-profitable gap and the best net % seen. If `profitable` stays at 0,
arbitrage isn't viable at your fee tier on these exchanges. Try other
exchanges/symbols or enter your real (lower) fees via `taker_fee`.

**2. Paper trade.** Set `mode = "paper"` and fill `[paper_balances.*]` with the
inventory you'd actually deposit. This trades against live order books with
fake money and reports P&L and P&L per hour.

**3. Live (real money).** Only after paper trading looks good for a
meaningful period:

- Create API keys with **trade permission only, no withdrawal permission**,
  and IP-restrict them if the exchange allows it.
- Export them as env vars, e.g. `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`
  (`<ID>_API_PASSWORD` for exchanges that need a passphrase).
- Set `mode = "live"`, start with a small `max_trade_quote` and a tight
  `max_daily_loss_quote`, then run:

```bash
python run_bot.py --config config.toml --live
```

Both `mode = "live"` and `--live` are required; either one alone refuses to trade.

## Tests

```bash
python -m unittest discover -s tests -t .
```

The tests cover fill simulation, fee math, sizing, risk limits and full
paper-trading cycles. They need no network or third-party packages.

## Ideas for later

- Triangular arbitrage within a single exchange (no inventory split needed)
- Automatic rebalancing alerts when inventory drifts
