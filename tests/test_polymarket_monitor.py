"""
Tests for polymarket_monitor.py

Run with: pytest tests/ -v --cov=polymarket_monitor --cov-report=term-missing
"""

import json
import tempfile
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock
import pytest

# Import from parent
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket_monitor import (
    CONFIG,
    RateLimiter,
    SeenTradesStore,
    Trade,
    Alert,
    PolymarketClient,
    AnomalyDetector,
    AlertNotifier,
    PolymarketMonitor,
    ColoredConsoleFormatter,
    setup_logging,
)


# ============ Fixtures ============

@pytest.fixture
def sample_trade():
    """Create a sample trade for testing"""
    return Trade(
        id="trade_001",
        market_id="market_123",
        market_slug="will-something-happen",
        wallet="0x1234567890abcdef1234567890abcdef12345678",
        side="buy",
        outcome="Yes",
        amount_usd=1000.0,
        price=0.65,
        timestamp=datetime.now(),
    )


@pytest.fixture
def large_trade():
    """Trade that exceeds thresholds"""
    return Trade(
        id="trade_large_001",
        market_id="market_123",
        market_slug="will-something-happen",
        wallet="0xnewwallet0000000000000000000000000000001",
        side="buy",
        outcome="Yes",
        amount_usd=15000.0,
        price=0.75,
        timestamp=datetime.now(),
    )


