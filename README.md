# Polymarket 老鼠仓监控脚本

## 🎯 功能说明

这个脚本通过 API 监控 Polymarket 上的异常交易活动，检测潜在的内幕交易（"老鼠仓"）信号。

### 检测指标

| 指标 | 说明 | 默认阈值 |
|------|------|----------|
| 🆕 新钱包大额下注 | 无历史记录的钱包突然大额进场 | $5,000 |
| 💰 异常大额交易 | 单笔金额远超市场平均 | $10,000 或 5x 平均 |
| 🔄 重复进场 | 同一钱包在短时间内反复加仓 | 24小时内3次 |

## 🚀 快速开始

### 安装依赖

```bash
pip install requests
```

### 基础运行

```bash
python polymarket_monitor.py
```

### 带通知运行

```bash
# Discord/Slack Webhook
python polymarket_monitor.py --webhook "https://discord.com/api/webhooks/xxx"

# Telegram
python polymarket_monitor.py \
    --telegram-token "123456:ABC-DEF" \
    --telegram-chat "-1001234567890"
```

### 自定义阈值

```bash
python polymarket_monitor.py \
    --min-amount 3000 \
    --large-bet 8000 \
    --interval 15
```

### 监控特定市场

```bash
python polymarket_monitor.py --markets "0x123..." "0x456..."
```

## 📊 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--min-amount` | 新钱包警报阈值 (USD) | 5000 |
| `--large-bet` | 大额交易阈值 (USD) | 10000 |
| `--interval` | 监控间隔 (秒) | 30 |
| `--webhook` | Discord/Slack Webhook URL | - |
| `--telegram-token` | Telegram Bot Token | - |
| `--telegram-chat` | Telegram Chat ID | - |
| `--markets` | 指定监控的市场ID | 全部 |

## ⚠️ 重要提示

1. **API 限制**: Polymarket 可能有请求频率限制，建议 interval 不低于 15 秒
2. **仅供参考**: 警报仅作为信号参考，不构成投资建议
3. **人工复核**: 脚本只负责报警，需要人工判断是否跟单
4. **风险自担**: 预测市场波动大，投资需谨慎

## 🔧 进阶配置

修改脚本顶部的 `CONFIG` 字典可以调整更多参数：

```python
CONFIG = {
    "NEW_WALLET_THRESHOLD_USD": 5000,      # 新钱包阈值
    "LARGE_BET_THRESHOLD_USD": 10000,      # 大额交易阈值
    "LARGE_BET_MULTIPLIER": 5,             # 超过平均X倍视为异常
    "REPEAT_ENTRY_COUNT": 3,               # 重复进场次数
    "REPEAT_ENTRY_WINDOW_HOURS": 24,       # 时间窗口
    "WALLET_AGE_THRESHOLD_DAYS": 7,        # 新钱包定义
    "POLL_INTERVAL_SECONDS": 30,           # 轮询间隔
}
```

## 📝 输出示例

```
============================================================
[CRITICAL] new_wallet
时间: 2025-01-06 15:30:45
市场: will-trump-win-2024
钱包: 0x1234...abcd
详情: 🚨 新钱包大额下注! $15,000 on Yes @ 0.65
============================================================
```

## 🤝 扩展建议

1. **添加更多数据源**: 结合链上数据分析钱包关联
2. **机器学习**: 训练模型识别更复杂的异常模式
3. **自动交易**: 可以对接交易 API 实现自动跟单（风险自担）
4. **数据库存储**: 保存历史数据用于回测分析
