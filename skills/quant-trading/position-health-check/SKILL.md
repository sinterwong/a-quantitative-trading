---
name: position-health-check
description: Use when checking current holdings for risk — returns unrealized P&L, RSI overbought/oversold, and overdue positions. Inputs: none (reads all current positions). Triggers: "检查持仓", "持仓健康", "持仓预警", or any portfolio risk inquiry.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [quant-trading, portfolio, position-risk, risk-management, unrealized-pnl]
    related_skills: [stock-analyst, trade-review, sector-rotation]
---

# Position Health Check（持仓健康检查）

## Overview

检查当前所有持仓的浮盈/浮亏状态、RSI 超买超卖、持仓时长是否超限，输出预警信号。是持仓风险管理的核心工具。

**输入**: 无（自动读取当前持仓）
**输出**: 各持仓状态摘要 + 预警列表
**推送**: 触发预警时立即推送飞书

---

## API 超时配置

| 接口 | 建议超时 | 说明 |
|------|----------|------|
| `/positions` | 10s | 持仓列表 |
| `/portfolio/summary` | 10s | 组合摘要 |
| `/data/realtime/{symbol}` | 10s | 实时行情 |
| `/data/daily/{symbol}` | 15s | 日线数据（计算RSI） |

---

## API 调用链

```
1. GET  /positions                         → 当前持仓列表（代码/数量/成本/entry_date）
2. GET  /portfolio/summary                 → 浮盈浮亏/现金/总权益（持仓数据嵌在此）
3. GET  /data/realtime/{symbol}            → 各持仓最新价（支持多标的逗号分隔）
4. GET  /data/daily/{symbol}?days=20      → 近20日K线（计算RSI）
```

**Base URL**: `http://localhost:5555`
**认证**: `X-API-Key` header（本地开发可省略）

### 实测返回格式

**GET /positions** 和 **GET /portfolio/summary**
```json
{
  "positions": [
    {
      "symbol": "600900.SH",
      "shares": 3700,
      "entry_price": 26.702,
      "latest_price": 26.58,
      "cost_value": 98797.4,
      "current_value": 98346.0,
      "unrealized_pnl": -451.4,
      "unrealized_pnl_pct": -0.46,
      "entry_date": "2025-02-28"
    }
  ],
  "cash": 151062.6,
  "position_value": 329596.0,
  "position_cost": 348937.4
}
```

⚠️ `/data/realtime/{symbol}` 路径实测正常，但 `/data/realtime/{symbol1},{symbol2}` 多标的逗号分隔方式未验证，建议循环调用。

---

## 快速调用示例

```bash
# 持仓列表
curl -s "http://localhost:5555/positions"

# 组合摘要
curl -s "http://localhost:5555/portfolio/summary"

# 持仓实时行情（单标的）
curl -s "http://localhost:5555/data/realtime/600900.SH"

# 持仓日线（计算RSI）
curl -s "http://localhost:5555/data/daily/600900.SH?days=20"
```

```python
import requests
from datetime import date, datetime

BASE = "http://localhost:5555"

def get_positions() -> dict:
    return requests.get(f"{BASE}/positions", timeout=10).json()

def get_portfolio_summary() -> dict:
    return requests.get(f"{BASE}/portfolio/summary", timeout=10).json()

def get_realtime(symbols: list[str]) -> dict:
    # 建议逐个标的调用，避免多标的解析问题
    results = {}
    for sym in symbols:
        r = requests.get(f"{BASE}/data/realtime/{sym}", timeout=10).json()
        results[sym] = r
    return results

def get_daily(symbol: str, days: int = 20) -> dict:
    return requests.get(f"{BASE}/data/daily/{symbol}", params={"days": days}, timeout=15).json()

def calc_rsi(closes: list, period: int = 14) -> float | None:
    """计算RSI指标"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i+1] - closes[i]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)
```

---

## 预警规则

### 浮亏预警

| 条件 | 预警级别 | 说明 |
|------|----------|------|
| 浮亏 > 5% | 🟡 黄色 | 轻度警示 |
| 浮亏 > 10% | 🔴 红色 | 重度预警，考虑止损 |
| 浮亏 > 15% | ⚠️ 黑色 | 强制止损线 |

