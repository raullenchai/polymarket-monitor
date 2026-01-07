#!/usr/bin/env python3
"""
Polymarket 异常交易监控脚本 (老鼠仓检测)
=========================================
检测指标:
1. 新钱包 - 无历史记录的钱包突然大额下注
2. 大额异常 - 单笔交易金额远超该市场平均
3. 重复进场 - 同一钱包在窄市场反复加仓
4. 时间异常 - 在重大事件前的集中买入

使用方法:
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

# ============ 配置 ============
CONFIG = {
    # Polymarket API endpoints (verified against official docs)
    # See: https://docs.polymarket.com/quickstart/reference/endpoints
    "CLOB_API": "https://clob.polymarket.com",
    "GAMMA_API": "https://gamma-api.polymarket.com",
    "DATA_API": "https://data-api.polymarket.com",

    # 检测阈值
    "NEW_WALLET_THRESHOLD_USD": 5000,      # 新钱包下注超过此金额触发警报
    "LARGE_BET_THRESHOLD_USD": 10000,      # 大额交易阈值
    "LARGE_BET_MULTIPLIER": 5,             # 交易金额超过市场平均X倍视为异常
    "REPEAT_ENTRY_COUNT": 3,               # 同市场重复进场次数阈值
    "REPEAT_ENTRY_WINDOW_HOURS": 24,       # 重复进场检测时间窗口
    "WALLET_AGE_THRESHOLD_DAYS": 7,        # 钱包年龄小于此天数视为"新钱包"

    # 监控间隔
    "POLL_INTERVAL_SECONDS": 30,

    # Rate limiting (requests per second)
    "RATE_LIMIT_RPS": 2,                   # Max requests per second
    "RATE_LIMIT_BURST": 5,                 # Allow burst of requests

    # Persistence
    "SEEN_TRADES_FILE": "seen_trades.json",
    "SEEN_TRADES_TTL_HOURS": 48,           # TTL for seen trades (auto-cleanup)

    # Market filtering
    "MAX_END_DAYS": 30,                    # Only monitor markets ending within N days

    # 关注的市场类别 (政治类通常信息不对称更严重)
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


# ============ 数据结构 ============
@dataclass
class Trade:
    """交易记录"""
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
    """警报"""
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

# ============ API 客户端 ============
class PolymarketClient:
    """Polymarket API 客户端 with rate limiting"""

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
        """获取市场列表 via Gamma API (both events and standalone markets)

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
            print(f"[ERROR] 获取市场列表失败: {e}")
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
        """获取特定token的最近交易 - DEPRECATED, use get_recent_trades instead"""
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
            print(f"[ERROR] 获取交易记录失败 (token={token_id}): {e}")
            return []

    def get_recent_trades(self, limit=500) -> list:
        """获取最近交易 (all markets) via Data API"""
        try:
            params = {"limit": limit}
            resp = self._request("GET", f"{CONFIG['DATA_API']}/trades", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[ERROR] 获取交易记录失败: {e}")
            return []

    def get_market_info(self, condition_id: str) -> dict:
        """获取市场详情 via Gamma API"""
        try:
            # Gamma API: /markets/{id}
            resp = self._request("GET", f"{CONFIG['GAMMA_API']}/markets/{condition_id}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[ERROR] 获取市场信息失败: {e}")
            return {}

    def get_wallet_history(self, wallet: str) -> dict:
        """获取钱包历史 (用于判断是否为新钱包) via Data API"""
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
            # 如果查询失败，假设是新钱包
            return {"first_seen": None, "total_trades": 0, "total_volume": 0}

    def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """获取最近成交价 via CLOB API"""
        try:
            resp = self._request("GET", f"{CONFIG['CLOB_API']}/last-trade-price",
                                 params={"token_id": token_id})
            resp.raise_for_status()
            return float(resp.json().get("price", 0))
        except Exception:
            return None

# ============ 检测引擎 ============
class AnomalyDetector:
    """异常检测引擎"""

    def __init__(self, client: PolymarketClient, seen_trades_store: SeenTradesStore = None):
        self.client = client
        self.wallet_cache = {}  # 钱包信息缓存
        self.market_stats = {}  # 市场统计缓存
        self.trade_history = defaultdict(list)  # 钱包交易历史
        self.seen_trades = seen_trades_store or SeenTradesStore()  # 持久化的已处理交易ID
        self.alerts = []

    def analyze_trade(self, trade: Trade) -> list[Alert]:
        """分析单笔交易，返回触发的警报列表"""
        alerts = []

        # 跳过已处理的交易 (使用持久化存储)
        if not self.seen_trades.add(trade.id):
            return alerts
        
        # 记录交易历史
        self.trade_history[trade.wallet].append(trade)
        
        # 检测1: 新钱包大额下注
        new_wallet_alert = self._check_new_wallet(trade)
        if new_wallet_alert:
            alerts.append(new_wallet_alert)
        
        # 检测2: 异常大额交易
        large_bet_alert = self._check_large_bet(trade)
        if large_bet_alert:
            alerts.append(large_bet_alert)
        
        # 检测3: 重复进场
        repeat_alert = self._check_repeat_entry(trade)
        if repeat_alert:
            alerts.append(repeat_alert)
        
        return alerts
    
    def _check_new_wallet(self, trade: Trade) -> Optional[Alert]:
        """检测新钱包大额下注"""
        if trade.amount_usd < CONFIG["NEW_WALLET_THRESHOLD_USD"]:
            return None
        
        # 获取或缓存钱包信息
        if trade.wallet not in self.wallet_cache:
            self.wallet_cache[trade.wallet] = self.client.get_wallet_history(trade.wallet)
        
        wallet_info = self.wallet_cache[trade.wallet]
        
        # 判断是否为新钱包
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
                    "message": f"🚨 新钱包大额下注! ${trade.amount_usd:,.0f} on {trade.outcome} @ {trade.price:.2f}"
                }
            )
        return None
    
    def _check_large_bet(self, trade: Trade) -> Optional[Alert]:
        """检测异常大额交易"""
        # 获取市场平均交易额
        if trade.market_id not in self.market_stats:
            self.market_stats[trade.market_id] = {"avg_trade": 500, "count": 0}  # 默认值
        
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
                    "message": f"💰 大额交易! ${trade.amount_usd:,.0f} ({trade.amount_usd/max(stats['avg_trade'],1):.1f}x 市场平均)"
                }
            )
        
        # 更新市场统计
        stats["avg_trade"] = (stats["avg_trade"] * stats["count"] + trade.amount_usd) / (stats["count"] + 1)
        stats["count"] += 1
        
        return None
    
    def _check_repeat_entry(self, trade: Trade) -> Optional[Alert]:
        """检测重复进场"""
        window = timedelta(hours=CONFIG["REPEAT_ENTRY_WINDOW_HOURS"])
        # Use timezone-aware datetime if trade.timestamp has timezone
        now = datetime.now(trade.timestamp.tzinfo) if trade.timestamp.tzinfo else datetime.now()
        cutoff = now - window

        # 获取该钱包在此市场的近期交易
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
                    "message": f"🔄 重复进场! {len(recent_trades)}笔交易 共${total_amount:,.0f} 在{CONFIG['REPEAT_ENTRY_WINDOW_HOURS']}小时内"
                }
            )
        return None

