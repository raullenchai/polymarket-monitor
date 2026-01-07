#!/usr/bin/env python3
"""
Polymarket Abnormal Trade Monitor
=================================
Detection methods:
1. New wallet - Wallets with no history suddenly placing large bets
2. Large trade - Single trade amount far exceeds market average
3. Repeat entry - Same wallet repeatedly entering the same market
4. Timing anomaly - Concentrated buying before major events

Usage:
    python polymarket_monitor.py --min-amount 1000 --alert-webhook <your_webhook>
"""

import requests
import json
import time
import argparse
import threading
import atexit
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
import os

# ============ Logging Setup ============
class ColoredConsoleFormatter(logging.Formatter):
    """Formatter that adds colors for console output"""
    COLORS = {
        logging.WARNING: "\033[93m",   # Yellow
        logging.CRITICAL: "\033[91m",  # Red
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        message = super().format(record)
        if color:
            return f"{color}{message}{self.RESET}"
        return message


def setup_logging(log_file: str = "trades.log"):
    """Setup logging to file AND console"""
    logger = logging.getLogger("polymarket")
    logger.setLevel(logging.DEBUG)

    # Clear existing handlers (allows reconfiguration)
    logger.handlers.clear()

    # File handler - all trades (plain text, no colors)
    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s',
                                  datefmt='%Y-%m-%d %H:%M:%S')
    fh.setFormatter(file_fmt)
    logger.addHandler(fh)

    # Console handler - show trades on screen with colors
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)  # INFO and above (skip DEBUG poll summaries)
    console_fmt = ColoredConsoleFormatter('%(asctime)s | %(message)s',
                                           datefmt='%H:%M:%S')
    ch.setFormatter(console_fmt)
    logger.addHandler(ch)

    return logger

# Global logger (will be reconfigured in main() if custom path provided)
trade_logger = setup_logging()

# ============ Configuration ============
CONFIG = {
    # Polymarket API endpoints (verified against official docs)
    # See: https://docs.polymarket.com/quickstart/reference/endpoints
    "CLOB_API": "https://clob.polymarket.com",
    "GAMMA_API": "https://gamma-api.polymarket.com",
    "DATA_API": "https://data-api.polymarket.com",

    # Detection thresholds
    "NEW_WALLET_THRESHOLD_USD": 5000,      # New wallet bet threshold for alert
    "LARGE_BET_THRESHOLD_USD": 10000,      # Large trade threshold
    "LARGE_BET_MULTIPLIER": 5,             # Trade amount exceeding Xx market avg = anomaly
    "REPEAT_ENTRY_COUNT": 3,               # Repeat entry count threshold
    "REPEAT_ENTRY_WINDOW_HOURS": 24,       # Time window for repeat detection
    "WALLET_AGE_THRESHOLD_DAYS": 7,        # Wallet younger than this = "new wallet"

    # Monitoring interval
    "POLL_INTERVAL_SECONDS": 30,

    # Rate limiting (requests per second)
    "RATE_LIMIT_RPS": 2,                   # Max requests per second
    "RATE_LIMIT_BURST": 5,                 # Allow burst of requests

    # Persistence
    "SEEN_TRADES_FILE": "seen_trades.json",
    "SEEN_TRADES_TTL_HOURS": 48,           # TTL for seen trades (auto-cleanup)

    # Market filtering
    "MAX_END_DAYS": 30,                    # Only monitor markets ending within N days

    # Focus categories (politics often has more information asymmetry)
    "FOCUS_CATEGORIES": ["politics", "elections", "government"],
}