### RSI 预警

| 条件 | 信号 | 操作 |
|------|------|------|
| RSI(14) > 70 | 超买 | 考虑减仓或止盈 |
| RSI(14) < 30 | 超卖 | 观察是否见底 |
| RSI(14) > 80 | 严重超买 | 警惕回调 |

### 持仓时长预警

| 条件 | 信号 |
|------|------|
| 持仓 > 30 自然日 | 长期持有预警（不自动平仓，仅提示）|
| 持仓 > 60 自然日 | 超长期，触发复盘 |

---

## 输出格式

### 持仓状态表

| 持仓 | 代码 | 成本 | 现价 | 浮盈亏 | 浮亏% | RSI | 持仓天数 |
|------|------|------|------|--------|-------|-----|---------|
| 长江电力 | 600900.SH | 23.50 | 24.10 | +0.60 | +2.55% | 58 | 82天⚠️ |

### 预警汇总

```
🚨 预警持仓: {N} 只
⚠️ {代码} {名称} 浮亏 -{pct}%（>{threshold}%），已持仓 {days} 天
📉 {代码} {名称} RSI={rsi}（超买/超卖）
```

---

## 测试指南

### 使用测试端点创建持仓

```bash
# 创建测试持仓
curl -X POST "http://localhost:5555/test/positions" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "600900.SH",
    "shares": 1000,
    "entry_price": 26.50,
    "entry_date": "2026-01-15"
  }'

# 创建多个测试持仓
curl -X POST "http://localhost:5555/test/positions" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "000001.SZ", "shares": 500, "entry_price": 15.20}'

# 验证持仓
curl -s "http://localhost:5555/positions"

# 执行持仓健康检查
curl -s "http://localhost:5555/portfolio/summary"

# 重置测试数据
curl -X POST "http://localhost:5555/test/reset"
```

---

## 已知数据缺口

1. **持仓 entry_date**: 若系统中未录入入场日期，持仓天数计算不准确（历史数据迁移已完成，需确认所有持仓均有 entry_date）
2. **RSI 实时计算**: 部分持仓可能因行情数据不足无法计算 RSI
3. **涨跌停状态**: 涨停时无法卖出，需在预警中标注

---

## 飞书输出模板

```
【持仓健康检查】{日期} {时间}
---
总持仓: {N} 只 | 总浮盈亏: {total_pnl}（{total_pnl_pct}%）
现金: {cash} | 总权益: {total_equity}

📊 持仓详情:
| 标的 | 成本 | 现价 | 浮盈亏 | RSI | 持仓天数 |
|------|------|------|--------|-----|---------|
| {name}({code}) | {cost} | {price} | {pnl} | {rsi} | {days} |

🚨 预警 ({N} 只):
{alerts}

📌 建议操作:
{recommendations}
```

---

## Common Pitfalls

1. **entry_date 缺失**: 持仓天数计算依赖 entry_date，系统数据迁移后需确认所有持仓均已入库
2. **涨跌停无法成交**: 预警触发后若标的涨停/跌停，实际无法执行止损/止盈，需在建议中注明"涨停/跌停中，无法操作"
3. **多账号持仓**: 当前 API 只能获取主账号持仓，子账号需单独查询
4. **港股持仓 RSI**: 港股日线数据质量可能不稳定，RSI 结果仅供参考
5. **RSI 数据不足**: 需要至少 15 天日线数据计算 RSI(14)，数据不足时标注"数据不足"

---

## Verification Checklist

- [ ] `/positions` 返回持仓列表（非空）
- [ ] `/portfolio/summary` 浮盈亏数据与持仓市值一致
- [ ] 各持仓 RSI 计算完成（数据不足时标注"数据不足"）
- [ ] 持仓天数计算正确（需 entry_date）
- [ ] 预警规则全部应用（浮亏%/RSI/持仓天数）
- [ ] 飞书推送触发条件正确（仅在有预警时推送，或每日定时推送）
- [ ] 测试端点可用（/test/positions, /test/reset）
