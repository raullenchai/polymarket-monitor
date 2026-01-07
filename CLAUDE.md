# CLAUDE.md

## Project Overview

This is a **Polymarket abnormal trading monitor** (老鼠仓检测) that detects potential insider trading signals on the Polymarket prediction market platform.

The script monitors for:
1. **New wallet large bets** - Wallets with no history suddenly placing large bets
2. **Abnormally large trades** - Single trades far exceeding market average
3. **Repeat entries** - Same wallet repeatedly entering the same market in short timeframes

## Tech Stack

- **Language**: Python 3.10+
- **Dependencies**: `requests` (only external dependency)
- **APIs Used**:
  - Polymarket CLOB API: `https://clob.polymarket.com`
  - Polymarket Gamma API: `https://gamma-api.polymarket.com`

## Project Structure

```
.
├── polymarket_monitor.py   # Main script (single file)
├── README.md               # User documentation
└── CLAUDE.md               # This file
```

## Key Classes

| Class | Purpose |
|-------|---------|
| `PolymarketClient` | API client for fetching markets, trades, wallet history |
| `AnomalyDetector` | Core detection engine with 3 detection methods |
| `AlertNotifier` | Sends alerts to console, Discord/Slack webhook, Telegram |
| `PolymarketMonitor` | Main orchestrator that polls markets and processes trades |

## Data Structures

```python
@dataclass
class Trade:
    id, market_id, market_slug, wallet, side, outcome, amount_usd, price, timestamp

@dataclass  
class Alert:
    type, severity, wallet, market_id, market_slug, details, timestamp
```

## Configuration

All thresholds are in the `CONFIG` dict at the top of the script:

```python
CONFIG = {
    "NEW_WALLET_THRESHOLD_USD": 5000,
    "LARGE_BET_THRESHOLD_USD": 10000,
    "LARGE_BET_MULTIPLIER": 5,
    "REPEAT_ENTRY_COUNT": 3,
    "REPEAT_ENTRY_WINDOW_HOURS": 24,
    "WALLET_AGE_THRESHOLD_DAYS": 7,
    "POLL_INTERVAL_SECONDS": 30,
}
```

## Common Development Tasks

### Adding a new detection method

1. Add a new method `_check_xxx(self, trade: Trade) -> Optional[Alert]` in `AnomalyDetector`
2. Call it from `analyze_trade()` method
3. Define appropriate alert type and severity

### Adding a new notification channel

1. Add config parameters to `AlertNotifier.__init__()`
2. Implement `_send_xxx(self, alert: Alert)` method
3. Call it from `send()` method

### Modifying API endpoints

The Polymarket API may change. Key endpoints are in `PolymarketClient`:
- `get_markets()` - List active markets
- `get_market_trades()` - Get trades for a token
- `get_wallet_history()` - Check if wallet is new

## Code Style

- Use type hints for function signatures
- Dataclasses for structured data
- f-strings for formatting
- Chinese comments are acceptable (this is for a Chinese-speaking user)
- Keep it single-file for simplicity unless it grows significantly

## Known Issues / TODOs

- [ ] Polymarket API endpoints may need updating based on their latest docs
- [ ] `get_wallet_history()` endpoint is a guess - needs verification
- [ ] Add rate limiting to avoid API throttling
- [ ] Add persistent storage for trade history (currently in-memory only)
- [ ] Add backtesting capability with historical data
- [ ] Consider adding whale wallet tracking (known big players)

## Testing

Currently no tests. To add tests:
```bash
# Suggested structure
tests/
├── test_detector.py
├── test_client.py
└── fixtures/
    └── sample_trades.json
```

Mock the API responses for unit testing the detection logic.

## Running the Script

```bash
# Basic
python polymarket_monitor.py

# With notifications
python polymarket_monitor.py --webhook "https://discord.com/api/webhooks/..."

# Custom thresholds
python polymarket_monitor.py --min-amount 3000 --large-bet 8000 --interval 15
```

## API Documentation References

- Polymarket CLOB API: https://docs.polymarket.com/
- Polymarket Gamma API: https://gamma-api.polymarket.com/docs (if available)

## Important Notes for Claude Code

1. **Single file architecture** - Keep everything in `polymarket_monitor.py` unless there's a strong reason to split
2. **Minimal dependencies** - Only `requests` is required, avoid adding unnecessary deps
3. **User is Chinese-speaking** - Chinese comments and output messages are fine
4. **This is a monitoring tool, not auto-trading** - It alerts only, human makes the decision
5. **API may be outdated** - Polymarket APIs change; if something doesn't work, check their latest docs first