# ============ 通知系统 ============
class AlertNotifier:
    """警报通知"""
    
    def __init__(self, webhook_url: str = None, telegram_config: dict = None):
        self.webhook_url = webhook_url
        self.telegram_config = telegram_config
    
    def send(self, alert: Alert):
        """发送警报"""
        # 控制台输出
        self._print_alert(alert)
        
        # Webhook (Discord/Slack等)
        if self.webhook_url:
            self._send_webhook(alert)
        
        # Telegram
        if self.telegram_config:
            self._send_telegram(alert)
    
    def _print_alert(self, alert: Alert):
        """控制台打印"""
        severity_colors = {
            "low": "\033[94m",      # 蓝
            "medium": "\033[93m",   # 黄
            "high": "\033[91m",     # 红
            "critical": "\033[95m"  # 紫
        }
        reset = "\033[0m"
        color = severity_colors.get(alert.severity, "")
        
        print(f"\n{'='*60}")
        print(f"{color}[{alert.severity.upper()}] {alert.type}{reset}")
        print(f"时间: {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"市场: {alert.market_slug}")
        print(f"钱包: {alert.wallet[:10]}...{alert.wallet[-6:]}")
        print(f"详情: {alert.details.get('message', json.dumps(alert.details))}")
        print(f"{'='*60}\n")
    
    def _send_webhook(self, alert: Alert):
        """发送到Webhook"""
        try:
            payload = {
                "content": None,
                "embeds": [{
                    "title": f"🚨 {alert.type.upper()} - {alert.severity.upper()}",
                    "description": alert.details.get("message", ""),
                    "color": {"low": 3447003, "medium": 16776960, "high": 15158332, "critical": 10038562}.get(alert.severity),
                    "fields": [
                        {"name": "市场", "value": alert.market_slug, "inline": True},
                        {"name": "钱包", "value": f"`{alert.wallet[:10]}...`", "inline": True},
                        {"name": "金额", "value": f"${alert.details.get('amount_usd', 'N/A'):,.0f}", "inline": True}
                    ],
                    "timestamp": alert.timestamp.isoformat()
                }]
            }
            requests.post(self.webhook_url, json=payload, timeout=10)
        except Exception as e:
            print(f"[ERROR] Webhook发送失败: {e}")
    
    def _send_telegram(self, alert: Alert):
        """发送到Telegram"""
        try:
            bot_token = self.telegram_config.get("bot_token")
            chat_id = self.telegram_config.get("chat_id")
            
            text = f"""
🚨 *{alert.type.upper()}* [{alert.severity.upper()}]

📊 市场: `{alert.market_slug}`
👛 钱包: `{alert.wallet[:10]}...`
💵 金额: ${alert.details.get('amount_usd', 'N/A'):,.0f}

{alert.details.get('message', '')}
            """
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=10)
        except Exception as e:
            print(f"[ERROR] Telegram发送失败: {e}")

