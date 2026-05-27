---
name: dynamic-stock-screener
description: Use when screening stocks by technical/fundamental/money-flow conditions — returns ranked list of candidates. Inputs: filter criteria (RSI threshold, sector, market cap, fund flow direction). Triggers: "选股", "帮我筛选", "screen stocks", or any stock discovery request.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [quant-trading, stock-screening, sector-rotation, money-flow, momentum]
    related_skills: [stock-analyst, sector-rotation, stock-backtest, position-health-check]
---

# Dynamic Stock Screener（动态选股）

## Overview

根据技术面、基本面、资金面条件筛选股票，返回符合条件的标的排名列表。是整个分析链条的入口，通常作为 `stock-analyst` 的上游。

**输入**: 筛选条件组（可组合）
**输出**: 符合条件股票列表 + 评分排名
**推送**: 筛选结果可推送到飞书

---

## API 超时配置

| 接口 | 建议超时 | 说明 |
|------|----------|------|
| `/analysis/sector_rotation` | **60s** | 板块轮动分析 |
| `/northbound/flow` | 10s | 北向资金 |
| `/positions` | 10s | 现有持仓 |
| `/fundamentals/{symbol}` | 10s | 基本面数据 |
| `/data/daily/{symbol}` | 15s | 日线数据 |

---

## 筛选维度

### 技术面条件

| 条件 | 说明 | API 来源 |
|------|------|----------|
| RSI(14) < 30 | 超卖 | `/data/daily/{code}` → 本地计算 |
| RSI(14) > 70 | 超买 | 同上 |
| MA多头排列 | MA5 > MA20 > MA60 | 同上 |
| MACD金叉 | DIF 上穿 DEA | 同上 |
| 涨停/跌停 | 当日涨幅 ±10% | `/market/status` |

### 基本面条件

| 条件 | 说明 | API 来源 |
|------|------|----------|
| PE < 15 | 低估值 | `/fundamentals/{symbol}` |
| ROE > 10% | 优质盈利 | `/fundamentals/{symbol}` |
| 营收YoY > 0 | 营收正增长 | `/fundamentals/{symbol}` |
| 净利YoY > 0 | 净利润正增长 | `/fundamentals/{symbol}` |
| 股息率 > 2% | 高股息 | `/fundamentals/{symbol}` |

### 资金面条件

| 条件 | 说明 | API 来源 |
|------|------|----------|
| 北向净流入 | 当日沪股通+深股通净买入 | `/northbound/flow` |
| 主力净流入 | 大单资金净流入 | 需要额外数据源 |
| 板块净流入 | 行业资金净流入前三 | `/analysis/sector_rotation` |

---

## API 调用链

```
1. POST /analysis/sector_rotation         → 板块轮动强弱（ETF动量排名）
   - 请求: POST /analysis/sector_rotation  {"date": "2026-05-22"}
   - 返回: {scores: {ETF代码: 动量分数}, buy/hold/sell: [ETF列表], top_n: 3}
   - scores 值越大越强势（正值=动量向上）
2. GET  /northbound/flow                 → 北向资金（注意是 northbound 不是 north_capital）
   - 返回: {net_north_yi, direction, trend_yi, summary}
3. GET  /positions                       → 现有持仓（对比是否在强势板块内）
4. GET  /fundamentals/{symbol}           → 基本面数据（PE/PB/ROE/营收YoY/净利YoY）
```

**Base URL**: `http://localhost:5555`
**认证**: `X-API-Key` header（本地开发可省略）

### 实测返回格式

**POST /analysis/sector_rotation**
```json
{
  "scores": {
    "515000.SH": 0.279,
    "159915.SZ": 0.198,
    "515030.SH": 0.085
  },
  "buy": ["515000.SH", "159915.SZ", "515030.SH"],
  "hold": [],
  "sell": [],
  "top_n": 3,
  "universe_size": 14
}
```

**GET /northbound/flow**
```json
{
  "net_north_yi": 0.0,
  "direction": "neutral",
  "trend_yi": 0.0,
  "summary": "北向资金配额使用: 沪股通 100% / 南向 100%",
  "status": "ok"
}
```

**GET /fundamentals/{symbol}**
```json
{
  "pe": 18.47, "pb": 2.92, "dividend_yield": 0.15,
  "market_cap": 6665.14, "name": "长江电力", "price": 27.24,
  "symbol": "600900.SH", "status": "ok",
  "revenue_yoy": 6.44, "profit_yoy": 30.50, "roe_ttm": 3.01
}
```

