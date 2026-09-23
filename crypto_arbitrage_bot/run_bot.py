"""Command-line entry point.

    python run_bot.py --config config.toml            # uses the mode in the config
    python run_bot.py --config config.toml --cycles 60
    python run_bot.py --config config.toml --live     # required in addition to mode = "live"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from arbbot.bot import ArbitrageBot
from arbbot.config import load_config
from arbbot.executor import LiveExecutor, PaperExecutor
from arbbot.journal import Journal
from arbbot.market_data import ExchangeHub
from arbbot.risk import RiskManager


async def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-exchange crypto arbitrage bot")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    parser.add_argument("--live", action="store_true",
                        help="confirm real-money trading (config must also say mode = \"live\")")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if cfg.mode == "live" and not args.live:
        print('Config says mode = "live" but --live was not passed. Refusing to trade.')
        return 2
    if args.live and cfg.mode != "live":
        print(f'--live passed but config mode is "{cfg.mode}". Refusing to trade.')
        return 2

    hub = ExchangeHub(cfg.exchanges, use_credentials=cfg.mode == "live")
    try:
        await hub.load(cfg.symbols)
        executor = {
            "scan": None,
            "paper": PaperExecutor(cfg.paper_balances),
            "live": LiveExecutor(hub),
        }[cfg.mode]
        bot = ArbitrageBot(cfg, hub, executor, RiskManager(cfg.risk), Journal(cfg.log_dir))
        await bot.run(max_cycles=args.cycles)
    finally:
        await hub.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("stopped")