# ============ 主监控器 ============
class PolymarketMonitor:
    """主监控器"""

    def __init__(self, webhook_url: str = None, telegram_config: dict = None):
        # Shared components
        self.rate_limiter = RateLimiter()
        self.seen_trades_store = SeenTradesStore()

        self.client = PolymarketClient(rate_limiter=self.rate_limiter)
        self.detector = AnomalyDetector(self.client, seen_trades_store=self.seen_trades_store)
        self.notifier = AlertNotifier(webhook_url, telegram_config)
        self.running = False
        self._poll_count = 0

    def run(self, market_ids: list = None, num_markets: int = 50, max_end_days: int = None):
        """启动监控"""
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
                print("\n[INFO] 监控已停止")
                self.seen_trades_store.save()
                break
            except Exception as e:
                print(f"[ERROR] 监控循环错误: {e}")
                time.sleep(60)
    
    def _poll_markets(self, market_ids: list = None, num_markets: int = 50,
                      max_end_days: int = None):
        """轮询市场 - 批量获取交易并匹配到市场"""
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
                        self.notifier.send(alert)
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
        """解析原始交易数据 (Data API format)"""
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
    parser = argparse.ArgumentParser(description="Polymarket 异常交易监控")
    parser.add_argument("--min-amount", type=float, default=5000, help="新钱包警报阈值 (USD)")
    parser.add_argument("--large-bet", type=float, default=10000, help="大额交易阈值 (USD)")
    parser.add_argument("--interval", type=int, default=30, help="监控间隔 (秒)")
    parser.add_argument("--num-markets", type=int, default=50, help="监控市场数量 (按交易量排序, 默认50)")
    parser.add_argument("--max-days", type=int, default=30, help="只监控N天内结束的市场 (默认30)")
    parser.add_argument("--log-file", type=str, default="trades.log", help="交易日志文件路径 (默认: trades.log)")
    parser.add_argument("--webhook", type=str, help="Discord/Slack Webhook URL")
    parser.add_argument("--telegram-token", type=str, help="Telegram Bot Token")
    parser.add_argument("--telegram-chat", type=str, help="Telegram Chat ID")
    parser.add_argument("--markets", type=str, nargs="+", help="指定监控的市场ID (覆盖 --num-markets)")
    
    args = parser.parse_args()

    # Setup logging with custom file
    global trade_logger
    trade_logger = setup_logging(args.log_file)
    print(f"[INFO] 交易日志: {args.log_file}")

    # 更新配置
    CONFIG["NEW_WALLET_THRESHOLD_USD"] = args.min_amount
    CONFIG["LARGE_BET_THRESHOLD_USD"] = args.large_bet
    CONFIG["POLL_INTERVAL_SECONDS"] = args.interval
    
    # Telegram配置
    telegram_config = None
    if args.telegram_token and args.telegram_chat:
        telegram_config = {
            "bot_token": args.telegram_token,
            "chat_id": args.telegram_chat
        }
    
    # 启动监控
    monitor = PolymarketMonitor(
        webhook_url=args.webhook,
        telegram_config=telegram_config
    )
    monitor.run(market_ids=args.markets, num_markets=args.num_markets,
                max_end_days=args.max_days)

if __name__ == "__main__":
    main()