# ============ Rate Limiter ============
class RateLimiter:
    """Token bucket rate limiter for API requests"""

    def __init__(self, rps: float = None, burst: int = None):
        self.rps = rps or CONFIG["RATE_LIMIT_RPS"]
        self.burst = burst or CONFIG["RATE_LIMIT_BURST"]
        self.tokens = self.burst
        self.last_update = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, timeout: float = None) -> bool:
        """
        Acquire a token. Blocks until a token is available or timeout.
        Returns True if acquired, False if timeout.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            with self.lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True

            # Wait and retry
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _refill(self):
        """Refill tokens based on elapsed time"""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst, self.tokens + elapsed * self.rps)
        self.last_update = now


# ============ Persistent Trade Store ============
class SeenTradesStore:
    """Persistent storage for seen trade IDs with TTL"""

    def __init__(self, filepath: str = None, ttl_hours: int = None):
        self.filepath = Path(filepath or CONFIG["SEEN_TRADES_FILE"])
        self.ttl_hours = ttl_hours or CONFIG["SEEN_TRADES_TTL_HOURS"]
        self.data: dict[str, float] = {}  # trade_id -> timestamp
        self.lock = threading.Lock()
        self._load()
        # Register save on exit
        atexit.register(self.save)

    def _load(self):
        """Load seen trades from disk"""
        if self.filepath.exists():
            try:
                with open(self.filepath, "r") as f:
                    raw = json.load(f)
                    # Clean expired entries on load
                    cutoff = time.time() - (self.ttl_hours * 3600)
                    self.data = {k: v for k, v in raw.items() if v > cutoff}
            except (json.JSONDecodeError, IOError) as e:
                print(f"[WARN] Failed to load seen trades: {e}")
                self.data = {}

    def save(self):
        """Save seen trades to disk"""
        with self.lock:
            try:
                # Clean expired before saving
                cutoff = time.time() - (self.ttl_hours * 3600)
                clean_data = {k: v for k, v in self.data.items() if v > cutoff}
                with open(self.filepath, "w") as f:
                    json.dump(clean_data, f)
            except IOError as e:
                print(f"[WARN] Failed to save seen trades: {e}")

    def add(self, trade_id: str) -> bool:
        """
        Add a trade ID. Returns True if new, False if already seen.
        """
        with self.lock:
            if trade_id in self.data:
                return False
            self.data[trade_id] = time.time()
            return True

    def contains(self, trade_id: str) -> bool:
        """Check if trade ID was seen (and not expired)"""
        with self.lock:
            if trade_id not in self.data:
                return False
            cutoff = time.time() - (self.ttl_hours * 3600)
            if self.data[trade_id] <= cutoff:
                del self.data[trade_id]
                return False
            return True

    def cleanup(self):
        """Remove expired entries"""
        with self.lock:
            cutoff = time.time() - (self.ttl_hours * 3600)
            self.data = {k: v for k, v in self.data.items() if v > cutoff}

    def __len__(self):
        return len(self.data)


# ============ Data Structures ============
@dataclass
class Trade:
    """Trade record"""
    id: str
    market_id: str
    market_slug: str
    wallet: str
    side: str  # "buy" or "sell"
    outcome: str  # "Yes" or "No"
    amount_usd: float
    price: float
    timestamp: datetime
    
@dataclass
class Alert:
    """Alert record"""
    type: str  # "new_wallet", "large_bet", "repeat_entry", "timing_anomaly"
    severity: str  # "low", "medium", "high", "critical"
    wallet: str
    market_id: str
    market_slug: str
    details: dict
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self):
        return {
            "type": self.type,
            "severity": self.severity,
            "wallet": self.wallet,
            "market": self.market_slug,
            "details": self.details,
            "time": self.timestamp.isoformat()
        }

# ============ API Client ============
class PolymarketClient:
    """Polymarket API client with rate limiting"""

    def __init__(self, rate_limiter: RateLimiter = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "PolymarketMonitor/1.0",
            "Accept": "application/json"
        })
        self.rate_limiter = rate_limiter or RateLimiter()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make a rate-limited request"""
        self.rate_limiter.acquire()
        return self.session.request(method, url, **kwargs)

    def get_markets(self, limit=100, active_only=True, sort_by_volume=False,
                    max_end_days: int = None) -> list:
        """Get market list via Gamma API (both events and standalone markets)

        Args:
            limit: Maximum number of markets to return
            active_only: Only return active markets
            sort_by_volume: If True, fetch extra markets and return top N by volume
            max_end_days: Only include markets ending within N days (None = no filter)
        """
        from datetime import timezone
        all_markets = []

        try:
            params = {"limit": 500, "closed": "false"}
            if active_only:
                params["active"] = "true"

            # 1. Fetch Events (contains high-volume markets like Fed Decision)
            resp_events = self._request("GET", f"{CONFIG['GAMMA_API']}/events", params=params)
            resp_events.raise_for_status()
            events = resp_events.json()

            # Extract markets from events
            for event in events:
                event_end = event.get('endDate')
                event_slug = event.get('slug', '')
                for market in event.get('markets', []):
                    # Inherit end date from event if not set on market
                    if not market.get('endDate'):
                        market['endDate'] = event_end
                    # Add event context
                    market['_event_slug'] = event_slug
                    market['_source'] = 'event'
                    all_markets.append(market)

            # 2. Fetch standalone markets
            resp_markets = self._request("GET", f"{CONFIG['GAMMA_API']}/markets", params=params)
            resp_markets.raise_for_status()
            standalone = resp_markets.json()

            for market in standalone:
                market['_source'] = 'market'
                all_markets.append(market)

            # Deduplicate by market ID
            seen_ids = set()
            unique_markets = []
            for m in all_markets:
                mid = m.get('id') or m.get('conditionId')
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    unique_markets.append(m)
            all_markets = unique_markets

        except Exception as e:
            print(f"[ERROR] Failed to get markets: {e}")
            return []

        # Filter by end date if specified
        if max_end_days is not None and all_markets:
            now = datetime.now(timezone.utc)
            max_end = now + timedelta(days=max_end_days)

            filtered = []
            for m in all_markets:
                if m.get('closed'):
                    continue
                end_str = m.get('endDate')
                if not end_str:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                    if now < end_dt <= max_end:
                        filtered.append(m)
                except (ValueError, TypeError):
                    pass
            all_markets = filtered

        # Sort by volume if requested
        if sort_by_volume and all_markets:
            all_markets = sorted(
                all_markets,
                key=lambda m: float(m.get('volume', 0) or 0),
                reverse=True
            )

        return all_markets[:limit]

    def get_market_trades(self, token_id: str, limit=100) -> list:
        """Get recent trades for a specific token - DEPRECATED, use get_recent_trades instead"""
        # Note: Data API doesn't filter by token_id properly
        # This method is kept for compatibility but may return unfiltered results
        try:
            params = {"limit": limit}
            resp = self._request("GET", f"{CONFIG['DATA_API']}/trades", params=params)
            resp.raise_for_status()
            trades = resp.json()
            # Client-side filter by asset
            return [t for t in trades if t.get('asset') == token_id]
        except Exception as e:
            print(f"[ERROR] Failed to get trades (token={token_id}): {e}")
            return []

    def get_recent_trades(self, limit=500) -> list:
        """Get recent trades (all markets) via Data API"""
        try:
            params = {"limit": limit}
            resp = self._request("GET", f"{CONFIG['DATA_API']}/trades", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[ERROR] Failed to get trades: {e}")
            return []

    def get_market_info(self, condition_id: str) -> dict:
        """Get market details via Gamma API"""
        try:
            # Gamma API: /markets/{id}
            resp = self._request("GET", f"{CONFIG['GAMMA_API']}/markets/{condition_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[ERROR] Failed to get market info: {e}")
            return {}

    def get_wallet_history(self, wallet: str) -> dict:
        """Get wallet history (to determine if wallet is new) via Data API"""
        try:
            # Data API: /positions endpoint for user positions
            # See: https://docs.polymarket.com/quickstart/reference/endpoints
            params = {"user": wallet}
            resp = self._request("GET", f"{CONFIG['DATA_API']}/positions", params=params)
            resp.raise_for_status()
            data = resp.json()

            # Handle both list and dict responses
            positions = data if isinstance(data, list) else data.get("positions", [])
            return {
                "first_seen": data.get("created_at") if isinstance(data, dict) else None,
                "total_trades": len(positions),
                "total_volume": sum(float(p.get("value", 0) or 0) for p in positions)
            }
        except Exception as e:
            # If query fails, assume it's a new wallet
            return {"first_seen": None, "total_trades": 0, "total_volume": 0}

    def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Get last trade price via CLOB API"""
        try:
            resp = self._request("GET", f"{CONFIG['CLOB_API']}/last-trade-price",
                                 params={"token_id": token_id})
            resp.raise_for_status()
            return float(resp.json().get("price", 0))
        except Exception:
            return None

# ============ Detection Engine ============
class AnomalyDetector:
    """Anomaly detection engine"""

    def __init__(self, client: PolymarketClient, seen_trades_store: SeenTradesStore = None):
        self.client = client
        self.wallet_cache = {}  # Wallet info cache
        self.market_stats = {}  # Market statistics cache
        self.trade_history = defaultdict(list)  # Wallet trade history
        self.seen_trades = seen_trades_store or SeenTradesStore()  # Persistent processed trade IDs
        self.alerts = []

    def analyze_trade(self, trade: Trade) -> list[Alert]:
        """Analyze a single trade and return list of triggered alerts"""
        alerts = []

        # Skip already processed trades (using persistent storage)
        if not self.seen_trades.add(trade.id):
            return alerts

        # Record trade history
        self.trade_history[trade.wallet].append(trade)

        # Detection 1: New wallet large bet
        new_wallet_alert = self._check_new_wallet(trade)
        if new_wallet_alert:
            alerts.append(new_wallet_alert)

        # Detection 2: Abnormally large trade
        large_bet_alert = self._check_large_bet(trade)
        if large_bet_alert:
            alerts.append(large_bet_alert)

        # Detection 3: Repeat entry
        repeat_alert = self._check_repeat_entry(trade)
        if repeat_alert:
            alerts.append(repeat_alert)

        return alerts

    def _check_new_wallet(self, trade: Trade) -> Optional[Alert]:
        """Detect new wallet large bets"""
        if trade.amount_usd < CONFIG["NEW_WALLET_THRESHOLD_USD"]:
            return None
        
        # Get or cache wallet info
        if trade.wallet not in self.wallet_cache:
            self.wallet_cache[trade.wallet] = self.client.get_wallet_history(trade.wallet)

        wallet_info = self.wallet_cache[trade.wallet]

        # Determine if wallet is new
        is_new = False
        if wallet_info["first_seen"] is None:
            is_new = True
        elif wallet_info["total_trades"] < 5:
            is_new = True
        else:
            try:
                first_seen = datetime.fromisoformat(wallet_info["first_seen"].replace("Z", "+00:00"))
                if datetime.now(first_seen.tzinfo) - first_seen < timedelta(days=CONFIG["WALLET_AGE_THRESHOLD_DAYS"]):
                    is_new = True
            except:
                pass
        
        if is_new:
            severity = "critical" if trade.amount_usd > CONFIG["NEW_WALLET_THRESHOLD_USD"] * 2 else "high"
            return Alert(
                type="new_wallet",
                severity=severity,
                wallet=trade.wallet,
                market_id=trade.market_id,
                market_slug=trade.market_slug,
                details={
                    "amount_usd": trade.amount_usd,
                    "outcome": trade.outcome,
                    "price": trade.price,
                    "wallet_age_trades": wallet_info["total_trades"],
                    "message": f"🚨 New wallet large bet! ${trade.amount_usd:,.0f} on {trade.outcome} @ {trade.price:.2f}"
                }
            )
        return None
    
    def _check_large_bet(self, trade: Trade) -> Optional[Alert]:
        """Detect abnormally large trades"""
        # Get market average trade amount
        if trade.market_id not in self.market_stats:
            self.market_stats[trade.market_id] = {"avg_trade": 500, "count": 0}  # Default
        
        stats = self.market_stats[trade.market_id]
        threshold = max(
            CONFIG["LARGE_BET_THRESHOLD_USD"],
            stats["avg_trade"] * CONFIG["LARGE_BET_MULTIPLIER"]
        )
        
        if trade.amount_usd >= threshold:
            severity = "critical" if trade.amount_usd > threshold * 2 else "high"
            return Alert(
                type="large_bet",
                severity=severity,
                wallet=trade.wallet,
                market_id=trade.market_id,
                market_slug=trade.market_slug,
                details={
                    "amount_usd": trade.amount_usd,
                    "market_avg": stats["avg_trade"],
                    "multiplier": trade.amount_usd / max(stats["avg_trade"], 1),
                    "outcome": trade.outcome,
                    "price": trade.price,
                    "message": f"💰 Large trade! ${trade.amount_usd:,.0f} ({trade.amount_usd/max(stats['avg_trade'],1):.1f}x market avg)"
                }
            )
        
        # Update market statistics
        stats["avg_trade"] = (stats["avg_trade"] * stats["count"] + trade.amount_usd) / (stats["count"] + 1)
        stats["count"] += 1

        return None

    def _check_repeat_entry(self, trade: Trade) -> Optional[Alert]:
        """Detect repeat entries"""
        window = timedelta(hours=CONFIG["REPEAT_ENTRY_WINDOW_HOURS"])
        # Use timezone-aware datetime if trade.timestamp has timezone
        now = datetime.now(trade.timestamp.tzinfo) if trade.timestamp.tzinfo else datetime.now()
        cutoff = now - window

        # Get recent trades from this wallet in this market
        recent_trades = [
            t for t in self.trade_history[trade.wallet]
            if t.market_id == trade.market_id and t.timestamp > cutoff
        ]
        
        if len(recent_trades) >= CONFIG["REPEAT_ENTRY_COUNT"]:
            total_amount = sum(t.amount_usd for t in recent_trades)
            severity = "high" if total_amount > CONFIG["LARGE_BET_THRESHOLD_USD"] else "medium"
            return Alert(
                type="repeat_entry",
                severity=severity,
                wallet=trade.wallet,
                market_id=trade.market_id,
                market_slug=trade.market_slug,
                details={
                    "trade_count": len(recent_trades),
                    "total_amount": total_amount,
                    "window_hours": CONFIG["REPEAT_ENTRY_WINDOW_HOURS"],
                    "message": f"🔄 Repeat entry! {len(recent_trades)} trades totaling ${total_amount:,.0f} in {CONFIG['REPEAT_ENTRY_WINDOW_HOURS']} hours"
                }
            )
        return None

# ============ Notification System ============
class AlertNotifier:
    """Alert notifier"""

    def __init__(self, webhook_url: str = None, telegram_config: dict = None,
                 lark_webhook: str = None):
        self.webhook_url = webhook_url
        self.telegram_config = telegram_config
        self.lark_webhook = lark_webhook

    def send(self, alert: Alert, market_url: str = None):
        """Send alert"""
        # Console output
        self._print_alert(alert)

        # Webhook (Discord/Slack/etc)
        if self.webhook_url:
            self._send_webhook(alert)

        # Telegram
        if self.telegram_config:
            self._send_telegram(alert)

        # Lark/Feishu
        if self.lark_webhook:
            self._send_lark(alert, market_url)

    def _print_alert(self, alert: Alert):
        """Print to console"""
        severity_colors = {
            "low": "\033[94m",      # Blue
            "medium": "\033[93m",   # Yellow
            "high": "\033[91m",     # Red
            "critical": "\033[95m"  # Purple
        }
        reset = "\033[0m"
        color = severity_colors.get(alert.severity, "")
        
        print(f"\n{'='*60}")
        print(f"{color}[{alert.severity.upper()}] {alert.type}{reset}")
        print(f"Time: {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Market: {alert.market_slug}")
        print(f"Wallet: {alert.wallet[:10]}...{alert.wallet[-6:]}")
        print(f"Details: {alert.details.get('message', json.dumps(alert.details))}")
        print(f"{'='*60}\n")

    def _send_webhook(self, alert: Alert):
        """Send to Webhook"""
        try:
            payload = {
                "content": None,
                "embeds": [{
                    "title": f"🚨 {alert.type.upper()} - {alert.severity.upper()}",
                    "description": alert.details.get("message", ""),
                    "color": {"low": 3447003, "medium": 16776960, "high": 15158332, "critical": 10038562}.get(alert.severity),
                    "fields": [
                        {"name": "Market", "value": alert.market_slug, "inline": True},
                        {"name": "Wallet", "value": f"`{alert.wallet[:10]}...`", "inline": True},
                        {"name": "Amount", "value": f"${alert.details.get('amount_usd', 'N/A'):,.0f}", "inline": True}
                    ],
                    "timestamp": alert.timestamp.isoformat()
                }]
            }
            requests.post(self.webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"[ERROR] Failed to send webhook: {e}")

    def _send_telegram(self, alert: Alert):
        """Send to Telegram"""
        try:
            bot_token = self.telegram_config.get("bot_token")
            chat_id = self.telegram_config.get("chat_id")

            text = f"""
🚨 *{alert.type.upper()}* [{alert.severity.upper()}]

📊 Market: `{alert.market_slug}`
👛 Wallet: `{alert.wallet[:10]}...`
💵 Amount: ${alert.details.get('amount_usd', 'N/A'):,.0f}

{alert.details.get('message', '')}
            """
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=10)
        except Exception as e:
            print(f"[ERROR] Failed to send Telegram: {e}")

    def _send_lark(self, alert: Alert, market_url: str = None):
        """Send to Lark/Feishu"""
        try:
            # Color based on severity
            color = {
                "low": "blue",
                "medium": "yellow",
                "high": "orange",
                "critical": "red"
            }.get(alert.severity, "blue")

            # Get outcome and price from alert details
            outcome = alert.details.get('outcome', 'N/A')
            price = alert.details.get('price', 0)

            # Build interactive card
            card = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": f"🚨 {alert.type.upper()} - {alert.severity.upper()}"
                        },
                        "template": color
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": alert.details.get('message', '')
                            }
                        },
                        {
                            "tag": "div",
                            "fields": [
                                {
                                    "is_short": True,
                                    "text": {
                                        "tag": "lark_md",
                                        "content": f"**Market**\n{alert.market_slug}"
                                    }
                                },
                                {
                                    "is_short": True,
                                    "text": {
                                        "tag": "lark_md",
                                        "content": f"**Amount**\n${alert.details.get('amount_usd', 0):,.0f}"
                                    }
                                }
                            ]
                        },
                        {
                            "tag": "div",
                            "fields": [
                                {
                                    "is_short": True,
                                    "text": {
                                        "tag": "lark_md",
                                        "content": f"**Bet**\n{outcome} @ {price:.2f}" if price else f"**Bet**\n{outcome}"
                                    }
                                },
                                {
                                    "is_short": True,
                                    "text": {
                                        "tag": "lark_md",
                                        "content": f"**Wallet**\n`{alert.wallet}`"
                                    }
                                }
                            ]
                        },
                        {
                            "tag": "hr"
                        },
                        {
                            "tag": "action",
                            "actions": [
                                {
                                    "tag": "button",
                                    "text": {
                                        "tag": "plain_text",
                                        "content": "View on Polymarket"
                                    },
                                    "type": "primary",
                                    "url": market_url or "https://polymarket.com"
                                }
                            ]
                        }
                    ]
                }
            }

            resp = requests.post(self.lark_webhook, json=card, timeout=10)
            if resp.status_code != 200:
                print(f"[ERROR] Failed to send Lark: {resp.text}")
        except Exception as e:
            print(f"[ERROR] Failed to send Lark: {e}")


# ============ Main Monitor ============
class PolymarketMonitor:
    """Main monitor orchestrator"""

    def __init__(self, webhook_url: str = None, telegram_config: dict = None,
                 lark_webhook: str = None):
        # Shared components
        self.rate_limiter = RateLimiter()
        self.seen_trades_store = SeenTradesStore()

        self.client = PolymarketClient(rate_limiter=self.rate_limiter)
        self.detector = AnomalyDetector(self.client, seen_trades_store=self.seen_trades_store)
        self.notifier = AlertNotifier(webhook_url, telegram_config, lark_webhook=lark_webhook)
        self.running = False
        self._poll_count = 0

    def run(self, market_ids: list = None, num_markets: int = 50, max_end_days: int = None):
        """Start monitoring"""
        self.running = True
        self._num_markets = num_markets
        self._max_end_days = max_end_days or CONFIG["MAX_END_DAYS"]

        # Print config box
        w = 60  # box width
        def line(text):
            return f"║  {text:<{w-4}}║"

        print("╔" + "═" * (w-2) + "╗")
        print(f"║{'Polymarket Abnormal Trade Monitor':^{w-2}}║")
        print("╠" + "═" * (w-2) + "╣")
        print(line(f"Markets: Top {num_markets} by volume (ending ≤{self._max_end_days} days)"))
        print(line(f"New wallet threshold:    ${CONFIG['NEW_WALLET_THRESHOLD_USD']:,}"))
        print(line(f"Large bet threshold:     ${CONFIG['LARGE_BET_THRESHOLD_USD']:,}"))
        print(line(f"Repeat entry threshold:  {CONFIG['REPEAT_ENTRY_COUNT']} trades / {CONFIG['REPEAT_ENTRY_WINDOW_HOURS']} hours"))
        print(line(f"Poll interval:           {CONFIG['POLL_INTERVAL_SECONDS']} seconds"))
        print(line(f"Rate limit:              {CONFIG['RATE_LIMIT_RPS']} req/s (burst: {CONFIG['RATE_LIMIT_BURST']})"))
        print("╚" + "═" * (w-2) + "╝")

        # Fetch and display monitored markets
        print("\n[INFO] Fetching markets...")
        if market_ids:
            markets = [{"id": mid} for mid in market_ids]
        else:
            markets = self.client.get_markets(
                limit=num_markets,
                active_only=True,
                sort_by_volume=True,
                max_end_days=self._max_end_days
            )

        if markets:
            print(f"\n{'─'*70}")
            print(f"{'#':<4} {'Market':<40} {'Volume':>12} {'End Date':>12}")
            print(f"{'─'*70}")
            for i, m in enumerate(markets[:num_markets], 1):
                name = (m.get('question') or m.get('slug') or m.get('id', 'unknown'))[:38]
                vol = float(m.get('volume', 0) or 0)
                end = m.get('endDate', '')[:10] if m.get('endDate') else 'N/A'
                print(f"{i:<4} {name:<40} ${vol:>10,.0f} {end:>12}")
            print(f"{'─'*70}\n")
        else:
            print("[WARN] No markets found matching criteria\n")

        # Cache markets for polling
        self._cached_markets = markets

        while self.running:
            try:
                self._poll_markets(market_ids, num_markets=self._num_markets,
                                   max_end_days=self._max_end_days)
                self._poll_count += 1

                # Save seen trades every 10 polls and cleanup expired
                if self._poll_count % 10 == 0:
                    self.seen_trades_store.cleanup()
                    self.seen_trades_store.save()

                time.sleep(CONFIG["POLL_INTERVAL_SECONDS"])
            except KeyboardInterrupt:
                print("\n[INFO] Monitoring stopped")
                self.seen_trades_store.save()
                break
            except Exception as e:
                print(f"[ERROR] Monitor loop error: {e}")
                time.sleep(60)

    def _poll_markets(self, market_ids: list = None, num_markets: int = 50,
                      max_end_days: int = None):
        """Poll markets - batch fetch trades and match to markets"""
        # Use cached markets (fetched at startup), refresh every 100 polls
        if hasattr(self, '_cached_markets') and self._cached_markets and self._poll_count % 100 != 0:
            markets = self._cached_markets
        elif market_ids:
            markets = [{"id": mid} for mid in market_ids]
        else:
            # Fetch top markets by volume, filtered by end date
            markets = self.client.get_markets(
                limit=num_markets,
                active_only=True,
                sort_by_volume=True,
                max_end_days=max_end_days or CONFIG["MAX_END_DAYS"]
            )
            self._cached_markets = markets

        if not markets:
            return

        # Build token_id -> market mapping
        token_to_market = {}
        for market in markets:
            clob_tokens = market.get("clobTokenIds")
            if clob_tokens:
                try:
                    if isinstance(clob_tokens, str):
                        token_ids = json.loads(clob_tokens)
                    elif isinstance(clob_tokens, list):
                        token_ids = clob_tokens
                    else:
                        token_ids = []
                    for tid in token_ids:
                        token_to_market[tid] = market
                except (json.JSONDecodeError, TypeError):
                    pass

        if not token_to_market:
            return

        # Fetch recent trades (batch, more efficient)
        raw_trades = self.client.get_recent_trades(limit=500)

        # Match trades to our monitored markets
        trades_logged = 0
        for raw in raw_trades:
            asset_id = raw.get('asset')
            if asset_id not in token_to_market:
                continue  # Trade not in our monitored markets

            market = token_to_market[asset_id]
            trade = self._parse_trade(raw, market)
            if trade:
                # Analyze for anomalies
                alerts = self.detector.analyze_trade(trade)

                # Build market URL
                event_slug = market.get('_event_slug') or market.get('slug', '')
                market_name = market.get('question') or market.get('slug') or 'unknown'
                market_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

                if alerts:
                    # Abnormal trade - log with WARNING/CRITICAL
                    alert_types = ", ".join(a.type for a in alerts)
                    severity = max((a.severity for a in alerts), key=lambda s: ["low","medium","high","critical"].index(s))
                    log_level = logging.CRITICAL if severity == "critical" else logging.WARNING

                    trade_logger.log(log_level,
                        f"⚠️  ABNORMAL | ${trade.amount_usd:>10,.2f} | {trade.outcome:<4} | [{alert_types}]\n"
                        f"         Market: {market_name}\n"
                        f"         Wallet: {trade.wallet}\n"
                        f"         URL: {market_url}"
                    )

                    # Send notifications
                    for alert in alerts:
                        self.notifier.send(alert, market_url=market_url)
                else:
                    # Normal trade - log as INFO with full details
                    trade_logger.info(
                        f"   TRADE    | ${trade.amount_usd:>10,.2f} | {trade.outcome:<4} | {market_name}\n"
                        f"         Wallet: {trade.wallet} | {market_url}"
                    )

                trades_logged += 1

        if trades_logged > 0:
            trade_logger.debug(f"--- Poll complete: {trades_logged} trades logged ---")
    
    def _parse_trade(self, raw: dict, market: dict) -> Optional[Trade]:
        """Parse raw trade data (Data API format)"""
        try:
            # Generate unique ID from transaction hash or hash of raw data
            trade_id = raw.get("transactionHash") or raw.get("id") or str(hash(str(raw)))

            # Wallet: Data API uses 'proxyWallet', fallback to other fields
            wallet = raw.get("proxyWallet") or raw.get("maker") or raw.get("owner") or "unknown"

            # Parse timestamp: Data API returns unix timestamp (int), or ISO string
            ts_raw = raw.get("timestamp")
            if isinstance(ts_raw, (int, float)):
                timestamp = datetime.fromtimestamp(ts_raw)
            elif isinstance(ts_raw, str):
                timestamp = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                timestamp = datetime.now()

            # Amount calculation
            size = float(raw.get("size", 0))
            price = float(raw.get("price", 0))
            amount_usd = size * price

            return Trade(
                id=trade_id,
                market_id=market.get("id") or market.get("conditionId") or raw.get("conditionId"),
                market_slug=market.get("slug") or raw.get("slug") or "unknown",
                wallet=wallet,
                side=raw.get("side", "buy").lower(),
                outcome=raw.get("outcome", "unknown"),
                amount_usd=amount_usd,
                price=price,
                timestamp=timestamp,
            )
        except Exception as e:
            return None

# ============ CLI ============
def main():
    parser = argparse.ArgumentParser(description="Polymarket Abnormal Trade Monitor")
    parser.add_argument("--min-amount", type=float, default=5000, help="New wallet alert threshold (USD)")
    parser.add_argument("--large-bet", type=float, default=10000, help="Large trade threshold (USD)")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval (seconds)")
    parser.add_argument("--num-markets", type=int, default=50, help="Number of markets to monitor (by volume, default 50)")
    parser.add_argument("--max-days", type=int, default=30, help="Only monitor markets ending within N days (default 30)")
    parser.add_argument("--log-file", type=str, default="trades.log", help="Trade log file path (default: trades.log)")
    parser.add_argument("--webhook", type=str, help="Discord/Slack Webhook URL")
    parser.add_argument("--telegram-token", type=str, help="Telegram Bot Token")
    parser.add_argument("--telegram-chat", type=str, help="Telegram Chat ID")
    parser.add_argument("--lark-webhook", type=str, help="Lark/Feishu Bot Webhook URL")
    parser.add_argument("--markets", type=str, nargs="+", help="Specific market IDs to monitor (overrides --num-markets)")
    
    args = parser.parse_args()

    # Setup logging with custom file
    global trade_logger
    trade_logger = setup_logging(args.log_file)
    print(f"[INFO] Trade log: {args.log_file}")

    # Update config
    CONFIG["NEW_WALLET_THRESHOLD_USD"] = args.min_amount
    CONFIG["LARGE_BET_THRESHOLD_USD"] = args.large_bet
    CONFIG["POLL_INTERVAL_SECONDS"] = args.interval

    # Telegram config
    telegram_config = None
    if args.telegram_token and args.telegram_chat:
        telegram_config = {
            "bot_token": args.telegram_token,
            "chat_id": args.telegram_chat
        }

    # Start monitor
    monitor = PolymarketMonitor(
        webhook_url=args.webhook,
        telegram_config=telegram_config,
        lark_webhook=args.lark_webhook
    )
    monitor.run(market_ids=args.markets, num_markets=args.num_markets,
                max_end_days=args.max_days)

if __name__ == "__main__":
    main()