@pytest.fixture
def temp_seen_trades_file():
    """Temporary file for SeenTradesStore"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('{}')
        return Path(f.name)


@pytest.fixture
def mock_client():
    """Mocked PolymarketClient"""
    client = MagicMock(spec=PolymarketClient)
    client.get_wallet_history.return_value = {
        "first_seen": None,
        "total_trades": 0,
        "total_volume": 0
    }
    return client


# ============ RateLimiter Tests ============

class TestRateLimiter:
    def test_init_default_values(self):
        limiter = RateLimiter()
        assert limiter.rps == CONFIG["RATE_LIMIT_RPS"]
        assert limiter.burst == CONFIG["RATE_LIMIT_BURST"]

    def test_init_custom_values(self):
        limiter = RateLimiter(rps=5, burst=10)
        assert limiter.rps == 5
        assert limiter.burst == 10

    def test_acquire_within_burst(self):
        limiter = RateLimiter(rps=10, burst=5)
        # Should be able to acquire burst number of tokens immediately
        for _ in range(5):
            assert limiter.acquire(timeout=0.01) is True

    def test_acquire_exceeds_burst_with_timeout(self):
        limiter = RateLimiter(rps=1, burst=1)
        # First acquire should succeed
        assert limiter.acquire(timeout=0.1) is True
        # Second acquire should timeout quickly
        assert limiter.acquire(timeout=0.05) is False

    def test_acquire_refills_over_time(self):
        limiter = RateLimiter(rps=10, burst=2)
        # Drain the bucket
        limiter.acquire()
        limiter.acquire()
        # Wait for refill
        time.sleep(0.15)
        # Should have refilled at least 1 token
        assert limiter.acquire(timeout=0.01) is True

    def test_thread_safety(self):
        """Verify rate limiter is thread-safe"""
        limiter = RateLimiter(rps=100, burst=10)
        acquired = []

        def acquire_tokens():
            for _ in range(5):
                if limiter.acquire(timeout=0.5):
                    acquired.append(1)

        threads = [threading.Thread(target=acquire_tokens) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have acquired some tokens
        assert len(acquired) > 0


# ============ SeenTradesStore Tests ============

class TestSeenTradesStore:
    def test_init_creates_empty_store(self, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert len(store) == 0

    def test_add_new_trade(self, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert store.add("trade_001") is True
        assert len(store) == 1

    def test_add_duplicate_trade(self, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert store.add("trade_001") is True
        assert store.add("trade_001") is False  # Already exists
        assert len(store) == 1

    def test_contains(self, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        store.add("trade_001")
        assert store.contains("trade_001") is True
        assert store.contains("trade_002") is False

    def test_save_and_load(self, temp_seen_trades_file):
        # Create store and add trades
        store1 = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        store1.add("trade_001")
        store1.add("trade_002")
        store1.save()

        # Load in new store instance
        store2 = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert store2.contains("trade_001") is True
        assert store2.contains("trade_002") is True

    def test_cleanup_expired(self, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        # Manually add an expired entry
        expired_time = time.time() - 7200  # 2 hours ago
        store.data["old_trade"] = expired_time
        store.data["new_trade"] = time.time()

        store.cleanup()

        assert "old_trade" not in store.data
        assert "new_trade" in store.data

    def test_contains_expired_removes_entry(self, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        # Manually add an expired entry
        store.data["old_trade"] = time.time() - 7200

        # Checking contains should remove expired entry
        assert store.contains("old_trade") is False
        assert "old_trade" not in store.data

    def test_load_handles_corrupted_file(self, temp_seen_trades_file):
        # Write corrupted JSON
        with open(temp_seen_trades_file, 'w') as f:
            f.write("not valid json")

        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert len(store) == 0  # Should handle gracefully


# ============ Trade & Alert Tests ============

class TestDataStructures:
    def test_trade_creation(self, sample_trade):
        assert sample_trade.id == "trade_001"
        assert sample_trade.amount_usd == 1000.0
        assert sample_trade.wallet.startswith("0x")

    def test_alert_creation(self):
        alert = Alert(
            type="new_wallet",
            severity="high",
            wallet="0x123",
            market_id="market_1",
            market_slug="test-market",
            details={"amount_usd": 5000, "message": "Test alert"}
        )
        assert alert.type == "new_wallet"
        assert alert.severity == "high"

    def test_alert_to_dict(self):
        alert = Alert(
            type="large_bet",
            severity="critical",
            wallet="0xabc",
            market_id="market_2",
            market_slug="another-market",
            details={"amount_usd": 10000}
        )
        d = alert.to_dict()
        assert d["type"] == "large_bet"
        assert d["severity"] == "critical"
        assert "time" in d


# ============ AnomalyDetector Tests ============

class TestAnomalyDetector:
    def test_analyze_trade_skips_duplicates(self, mock_client, sample_trade, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        # First analysis
        alerts1 = detector.analyze_trade(sample_trade)
        # Second analysis of same trade
        alerts2 = detector.analyze_trade(sample_trade)

        assert len(alerts2) == 0  # Should be skipped

    def test_detect_new_wallet_large_bet(self, mock_client, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        # New wallet (no history) with large bet
        mock_client.get_wallet_history.return_value = {
            "first_seen": None,
            "total_trades": 0,
            "total_volume": 0
        }

        large_trade = Trade(
            id="trade_new_wallet",
            market_id="market_1",
            market_slug="test-market",
            wallet="0xnewwallet",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,  # Above NEW_WALLET_THRESHOLD_USD
            price=0.5,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(large_trade)

        new_wallet_alerts = [a for a in alerts if a.type == "new_wallet"]
        assert len(new_wallet_alerts) >= 1
        assert new_wallet_alerts[0].severity in ["high", "critical"]

    def test_no_alert_for_small_trade(self, mock_client, sample_trade, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        # Small trade (1000 USD) shouldn't trigger new_wallet alert
        alerts = detector.analyze_trade(sample_trade)
        new_wallet_alerts = [a for a in alerts if a.type == "new_wallet"]
        assert len(new_wallet_alerts) == 0

    def test_detect_large_bet(self, mock_client, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        # Existing wallet with huge bet
        mock_client.get_wallet_history.return_value = {
            "first_seen": (datetime.now() - timedelta(days=30)).isoformat(),
            "total_trades": 100,
            "total_volume": 50000
        }

        huge_trade = Trade(
            id="trade_huge",
            market_id="market_1",
            market_slug="test-market",
            wallet="0xoldwallet",
            side="buy",
            outcome="Yes",
            amount_usd=25000.0,  # Way above threshold
            price=0.8,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(huge_trade)
        large_bet_alerts = [a for a in alerts if a.type == "large_bet"]
        assert len(large_bet_alerts) >= 1

    def test_detect_repeat_entry(self, mock_client, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        wallet = "0xrepeatwallet"
        market_id = "market_repeat"

        # Create multiple trades from same wallet in same market
        for i in range(4):
            trade = Trade(
                id=f"trade_repeat_{i}",
                market_id=market_id,
                market_slug="repeat-market",
                wallet=wallet,
                side="buy",
                outcome="Yes",
                amount_usd=500.0,
                price=0.5,
                timestamp=datetime.now(),
            )
            alerts = detector.analyze_trade(trade)

        # Last trade should trigger repeat_entry alert
        repeat_alerts = [a for a in alerts if a.type == "repeat_entry"]
        assert len(repeat_alerts) >= 1

    def test_wallet_cache(self, mock_client, temp_seen_trades_file):
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        wallet = "0xcachedwallet"

        # First trade
        trade1 = Trade(
            id="trade_cache_1",
            market_id="market_1",
            market_slug="test",
            wallet=wallet,
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )
        detector.analyze_trade(trade1)

        # Second trade from same wallet
        trade2 = Trade(
            id="trade_cache_2",
            market_id="market_1",
            market_slug="test",
            wallet=wallet,
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )
        detector.analyze_trade(trade2)

        # get_wallet_history should only be called once due to cache
        assert mock_client.get_wallet_history.call_count == 1


# ============ PolymarketClient Tests ============

class TestPolymarketClient:
    def test_init_with_rate_limiter(self):
        limiter = RateLimiter(rps=5, burst=10)
        client = PolymarketClient(rate_limiter=limiter)
        assert client.rate_limiter is limiter

    def test_init_creates_default_rate_limiter(self):
        client = PolymarketClient()
        assert client.rate_limiter is not None

    @patch('polymarket_monitor.requests.Session')
    def test_get_markets_success(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_response = MagicMock()
        mock_response.json.return_value = [{"id": "market_1"}, {"id": "market_2"}]
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        markets = client.get_markets(limit=10)
        assert len(markets) == 2

    @patch('polymarket_monitor.requests.Session')
    def test_get_markets_handles_error(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_session.request.side_effect = Exception("Network error")

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        markets = client.get_markets()
        assert markets == []

    @patch('polymarket_monitor.requests.Session')
    def test_get_wallet_history_returns_default_on_error(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_session.request.side_effect = Exception("API error")

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_wallet_history("0x123")
        assert result["first_seen"] is None
        assert result["total_trades"] == 0

    @patch('polymarket_monitor.requests.Session')
    def test_get_recent_trades(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_response = MagicMock()
        mock_response.json.return_value = [{"id": "trade_1", "asset": "t1"}, {"id": "trade_2", "asset": "t2"}]
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        trades = client.get_recent_trades(limit=100)
        assert len(trades) == 2

    @patch('polymarket_monitor.requests.Session')
    def test_get_market_trades_filters_by_asset(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"id": "trade_1", "asset": "token_123"},
            {"id": "trade_2", "asset": "other_token"}
        ]
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        # Should filter to only matching asset
        trades = client.get_market_trades("token_123")
        assert len(trades) == 1
        assert trades[0]["asset"] == "token_123"


# ============ AlertNotifier Tests ============

class TestAlertNotifier:
    def test_init(self):
        notifier = AlertNotifier(webhook_url="https://example.com/webhook")
        assert notifier.webhook_url == "https://example.com/webhook"

    def test_print_alert(self, capsys):
        notifier = AlertNotifier()
        alert = Alert(
            type="test_alert",
            severity="high",
            wallet="0x1234567890abcdef",
            market_id="market_1",
            market_slug="test-market",
            details={"message": "Test message"}
        )

        notifier._print_alert(alert)
        captured = capsys.readouterr()
        assert "test_alert" in captured.out.lower()
        assert "test-market" in captured.out

    @patch('polymarket_monitor.requests.post')
    def test_send_webhook(self, mock_post):
        notifier = AlertNotifier(webhook_url="https://discord.com/api/webhooks/test")
        alert = Alert(
            type="large_bet",
            severity="critical",
            wallet="0xabc",
            market_id="market_1",
            market_slug="test-market",
            details={"amount_usd": 10000, "message": "Large bet detected"}
        )

        notifier._send_webhook(alert)
        mock_post.assert_called_once()

    @patch('polymarket_monitor.requests.post')
    def test_send_telegram(self, mock_post):
        notifier = AlertNotifier(telegram_config={
            "bot_token": "123456:ABC",
            "chat_id": "-1001234567890"
        })
        alert = Alert(
            type="new_wallet",
            severity="high",
            wallet="0xdef",
            market_id="market_2",
            market_slug="another-market",
            details={"amount_usd": 5000, "message": "New wallet"}
        )

        notifier._send_telegram(alert)
        mock_post.assert_called_once()


# ============ PolymarketMonitor Tests ============

class TestPolymarketMonitor:
    def test_init(self):
        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            assert monitor.client is not None
            assert monitor.detector is not None
            assert monitor.notifier is not None

    def test_parse_trade_valid(self):
        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            raw = {
                "id": "trade_123",
                "maker": "0xmaker",
                "side": "buy",
                "outcome": "Yes",
                "size": "100",
                "price": "0.5",
                "timestamp": "2025-01-01T12:00:00Z"
            }
            market = {"id": "market_1", "slug": "test-market"}

            trade = monitor._parse_trade(raw, market)

            assert trade is not None
            assert trade.id == "trade_123"
            assert trade.wallet == "0xmaker"
            assert trade.amount_usd == 50.0  # 100 * 0.5

    def test_parse_trade_invalid(self):
        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            raw = {"invalid": "data", "price": "not_a_number"}
            market = {"id": "market_1"}

            trade = monitor._parse_trade(raw, market)
            assert trade is None

    @patch.object(PolymarketClient, 'get_markets')
    @patch.object(PolymarketClient, 'get_recent_trades')
    def test_poll_markets(self, mock_get_trades, mock_get_markets):
        mock_get_markets.return_value = [
            {"id": "market_1", "slug": "test-1",
             "clobTokenIds": '["token_1", "token_2"]'}
        ]
        mock_get_trades.return_value = [
            {"id": "t1", "asset": "token_1", "proxyWallet": "0x1", "side": "buy",
             "outcome": "Yes", "size": "100", "price": "0.5", "timestamp": 1704110400,
             "transactionHash": "0xabc123"}
        ]

        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            monitor._poll_markets()

        mock_get_markets.assert_called_once()
        mock_get_trades.assert_called()


# ============ Integration Tests ============

class TestIntegration:
    def test_full_flow_new_wallet_alert(self, temp_seen_trades_file):
        """Test complete flow: trade -> detection -> alert"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)

        with patch.object(PolymarketClient, 'get_wallet_history') as mock_wallet:
            mock_wallet.return_value = {"first_seen": None, "total_trades": 0, "total_volume": 0}

            client = PolymarketClient(rate_limiter=RateLimiter(rps=100, burst=100))
            detector = AnomalyDetector(client, seen_trades_store=store)

            trade = Trade(
                id="integration_trade_1",
                market_id="m1",
                market_slug="integration-test",
                wallet="0xnewintegrationwallet",
                side="buy",
                outcome="Yes",
                amount_usd=8000.0,
                price=0.6,
                timestamp=datetime.now(),
            )

            alerts = detector.analyze_trade(trade)

            assert len(alerts) >= 1
            assert any(a.type == "new_wallet" for a in alerts)

    def test_persistence_across_instances(self, temp_seen_trades_file):
        """Test that seen trades persist across detector instances"""
        trade_id = "persist_trade_1"

        # First instance - add trade
        store1 = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert store1.add(trade_id) is True  # Should be new
        store1.save()

        # Verify file was written
        with open(temp_seen_trades_file, 'r') as f:
            saved_data = json.load(f)
        assert trade_id in saved_data

        # Second instance (simulating restart) - should find the trade
        store2 = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        assert store2.contains(trade_id) is True  # Should exist from persistence
        assert store2.add(trade_id) is False  # Should already exist

    def test_detector_skips_persisted_trades(self, temp_seen_trades_file):
        """Test that detector skips trades that were persisted from previous run"""
        trade_id = "detector_persist_trade"

        # Pre-populate the store file
        store1 = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        store1.add(trade_id)
        store1.save()

        # Create new detector with fresh store that loads from file
        store2 = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        mock_client = MagicMock(spec=PolymarketClient)
        mock_client.get_wallet_history.return_value = {
            "first_seen": None, "total_trades": 0, "total_volume": 0
        }
        detector = AnomalyDetector(mock_client, seen_trades_store=store2)

        trade = Trade(
            id=trade_id,  # Same ID as persisted
            market_id="m1",
            market_slug="test",
            wallet="0xwallet",
            side="buy",
            outcome="Yes",
            amount_usd=10000.0,
            price=0.5,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(trade)
        assert len(alerts) == 0  # Skipped because already persisted


# ============ ColoredConsoleFormatter Tests ============

class TestColoredConsoleFormatter:
    def test_format_warning_adds_yellow(self):
        import logging
        formatter = ColoredConsoleFormatter('%(message)s')
        record = logging.LogRecord(
            name='test', level=logging.WARNING, pathname='', lineno=0,
            msg='Test warning', args=(), exc_info=None
        )
        result = formatter.format(record)
        assert '\033[93m' in result  # Yellow
        assert '\033[0m' in result   # Reset

    def test_format_critical_adds_red(self):
        import logging
        formatter = ColoredConsoleFormatter('%(message)s')
        record = logging.LogRecord(
            name='test', level=logging.CRITICAL, pathname='', lineno=0,
            msg='Test critical', args=(), exc_info=None
        )
        result = formatter.format(record)
        assert '\033[91m' in result  # Red
        assert '\033[0m' in result   # Reset

    def test_format_info_no_color(self):
        import logging
        formatter = ColoredConsoleFormatter('%(message)s')
        record = logging.LogRecord(
            name='test', level=logging.INFO, pathname='', lineno=0,
            msg='Test info', args=(), exc_info=None
        )
        result = formatter.format(record)
        assert '\033[' not in result  # No color codes


class TestSetupLogging:
    def test_setup_logging_creates_logger(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.log', delete=False) as f:
            logger = setup_logging(f.name)
            assert logger is not None
            assert len(logger.handlers) == 2  # File + Console


# ============ Additional AlertNotifier Tests ============

class TestAlertNotifierLark:
    @patch('polymarket_monitor.requests.post')
    def test_send_lark_success(self, mock_post):
        mock_post.return_value.status_code = 200
        notifier = AlertNotifier(lark_webhook="https://open.larksuite.com/test")
        alert = Alert(
            type="new_wallet",
            severity="high",
            wallet="0x1234567890abcdef",
            market_id="market_1",
            market_slug="test-market",
            details={"amount_usd": 5000, "message": "Test alert"}
        )

        notifier._send_lark(alert, market_url="https://polymarket.com/event/test")
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert 'card' in call_args.kwargs['json']

    @patch('polymarket_monitor.requests.post')
    def test_send_lark_error_handling(self, mock_post):
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = "Bad request"
        notifier = AlertNotifier(lark_webhook="https://open.larksuite.com/test")
        alert = Alert(
            type="large_bet",
            severity="critical",
            wallet="0xabc",
            market_id="market_1",
            market_slug="test-market",
            details={"amount_usd": 10000, "message": "Large bet"}
        )

        # Should not raise
        notifier._send_lark(alert)

    @patch('polymarket_monitor.requests.post')
    def test_send_lark_exception_handling(self, mock_post):
        mock_post.side_effect = Exception("Network error")
        notifier = AlertNotifier(lark_webhook="https://open.larksuite.com/test")
        alert = Alert(
            type="repeat_entry",
            severity="medium",
            wallet="0xdef",
            market_id="market_1",
            market_slug="test-market",
            details={"message": "Repeat entry"}
        )

        # Should not raise
        notifier._send_lark(alert)

    def test_send_calls_lark_when_configured(self):
        with patch.object(AlertNotifier, '_print_alert'):
            with patch.object(AlertNotifier, '_send_lark') as mock_lark:
                notifier = AlertNotifier(lark_webhook="https://lark.test")
                alert = Alert(
                    type="test",
                    severity="low",
                    wallet="0x123",
                    market_id="m1",
                    market_slug="test",
                    details={}
                )
                notifier.send(alert, market_url="https://polymarket.com")
                mock_lark.assert_called_once()


# ============ Additional PolymarketClient Tests ============

class TestPolymarketClientAdditional:
    @patch('polymarket_monitor.requests.Session')
    def test_get_market_info_success(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "market_1", "question": "Test?"}
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_market_info("condition_123")
        assert result["id"] == "market_1"

    @patch('polymarket_monitor.requests.Session')
    def test_get_market_info_error(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_session.request.side_effect = Exception("API error")

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_market_info("condition_123")
        assert result == {}

    @patch('polymarket_monitor.requests.Session')
    def test_get_last_trade_price_success(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_response = MagicMock()
        mock_response.json.return_value = {"price": "0.75"}
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_last_trade_price("token_123")
        assert result == 0.75

    @patch('polymarket_monitor.requests.Session')
    def test_get_last_trade_price_error(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_session.request.side_effect = Exception("API error")

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_last_trade_price("token_123")
        assert result is None

    @patch('polymarket_monitor.requests.Session')
    def test_get_recent_trades_error(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_session.request.side_effect = Exception("API error")

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_recent_trades()
        assert result == []

    @patch('polymarket_monitor.requests.Session')
    def test_get_market_trades_error(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_session.request.side_effect = Exception("API error")

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_market_trades("token_123")
        assert result == []

    @patch('polymarket_monitor.requests.Session')
    def test_get_wallet_history_success(self, mock_session_class):
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"value": "100"},
            {"value": "200"}
        ]
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        result = client.get_wallet_history("0x123")
        assert result["total_trades"] == 2
        assert result["total_volume"] == 300

    @patch('polymarket_monitor.requests.Session')
    def test_get_markets_with_sorting_and_filtering(self, mock_session_class):
        from datetime import timezone
        mock_session = MagicMock()
        mock_session_class.return_value = mock_session

        # Mock events response
        future_date = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        mock_response = MagicMock()
        mock_response.json.side_effect = [
            # Events response
            [{"id": "event_1", "endDate": future_date, "markets": [
                {"id": "m1", "volume": "5000", "clobTokenIds": '["t1"]'}
            ]}],
            # Markets response
            [{"id": "m2", "volume": "10000", "endDate": future_date}]
        ]
        mock_response.raise_for_status = MagicMock()
        mock_session.request.return_value = mock_response

        limiter = RateLimiter(rps=100, burst=100)
        client = PolymarketClient(rate_limiter=limiter)
        client.session = mock_session

        markets = client.get_markets(limit=10, sort_by_volume=True, max_end_days=30)
        # Should have combined and deduplicated markets
        assert isinstance(markets, list)


# ============ Additional AnomalyDetector Tests ============

class TestAnomalyDetectorAdditional:
    def test_new_wallet_with_few_trades(self, mock_client, temp_seen_trades_file):
        """Wallet with < 5 trades is considered new"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        mock_client.get_wallet_history.return_value = {
            "first_seen": (datetime.now() - timedelta(days=30)).isoformat(),
            "total_trades": 3,  # Less than 5
            "total_volume": 1000
        }

        trade = Trade(
            id="trade_few_trades",
            market_id="m1",
            market_slug="test",
            wallet="0xfewtradeswallet",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(trade)
        new_wallet_alerts = [a for a in alerts if a.type == "new_wallet"]
        assert len(new_wallet_alerts) >= 1

    def test_new_wallet_recent_first_seen(self, mock_client, temp_seen_trades_file):
        """Wallet with recent first_seen (< 7 days) is considered new"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        mock_client.get_wallet_history.return_value = {
            "first_seen": (datetime.now() - timedelta(days=3)).isoformat(),
            "total_trades": 10,
            "total_volume": 5000
        }

        trade = Trade(
            id="trade_recent_wallet",
            market_id="m1",
            market_slug="test",
            wallet="0xrecentwallet",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(trade)
        new_wallet_alerts = [a for a in alerts if a.type == "new_wallet"]
        assert len(new_wallet_alerts) >= 1

    def test_old_wallet_no_alert(self, mock_client, temp_seen_trades_file):
        """Old wallet with many trades should not trigger new_wallet alert"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        mock_client.get_wallet_history.return_value = {
            "first_seen": (datetime.now() - timedelta(days=60)).isoformat(),
            "total_trades": 100,
            "total_volume": 50000
        }

        trade = Trade(
            id="trade_old_wallet",
            market_id="m1",
            market_slug="test",
            wallet="0xoldwallet",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(trade)
        new_wallet_alerts = [a for a in alerts if a.type == "new_wallet"]
        assert len(new_wallet_alerts) == 0

    def test_critical_severity_for_very_large_bet(self, mock_client, temp_seen_trades_file):
        """Very large bet from new wallet should be critical severity"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        mock_client.get_wallet_history.return_value = {
            "first_seen": None,
            "total_trades": 0,
            "total_volume": 0
        }

        trade = Trade(
            id="trade_critical",
            market_id="m1",
            market_slug="test",
            wallet="0xcriticalwallet",
            side="buy",
            outcome="Yes",
            amount_usd=15000.0,  # > 2x threshold
            price=0.5,
            timestamp=datetime.now(),
        )

        alerts = detector.analyze_trade(trade)
        new_wallet_alerts = [a for a in alerts if a.type == "new_wallet"]
        assert len(new_wallet_alerts) >= 1
        assert new_wallet_alerts[0].severity == "critical"


# ============ PolymarketMonitor Additional Tests ============

class TestPolymarketMonitorAdditional:
    def test_parse_trade_with_proxy_wallet(self):
        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            raw = {
                "transactionHash": "0xhash123",
                "proxyWallet": "0xproxywallet",
                "side": "buy",
                "outcome": "Yes",
                "size": "200",
                "price": "0.6",
                "timestamp": 1704110400  # Unix timestamp
            }
            market = {"id": "market_1", "slug": "test-market", "conditionId": "cond_1"}

            trade = monitor._parse_trade(raw, market)

            assert trade is not None
            assert trade.wallet == "0xproxywallet"
            assert trade.id == "0xhash123"

    def test_parse_trade_with_timestamp_string(self):
        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            raw = {
                "id": "trade_456",
                "owner": "0xowner",
                "side": "sell",
                "outcome": "No",
                "size": "50",
                "price": "0.3",
                "timestamp": "2025-01-01T12:00:00Z"
            }
            market = {"conditionId": "cond_1", "slug": "test"}

            trade = monitor._parse_trade(raw, market)

            assert trade is not None
            assert trade.side == "sell"
            assert trade.outcome == "No"

    def test_init_with_lark_webhook(self):
        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor(lark_webhook="https://lark.test/webhook")
            assert monitor.notifier.lark_webhook == "https://lark.test/webhook"

    @patch.object(PolymarketClient, 'get_markets')
    @patch.object(PolymarketClient, 'get_recent_trades')
    def test_poll_markets_with_cached_markets(self, mock_get_trades, mock_get_markets):
        mock_get_markets.return_value = [
            {"id": "m1", "slug": "test", "clobTokenIds": '["t1"]', "question": "Test?"}
        ]
        mock_get_trades.return_value = []

        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            monitor._cached_markets = [
                {"id": "m1", "slug": "cached", "clobTokenIds": '["t1"]'}
            ]
            monitor._poll_count = 5  # Not a refresh cycle

            monitor._poll_markets()

            # Should not call get_markets because cached
            mock_get_markets.assert_not_called()

    @patch.object(PolymarketClient, 'get_markets')
    @patch.object(PolymarketClient, 'get_recent_trades')
    def test_poll_markets_empty_markets(self, mock_get_trades, mock_get_markets):
        mock_get_markets.return_value = []

        with patch.object(SeenTradesStore, '_load'):
            monitor = PolymarketMonitor()
            monitor._poll_markets()

            mock_get_trades.assert_not_called()

    @patch.object(PolymarketClient, 'get_markets')
    @patch.object(PolymarketClient, 'get_recent_trades')
    def test_poll_markets_with_alerts(self, mock_get_trades, mock_get_markets):
        mock_get_markets.return_value = [
            {"id": "m1", "slug": "alert-market", "question": "Alert test?",
             "clobTokenIds": '["token_alert"]', "_event_slug": "event-slug"}
        ]
        mock_get_trades.return_value = [
            {"id": "t1", "asset": "token_alert", "proxyWallet": "0xnewwallet123",
             "side": "buy", "outcome": "Yes", "size": "10000", "price": "0.6",
             "timestamp": 1704110400, "transactionHash": "0xalerttx"}
        ]

        with patch.object(SeenTradesStore, '_load'):
            with patch.object(AlertNotifier, 'send') as mock_send:
                with patch.object(PolymarketClient, 'get_wallet_history') as mock_wallet:
                    mock_wallet.return_value = {
                        "first_seen": None,
                        "total_trades": 0,
                        "total_volume": 0
                    }
                    monitor = PolymarketMonitor()
                    monitor._poll_markets()

                    # Should have triggered alerts and called send
                    assert mock_send.called


# ============ Tests for Bug Fixes ============

class TestDuplicateNewWalletAlertPrevention:
    """Tests for fix: duplicate NEW_WALLET alerts for same wallet"""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock(spec=PolymarketClient)
        client.get_wallet_history.return_value = {
            "first_seen": None,
            "total_trades": 0,
            "total_volume": 0
        }
        return client

    @pytest.fixture
    def temp_seen_trades_file(self, tmp_path):
        return tmp_path / "seen_trades_dup.json"

    def test_no_duplicate_new_wallet_alerts(self, mock_client, temp_seen_trades_file):
        """Same wallet should only trigger NEW_WALLET alert once"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        wallet = "0xduplicatewallet"

        # First trade - should trigger new_wallet alert
        trade1 = Trade(
            id="trade_dup_1",
            market_id="m1",
            market_slug="test",
            wallet=wallet,
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )
        alerts1 = detector.analyze_trade(trade1)
        new_wallet_alerts1 = [a for a in alerts1 if a.type == "new_wallet"]
        assert len(new_wallet_alerts1) == 1, "First trade should trigger new_wallet alert"

        # Second trade from same wallet - should NOT trigger new_wallet alert
        trade2 = Trade(
            id="trade_dup_2",
            market_id="m1",
            market_slug="test",
            wallet=wallet,
            side="buy",
            outcome="Yes",
            amount_usd=7000.0,
            price=0.6,
            timestamp=datetime.now(),
        )
        alerts2 = detector.analyze_trade(trade2)
        new_wallet_alerts2 = [a for a in alerts2 if a.type == "new_wallet"]
        assert len(new_wallet_alerts2) == 0, "Second trade should NOT trigger new_wallet alert"

    def test_different_wallets_both_get_alerts(self, mock_client, temp_seen_trades_file):
        """Different wallets should each get their own NEW_WALLET alert"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        # First wallet
        trade1 = Trade(
            id="trade_w1",
            market_id="m1",
            market_slug="test",
            wallet="0xwallet1",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )
        alerts1 = detector.analyze_trade(trade1)
        assert len([a for a in alerts1 if a.type == "new_wallet"]) == 1

        # Second wallet - should also trigger alert
        trade2 = Trade(
            id="trade_w2",
            market_id="m1",
            market_slug="test",
            wallet="0xwallet2",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )
        alerts2 = detector.analyze_trade(trade2)
        assert len([a for a in alerts2 if a.type == "new_wallet"]) == 1

    def test_alerted_new_wallets_tracking(self, mock_client, temp_seen_trades_file):
        """Verify that alerted_new_wallets set is properly maintained"""
        store = SeenTradesStore(filepath=str(temp_seen_trades_file), ttl_hours=1)
        detector = AnomalyDetector(mock_client, seen_trades_store=store)

        assert len(detector.alerted_new_wallets) == 0

        trade = Trade(
            id="trade_track",
            market_id="m1",
            market_slug="test",
            wallet="0xtrackwallet",
            side="buy",
            outcome="Yes",
            amount_usd=6000.0,
            price=0.5,
            timestamp=datetime.now(),
        )
        detector.analyze_trade(trade)

        assert "0xtrackwallet" in detector.alerted_new_wallets


class TestLarkNotificationAmountFallback:
    """Tests for fix: $0 amount display in Lark notifications for repeat_entry alerts"""

    @patch('polymarket_monitor.requests.post')
    def test_lark_uses_total_amount_for_repeat_entry(self, mock_post):
        """Lark notification should use total_amount for repeat_entry alerts"""
        mock_post.return_value = MagicMock(status_code=200)

        notifier = AlertNotifier(lark_webhook="https://open.larksuite.com/test")
        alert = Alert(
            type="repeat_entry",
            severity="medium",
            wallet="0xrepeatwallet",
            market_id="market_1",
            market_slug="test-market",
            details={
                "trade_count": 5,
                "total_amount": 12345,  # repeat_entry uses total_amount, not amount_usd
                "window_hours": 24,
                "message": "🔄 Repeat entry! 5 trades totaling $12,345 in 24 hours"
            }
        )

        notifier._send_lark(alert)

        # Verify the request was made
        assert mock_post.called
        call_args = mock_post.call_args
        card = call_args[1]['json']

        # Find the Amount field in the card
        elements = card['card']['elements']
        amount_found = False
        for element in elements:
            if element.get('tag') == 'div' and 'fields' in element:
                for field in element['fields']:
                    content = field.get('text', {}).get('content', '')
                    if '**Amount**' in content:
                        amount_found = True
                        # Should show $12,345, not $0
                        assert '$12,345' in content, f"Expected $12,345 in content, got: {content}"

        assert amount_found, "Amount field not found in Lark card"

    @patch('polymarket_monitor.requests.post')
    def test_lark_uses_amount_usd_for_new_wallet(self, mock_post):
        """Lark notification should use amount_usd for new_wallet alerts"""
        mock_post.return_value = MagicMock(status_code=200)

        notifier = AlertNotifier(lark_webhook="https://open.larksuite.com/test")
        alert = Alert(
            type="new_wallet",
            severity="critical",
            wallet="0xnewwallet",
            market_id="market_1",
            market_slug="test-market",
            details={
                "amount_usd": 8888,  # new_wallet uses amount_usd
                "outcome": "Yes",
                "price": 0.65,
                "wallet_age_trades": 0,
                "message": "🚨 New wallet large bet! $8,888 on Yes @ 0.65"
            }
        )

        notifier._send_lark(alert)

        assert mock_post.called
        call_args = mock_post.call_args
        card = call_args[1]['json']

        elements = card['card']['elements']
        for element in elements:
            if element.get('tag') == 'div' and 'fields' in element:
                for field in element['fields']:
                    content = field.get('text', {}).get('content', '')
                    if '**Amount**' in content:
                        assert '$8,888' in content, f"Expected $8,888 in content, got: {content}"

    @patch('polymarket_monitor.requests.post')
    def test_lark_handles_missing_amount_gracefully(self, mock_post):
        """Lark notification should show $0 if neither amount_usd nor total_amount exists"""
        mock_post.return_value = MagicMock(status_code=200)

        notifier = AlertNotifier(lark_webhook="https://open.larksuite.com/test")
        alert = Alert(
            type="unknown_type",
            severity="low",
            wallet="0xwallet",
            market_id="market_1",
            market_slug="test-market",
            details={
                "message": "Some alert"
                # No amount_usd or total_amount
            }
        )

        notifier._send_lark(alert)

        assert mock_post.called
        call_args = mock_post.call_args
        card = call_args[1]['json']

        elements = card['card']['elements']
        for element in elements:
            if element.get('tag') == 'div' and 'fields' in element:
                for field in element['fields']:
                    content = field.get('text', {}).get('content', '')
                    if '**Amount**' in content:
                        assert '$0' in content, f"Expected $0 in content, got: {content}"


class TestSkipResolvedPrices:
    """Test that trades at near-resolved prices are filtered out"""

    def test_skip_high_price_trade(self, mock_client):
        """Trades at price >= 0.95 should be skipped"""
        detector = AnomalyDetector(mock_client)
        
        # Trade at price 0.99 - should be skipped
        trade = Trade(
            id="resolved_001",
            market_id="market_123",
            market_slug="fed-rate-market",
            wallet="0xnewwallet123",
            side="buy",
            outcome="No",
            amount_usd=15000.0,  # Would normally trigger alerts
            price=0.99,
            timestamp=datetime.now(),
        )
        
        alerts = detector.analyze_trade(trade)
        assert len(alerts) == 0, "Trades at 0.99 price should be skipped"

    def test_skip_low_price_trade(self, mock_client):
        """Trades at price <= 0.05 should be skipped"""
        detector = AnomalyDetector(mock_client)
        
        # Trade at price 0.03 - should be skipped
        trade = Trade(
            id="resolved_002",
            market_id="market_123",
            market_slug="fed-rate-market",
            wallet="0xnewwallet456",
            side="buy",
            outcome="Yes",
            amount_usd=15000.0,
            price=0.03,
            timestamp=datetime.now(),
        )
        
        alerts = detector.analyze_trade(trade)
        assert len(alerts) == 0, "Trades at 0.03 price should be skipped"

    def test_normal_price_not_skipped(self, mock_client):
        """Trades at normal prices should still trigger alerts"""
        detector = AnomalyDetector(mock_client)
        
        # Trade at price 0.50 - should NOT be skipped
        trade = Trade(
            id="normal_001",
            market_id="market_123",
            market_slug="fed-rate-market",
            wallet="0xnewwallet789",
            side="buy",
            outcome="Yes",
            amount_usd=15000.0,
            price=0.50,
            timestamp=datetime.now(),
        )
        
        alerts = detector.analyze_trade(trade)
        assert len(alerts) > 0, "Trades at normal prices should trigger alerts"

    def test_boundary_price_095_skipped(self, mock_client):
        """Trades at exactly 0.95 should be skipped"""
        detector = AnomalyDetector(mock_client)
        
        trade = Trade(
            id="boundary_001",
            market_id="market_123",
            market_slug="fed-rate-market",
            wallet="0xboundarywallet",
            side="buy",
            outcome="No",
            amount_usd=15000.0,
            price=0.95,
            timestamp=datetime.now(),
        )
        
        alerts = detector.analyze_trade(trade)
        assert len(alerts) == 0, "Trades at exactly 0.95 should be skipped"

    def test_boundary_price_094_not_skipped(self, mock_client):
        """Trades at 0.94 should NOT be skipped"""
        detector = AnomalyDetector(mock_client)
        
        trade = Trade(
            id="boundary_002",
            market_id="market_123",
            market_slug="fed-rate-market",
            wallet="0xboundarywallet2",
            side="buy",
            outcome="Yes",
            amount_usd=15000.0,
            price=0.94,
            timestamp=datetime.now(),
        )
        
        alerts = detector.analyze_trade(trade)
        assert len(alerts) > 0, "Trades at 0.94 should NOT be skipped"
