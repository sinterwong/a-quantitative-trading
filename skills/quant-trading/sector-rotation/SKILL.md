---
name: sector-rotation
description: Use when checking sector strength, momentum shifts, or industry allocation — returns ranked sector performance and capital flow. Inputs: date (optional, defaults to today). Triggers: "板块轮动", "行业配置", "哪个板块强", or any sector/macro allocation inquiry.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [quant-trading, sector-rotation, macro, industry-allocation, capital-flow]
    related_skills: [dynamic-stock-screener, stock-analyst, macro-north-capital]
---

# Sector Rotation（板块轮动）

## Overview

分析当前市场各行业板块的涨跌排名和资金流向，识别动量切换信号，辅助行业配置决策。通常作为动态选股的上游输入。

**输入**: 日期（可选，默认当天）
**输出**: 强势板块排名 + 资金净流入/流出 + 轮动信号
**推送**: 可生成飞书板块轮动卡片

---

## API 调用

```
1. POST /analysis/sector_rotation      → 板块轮动分析（ETF动量排名）
2. GET  /northbound/flow              → 北向资金（注意是 northbound 不是 north_capital）
```

**Base URL**: `http://localhost:5555`
**认证**: `X-API-Key` header（本地开发可省略）

### 实测返回格式

**POST /analysis/sector_rotation**
```json
{
  "scores": {"515000.SH": 0.279, "159915.SZ": 0.198, "515030.SH": 0.085},
  "buy": ["515000.SH", "159915.SZ", "515030.SH"],
  "hold": [], "sell": [],
  "top_n": 3, "universe_size": 14
}
```

**GET /northbound/flow**
```json
{"net_north_yi": 0.0, "direction": "neutral", "trend_yi": 0.0, "status": "ok"}
```

⚠️ `/data/market_flow` 和 `/market/north_capital` 均为 error。

---

## 快速调用示例

```bash
# 板块轮动（POST 需要 body）
curl -s -X POST "http://localhost:5555/analysis/sector_rotation" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-05-22"}'

# 北向资金
curl -s "http://localhost:5555/northbound/flow"
```

```python
import requests
from datetime import date

BASE = "http://localhost:5555"

def get_sector_rotation(trade_date: str = None) -> dict:
    payload = {"date": trade_date} if trade_date else {}
    return requests.post(f"{BASE}/analysis/sector_rotation", json=payload, timeout=10).json()

def get_north_flow() -> dict:
    return requests.get(f"{BASE}/northbound/flow", timeout=10).json()
```

---

## 输出解读

### 板块强弱信号

| 信号 | 含义 | 操作 |
|------|------|------|
| 资金净流入 + 涨幅居前 | 强势确认 | 持有或加仓 |
| 资金净流入 + 涨幅靠后 | 蓄力待涨 | 观察是否突破 |
| 资金净流出 + 跌幅居前 | 弱势确认 | 减仓或回避 |
| 资金净流出 + 涨幅居前 | 主力减仓 | 警惕见顶 |

### 轮动信号

- **资金从消费切向科技**: 风险偏好上升
- **资金从周期切向防御**: 市场情绪谨慎
- **北向集中买入某板块**: 外资看多信号

---

## 组合方式

```
sector-rotation          # 判断当日强势板块
    ↓
dynamic-stock-screener   # 在强势板块内选股
    ↓
stock-analyst            # 分析候选股
    ↓
stock-backtest           # 验证入场时机
```

---

## 飞书输出模板

```
【板块轮动】{日期}
---
强势板块（资金净流入 + 涨幅居前）:
1. {板块名}  +{涨幅}% | 净流入 {金额}亿
2. {板块名}  +{涨幅}% | 净流入 {金额}亿

弱势板块（资金净流出 + 跌幅居前）:
1. {板块名}  {跌幅}% | 净流出 {金额}亿

北向资金偏好: {净流入前三板块}

轮动信号: {描述}
配置建议: {描述}
```

---

## 已知数据缺口

1. **板块轮动日期参数**: 建议显式传入交易日，避免空参数导致异常
2. **板块粒度**: 返回板块层级（申万行业？主题板块？）需确认
3. **历史对比**: 当前 API 是否支持多日横向对比，需验证

---

## Common Pitfalls

1. **日内数据滞后**: 盘中资金流向数据 T+1 更新，建议在 16:00 后使用
2. **板块分类标准**: 不同数据源板块分类可能不同（申万/中信/主题），混用会导致数据打架
3. **轮动信号误读**: 单日资金流入不构成趋势，需连续 3-5 日观察

---

## Verification Checklist

- [ ] sector_rotation API 返回板块排名（非空）
- [ ] 强势/弱势板块标注清晰
- [ ] 资金流向与涨跌幅方向一致（异常情况需注明）
- [ ] 北向资金数据时间戳正确
- [ ] 飞书输出包含轮动信号描述
