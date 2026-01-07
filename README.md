# Polymarket Abnormal Trade Monitor

A Python script that monitors Polymarket for suspicious trading activity and potential insider trading signals.

## Features

| Detection Type | Description | Default Threshold |
|----------------|-------------|-------------------|
| New Wallet Large Bets | Wallets with no history suddenly placing large bets | $5,000 |
| Abnormally Large Trades | Single trades far exceeding market average | $10,000 or 5x average |
| Repeat Entries | Same wallet repeatedly entering the same market | 3 times within 24 hours |

## Quick Start

### Install Dependencies

```bash
pip install requests
```

### Basic Usage

```bash
python polymarket_monitor.py
```

### With Notifications

```bash
# Discord/Slack Webhook
python polymarket_monitor.py --webhook "https://discord.com/api/webhooks/xxx"

# Telegram
python polymarket_monitor.py \
    --telegram-token "123456:ABC-DEF" \
    --telegram-chat "-1001234567890"

# Lark/Feishu
python polymarket_monitor.py --lark-webhook "https://open.larksuite.com/open-apis/bot/v2/hook/xxx"
```

### Custom Thresholds

```bash
python polymarket_monitor.py \
    --min-amount 3000 \
    --large-bet 8000 \
    --interval 15
```

### Monitor Specific Markets

```bash
python polymarket_monitor.py --markets "0x123..." "0x456..."
```

## CLI Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--min-amount` | New wallet alert threshold (USD) | 5000 |
| `--large-bet` | Large trade threshold (USD) | 10000 |
| `--interval` | Polling interval (seconds) | 30 |
| `--num-markets` | Number of markets to monitor (by volume) | 50 |
| `--max-days` | Only monitor markets ending within N days | 30 |
| `--log-file` | Trade log file path | trades.log |
| `--webhook` | Discord/Slack Webhook URL | - |
| `--telegram-token` | Telegram Bot Token | - |
| `--telegram-chat` | Telegram Chat ID | - |
| `--lark-webhook` | Lark/Feishu Webhook URL | - |
| `--markets` | Specific market IDs to monitor | All |

## Important Notes

1. **API Rate Limits**: Polymarket may have rate limits; recommended interval is at least 15 seconds
2. **Not Financial Advice**: Alerts are signals only, not investment recommendations
3. **Manual Review Required**: The script only alerts; humans must decide whether to act
4. **Risk Warning**: Prediction markets are volatile; invest cautiously

## Advanced Configuration

Modify the `CONFIG` dict at the top of the script for more options:

```python
CONFIG = {
    "NEW_WALLET_THRESHOLD_USD": 5000,      # New wallet threshold
    "LARGE_BET_THRESHOLD_USD": 10000,      # Large trade threshold
    "LARGE_BET_MULTIPLIER": 5,             # Multiplier above average = anomaly
    "REPEAT_ENTRY_COUNT": 3,               # Repeat entry count threshold
    "REPEAT_ENTRY_WINDOW_HOURS": 24,       # Time window for repeat detection
    "WALLET_AGE_THRESHOLD_DAYS": 7,        # Days to consider wallet "new"
    "POLL_INTERVAL_SECONDS": 30,           # Polling interval
}
```

## Output Example

```
============================================================
[CRITICAL] new_wallet
Time: 2025-01-06 15:30:45
Market: will-trump-win-2024
Wallet: 0x1234...abcd
Details: New wallet large bet! $15,000 on Yes @ 0.65
============================================================
```

## Future Improvements

1. **On-chain Analysis**: Combine with blockchain data to analyze wallet relationships
2. **Machine Learning**: Train models to identify more complex anomaly patterns
3. **Auto-trading**: Connect to trading API for automated copy-trading (at your own risk)
4. **Database Storage**: Save historical data for backtesting analysis

## Testing

```bash
# Run tests with coverage
pytest tests/ -v --cov=polymarket_monitor --cov-report=term-missing
```

Current coverage: 81%

## License

MIT