⚠️ `/data/market_flow` 和 `/market/north_capital` 均为 error，使用前请先确认接口可用性。

---

## 快速调用示例

```bash
# 板块轮动（ETF动量）
curl -s -X POST "http://localhost:5555/analysis/sector_rotation" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-05-22"}'

# 北向资金（注意路径是 northbound/flow，不是 north_capital）
curl -s "http://localhost:5555/northbound/flow"

# 持仓列表（筛选前对比持仓是否已在强势板块）
curl -s "http://localhost:5555/positions"

# 基本面数据
curl -s "http://localhost:5555/fundamentals/600900.SH"
```

```python
import requests
from datetime import date

BASE = "http://localhost:5555"

def get_sector_rotation(trade_date: str = None) -> dict:
    payload = {"date": trade_date} if trade_date else {}
    return requests.post(f"{BASE}/analysis/sector_rotation", json=payload, timeout=60).json()

def get_north_flow() -> dict:
    # 注意路径是 northbound/flow
    return requests.get(f"{BASE}/northbound/flow", timeout=10).json()

def get_positions() -> dict:
    return requests.get(f"{BASE}/positions", timeout=10).json()

def get_fundamentals(symbol: str) -> dict:
    return requests.get(f"{BASE}/fundamentals/{symbol}", timeout=10).json()
```

---

## 组合链

```
dynamic-stock-screener     # 筛选候选股
    ↓
stock-analyst              # 逐一分析候选股
    ↓
stock-backtest             # 验证参数/择时
    ↓
POST /signals             # 发送信号
    ↓
trade-review              # 复盘
```

---

## 常见筛选策略

### 策略1：动量选股（动量/趋势跟踪）
```
条件: MA多头排列 + RSI 30-70 + 主力净流入前10
用途: 趋势跟踪，顺势而为
```

### 策略2：价值选股（低估价值）
```
条件: PE < 15 + ROE > 10% + 股息率 > 2% + 营收YoY > 0
用途: 价值投资，长持配置
```

### 策略3：北向跟随
```
条件: 北向当日净流入前5 + 属于沪股通/深股通
用途: 跟随聪明钱
```

### 策略4：超卖反弹
```
条件: RSI < 30 + 主力净流入 + 板块资金净流入
用途: 短线反弹博弈
```

---

## 已知数据缺口

1. **板块轮动 API**: `/analysis/sector_rotation` 需传入日期参数，无参数时需补充
2. **个股资金流向**: 数据来源稳定性需验证，部分小市值股票可能无数据
3. **北向资金**: 收盘后更新，日内数据可能滞后
4. **主力净流入**: 需要额外数据源，当前 API 可能不支持

---

## 飞书输出模板

```
【动态选股】{筛选策略名称}
---
候选股: {N} 只 | 筛选条件: {条件列表}

🥇 {股票名称}({代码})  - {score}
🥈 {股票名称}({代码})  - {score}
🥉 {股票名称}({代码})  - {score}

{详见分析报告链接}
```

---

## Common Pitfalls

1. **筛选条件过严**: 条件全用 AND 连接时，返回结果可能为空 → 适当放宽条件或使用 OR 组合
2. **板块轮动日期**: `/analysis/sector_rotation` 传空 date 时行为不确定，建议显式传入交易日
3. **资金流向延迟**: 北向资金 T+1 更新，盘中用前日数据需注明
4. **评分权重**: 多条件筛选时需预设权重逻辑，避免随机排序
5. **API 超时设置**: `/analysis/sector_rotation` 需要 60s 超时，否则可能返回空响应

---

## Verification Checklist

- [ ] sector_rotation API 调用成功（返回板块排名）
- [ ] market_flow 数据获取成功（至少返回部分股票）
- [ ] 筛选结果按评分/相关性排序
- [ ] 输出包含各条件命中文情（哪只股票命中了哪些条件）
- [ ] 结果数量合理（过多=条件太宽，过少=条件太严）
- [ ] 北向/主力资金数据注明时间戳（是否为当日数据）
- [ ] API 超时设置正确（/analysis/sector_rotation 使用 60s）
- [ ] 基本面字段完整（PE/PB/ROE/营收YoY/净利YoY）
