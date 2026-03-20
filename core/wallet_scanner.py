import asyncio
import os
import json
import logging
import aiohttp
import time
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

class WalletScanner:
    def __init__(self):
        # /trades works publicly and returns live BTC trades with wallet addresses.
        # /activity?user=<addr> returns per-wallet TRADE/REDEEM events.
        # A REDEEM event = winning position redeemed (win). We derive win rate from that.
        self.trades_url = "https://data-api.polymarket.com/trades"
        self.activity_url = "https://data-api.polymarket.com/activity"
        self.cache_file = "wallets_cache.json"
        self.min_win_rate = float(os.getenv("MIN_WIN_RATE", 0.60))
        self.min_trades = int(os.getenv("MIN_TRADES_TO_QUALIFY", 20))
        self.top_wallet = None
        self.leaderboard = []
        # Load cached leaderboard immediately so the copy engine has wallets
        # available from the very first second — before the first rescan completes.
        self._load_cache()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def _load_cache(self):
        """Load the leaderboard from disk cache on startup (sync, no await needed)."""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file) as f:
                    data = json.load(f)
                self.leaderboard = data.get("leaderboard", [])
                if self.leaderboard:
                    self.top_wallet = self.leaderboard[0]["address"]
                    logging.info(
                        f"WalletScanner: loaded {len(self.leaderboard)} wallets from cache. "
                        f"Top: {self.top_wallet[:10]}… ({self.leaderboard[0]['win_rate']:.0%} WR)"
                    )
        except Exception as e:
            logging.warning(f"WalletScanner: could not load cache: {e}")

    async def fetch_recent_btc_trades(self, limit: int = 500) -> List[Dict]:
        """
        Fetch recent public trades via /trades (no auth required).
        Returns records that include proxyWallet, title, conditionId, side, size, price.
        """
        url = f"{self.trades_url}?limit={limit}"
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        trades = await response.json()
                        # Filter for BTC 5-minute markets only — NOT monthly/weekly BTC markets
                        return [
                            t for t in trades
                            if re.match(r"btc-updown-5m-\d+", t.get("slug", ""))
                        ]
                    else:
                        logging.error(f"Trades API error: {response.status} at {url}")
                        return []
        except Exception as e:
            logging.error(f"Failed to fetch trades: {e}")
            return []

    async def fetch_wallet_activity(self, address: str, limit: int = 200) -> List[Dict]:
        """
        Fetch activity for a specific wallet (/activity requires ?user=<addr>).
        Returns TRADE and REDEEM events. REDEEM = winning position cashed out.
        """
        url = f"{self.activity_url}?user={address}&limit={limit}"
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        return []
        except Exception as e:
            logging.error(f"Failed to fetch activity for {address}: {e}")
            return []

    async def run_loop(self):
        """Background task to rescan wallets periodically."""
        interval = int(os.getenv("RESCAN_INTERVAL_MINUTES", 60)) * 60
        while True:
            logging.info("Starting wallet rescan via Data API...")
            await self.rescan_wallets()
            logging.info(f"Rescan complete. Next scan in {interval/60:.0f} minutes.")
            await asyncio.sleep(interval)

    async def rescan_wallets(self):
        """
        1. Pull recent BTC trades to discover active wallet addresses.
        2. For each candidate wallet fetch their full activity.
        3. Compute win rate: wins = markets where they had a REDEEM event,
           total = unique BTC conditionIds they traded BUY side.
        4. Rank and cache top wallets.
        """
        cutoff = time.time() - (30 * 86400)  # 30-day lookback

        # Step 1: discover candidate wallets from recent BTC trades
        btc_trades = await self.fetch_recent_btc_trades(limit=500)
        if not btc_trades:
            logging.warning("No BTC trades found; skipping wallet rescan.")
            return

        candidate_wallets = list({t["proxyWallet"] for t in btc_trades if t.get("proxyWallet")})
        logging.info(f"Evaluating {len(candidate_wallets)} wallets from recent BTC trades.")

        # Step 2 & 3: fetch per-wallet activity and compute stats concurrently
        tasks = [self._evaluate_wallet(addr, cutoff) for addr in candidate_wallets]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        stats = {}
        for addr, result in zip(candidate_wallets, results):
            if isinstance(result, dict):
                stats[addr] = result

        # Step 4: filter, rank, cache
        qualified = self.filter_wallets(stats)
        self.leaderboard = sorted(qualified, key=lambda x: x["win_rate"], reverse=True)

        if self.leaderboard:
            self.top_wallet = self.leaderboard[0]["address"]
            logging.info(
                f"Top wallet: {self.top_wallet} "
                f"(Win Rate: {self.leaderboard[0]['win_rate']:.1%}, "
                f"Trades: {self.leaderboard[0]['total_trades']})"
            )
        else:
            self.top_wallet = None
            logging.warning("No wallet met the 80% win rate threshold in recent BTC activity.")

        with open(self.cache_file, "w") as f:
            json.dump({"saved_at": time.time(), "leaderboard": self.leaderboard[:10]}, f, indent=2)

    async def _evaluate_wallet(self, address: str, cutoff: float) -> Dict:
        """
        Fetch activity for one wallet and return computed stats dict,
        or an empty dict if the wallet doesn't qualify for evaluation.
        """
        activity = await self.fetch_wallet_activity(address, limit=200)
        if not activity:
            return {}

        # Only consider BTC 5-MINUTE market events within the lookback window.
        # Filtering by slug pattern (btc-updown-5m-*) ensures we don't count wins
        # from long-duration BTC markets (monthly/weekly) which inflate win rates.
        btc_activity = [
            a for a in activity
            if self.is_btc_5m_market(a.get("title", ""), a.get("slug", ""))
            and float(a.get("timestamp", 0)) >= cutoff
        ]
        if not btc_activity:
            return {}

        # Count distinct conditionIds where the wallet placed a BUY (entered a position)
        bought_markets = {
            a["conditionId"]
            for a in btc_activity
            if a.get("type") == "TRADE" and a.get("side", "").upper() == "BUY"
            and a.get("conditionId")
        }
        # Count distinct conditionIds where the wallet redeemed (won)
        won_markets = {
            a["conditionId"]
            for a in btc_activity
            if a.get("type") == "REDEEM" and a.get("conditionId")
        }

        total = len(bought_markets)
        wins = len(won_markets & bought_markets)  # only count wins on markets they bought into
        if total == 0:
            return {}

        last_active = max(float(a.get("timestamp", 0)) for a in btc_activity)
        sizes = [float(a.get("usdcSize") or a.get("size") or 0)
                 for a in btc_activity if a.get("type") == "TRADE"]

        return {
            "total": total,
            "wins": wins,
            "last_active": last_active,
            "sizes": [s for s in sizes if s > 0],
        }

    def is_btc_5m_market(self, text: str, slug: str = "") -> bool:
        """Only count BTC 5-minute up/down markets — NOT monthly/weekly BTC markets."""
        if slug and re.match(r"btc-updown-5m-\d+", slug):
            return True
        return bool(re.search(r"btc.*5.?min|bitcoin.*5.?min|btc.*up.*down|bitcoin.*up.*down", text, re.I))

    def is_btc_market(self, text: str) -> bool:
        return bool(re.search(r"(btc|bitcoin)", text, re.I))

    def filter_wallets(self, stats: Dict) -> List[Dict]:
        """Apply qualification filters and return formatted leaderboard entries."""
        qualified = []
        now = time.time()
        stale_cutoff = now - (30 * 86400)

        for addr, s in stats.items():
            total = s.get("total", 0)
            wins = s.get("wins", 0)
            if total == 0:
                continue
            win_rate = wins / total
            avg_size = sum(s["sizes"]) / len(s["sizes"]) if s.get("sizes") else 0

            if (total >= self.min_trades
                    and s.get("last_active", 0) >= stale_cutoff
                    and avg_size >= 5.0
                    and win_rate >= self.min_win_rate):
                qualified.append({
                    "address": addr,
                    "win_rate": win_rate,
                    "total_trades": total,
                    "avg_size": avg_size,
                    "last_active": datetime.fromtimestamp(
                        s["last_active"], timezone.utc
                    ).isoformat(),
                })
        return qualified

    def get_top_wallet(self) -> Optional[str]:
        return self.top_wallet

    def get_leaderboard(self) -> List[Dict]:
        return self.leaderboard[:10]

    def get_target_win_rate(self) -> float:
        if self.leaderboard:
            return self.leaderboard[0]["win_rate"]
        return 0.84
