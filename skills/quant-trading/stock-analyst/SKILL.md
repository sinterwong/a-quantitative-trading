---
name: stock-analyst
description: Use when analyzing a single A-share or HK stock — returns technical summary, fundamental metrics, and LLM rating. Inputs: symbol code (e.g. 600900.SH or 1810.HK). Triggers: user asks "analyze XXX", "帮我分析 XXX", or any single-stock inquiry.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [quant-trading, stock-analysis, a-share, hk-stock, single-stock]
    related_skills: [dynamic-stock-screener, stock-backtest, sector-rotation, position-health-check]
---

# Stock Analyst（个股分析）

## Overview

对单只 A 股或港股进行全面技术面 + 基本面分析，输出结构化摘要和 LLM 综合评级（A/B/C/D）。是整个工具链的最小分析单元。

**输入**: 股票代码（如 `600900.SH`、`1810.HK`）
**输出**: 技术指标、基本面数据、评级卡片
**推送**: 分析完成后可选择推送到飞书

---

## API 超时配置

| 接口 | 建议超时 | 说明 |
|------|----------|------|
| `/data/realtime/{symbol}` | 10s | 实时行情 |
| `/data/daily/{symbol}` | 15s | 日线数据 |
| `/fundamentals/{symbol}` | 10s | 基本面快照 |
| `/analysis/stock/a` | **120s** | LLM 分析，耗时长 |
| `/analysis/sector_rotation` | **60s** | 板块轮动分析 |

---

## API 调用链

```
1. GET  /data/realtime/{symbol}        → 当前价格、涨跌幅
2. GET  /data/daily/{symbol}?days=60  → 近60日K线（计算RSI/MACD）
3. GET  /fundamentals/{symbol}        → PE/PB/ROE/股息率/市值/营收YoY/净利YoY
4. POST /analysis/stock/a             → A股综合分析（可选，耗时长）
```

**Base URL**: `http://localhost:5555`
**认证**: `X-API-Key` header（本地开发可省略）

### 各 API 实测返回格式

**GET /data/realtime/{symbol}**
```json
{
  "quote": {"price": 26.56, "pct": -0.38, "pe": "18.01", "high": 26.76, "low": 26.5, ...},
  "symbol": "600900.SH",
  "status": "ok"
}
```

**GET /data/daily/{symbol}?days=60**
```json
{
  "columns": ["date","open","high","low","close","volume","amount","pct_chg","ma5","ma10","ma20","volume_ratio"],
  "data": [
    {"date": "2026-05-22", "close": 26.56, "ma5": 26.85, "ma10": 26.93, "ma20": 26.94, ...},
    ...
  ],
  "status": "ok"
}
```

**GET /fundamentals/{symbol}**
```json
{
  "pe": 18.47, "pb": 2.92, "dividend_yield": 0.15,
  "market_cap": 6665.14, "name": "长江电力", "price": 27.24,
  "symbol": "600900.SH", "status": "ok",
  "revenue_yoy": 6.44, "profit_yoy": 30.50, "roe_ttm": 3.01,
  "eps_ttm": 0.2763, "ocf_to_profit": 1.73,
  "industry": "", "sector": ""
}
```

**POST /analysis/stock/a**（耗时 30-120s，建议 timeout=120）
```bash
curl -s -X POST "http://localhost:5555/analysis/stock/a" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH"}'
```

---

## 快速调用示例

```bash
# 实时行情
curl -s "http://localhost:5555/data/realtime/600900.SH"

# 日线历史（取60天）
curl -s "http://localhost:5555/data/daily/600900.SH?days=60"

# 基本面（路径是 /fundamentals/{symbol}，不是 /fundamentals/basic）
curl -s "http://localhost:5555/fundamentals/600900.SH"

# A股综合分析（POST，耗时较长，建议 timeout=120）
curl -s -X POST "http://localhost:5555/analysis/stock/a" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH"}'
```

```python
import requests

BASE = "http://localhost:5555"
HEADERS = {"X-API-Key": "your-api-key"}  # 本地可省略

def get_realtime(symbol: str) -> dict:
    return requests.get(f"{BASE}/data/realtime/{symbol}", headers=HEADERS, timeout=10).json()

def get_daily(symbol: str, days: int = 60) -> dict:
    return requests.get(f"{BASE}/data/daily/{symbol}", params={"days": days}, headers=HEADERS, timeout=15).json()

def get_fundamentals(symbol: str) -> dict:
    # 注意: 路径是 /fundamentals/{symbol}，不是 /fundamentals/basic
    return requests.get(f"{BASE}/fundamentals/{symbol}", headers=HEADERS, timeout=10).json()

def get_stock_analysis_a(symbol: str) -> dict:
    # 耗时较长，建议 timeout=120
    return requests.post(f"{BASE}/analysis/stock/a", json={"symbol": symbol}, headers=HEADERS, timeout=120).json()

# RSI 计算示例
def calc_rsi(closes: list, period: int = 14) -> float | None:
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

## 输出格式

### 技术面摘要（计算得到）

从日线数据计算：
- **RSI(14)**: 超过70=超买，低于30=超卖
- **MACD**: DIF/DEA 交叉信号（需自己计算或调用 `/analysis/stock/a` 获取）
- **MA5/MA10/MA20**: 均线多头/空头排列（API 直接返回 ma5/ma10/ma20）
- **最近支撑/压力位**: 近期高低点

### 基本面字段（来自 API）

| 字段 | 说明 | 正常范围 | 备注 |
|------|------|----------|------|
| pe | 市盈率 | A股平均10-25 | |
| pb | 市净率 | 1-5 | |
| dividend_yield | 股息率 | >2% | ⚠️ 返回原始小数（0.016 = 1.6%） |
| market_cap | 总市值（亿） | — | |
| name | 股票名称 | — | |
| price | 当前价 | — | |
| revenue_yoy | 营收同比增速 (%) | >0 | 正值表示增长 |
| profit_yoy | 净利润同比增速 (%) | >0 | 正值表示增长 |
| roe_ttm | ROE (TTM) (%) | >10% | 衡量盈利能力 |
| eps_ttm | EPS (TTM)（元/股） | >0 | 每股收益 |
| ocf_to_profit | 经营现金流/净利润 | >1 | 现金流质量 |
| industry | 所属行业 | — | 可能为空 |
| sector | 所属板块 | — | 可能为空 |

### LLM 综合评级

| 评级 | 含义 |
|------|------|
| A | 技术面+基本面双优，建议关注 |
| B | 整体健康，有小幅风险点 |
| C | 存在明显风险，建议谨慎 |
| D | 高风险，不建议持仓 |

---

## 组合方式

```
dynamic-stock-screener  →  stock-analyst  →  stock-backtest  →  发送信号
                                                          ↓
                                                    trade-review
```

---

## 测试指南

### 使用测试端点

```bash
# 创建测试持仓
curl -X POST "http://localhost:5555/test/positions" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH", "shares":1000, "entry_price":26.50}'

# 创建测试信号
curl -X POST "http://localhost:5555/test/signals" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH", "signal":"BUY", "strength":0.8}'

# 创建测试成交
curl -X POST "http://localhost:5555/test/trades" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH", "side":"BUY", "shares":1000, "price":27.25}'

# 重置所有测试数据
curl -X POST "http://localhost:5555/test/reset"
```

---

## 已知数据缺口

1. **港股基本面**: AkShare 港股基本面数据零覆盖，PE/PB/ROE 等字段可能为 null
2. **YoY 数据缺失**: `GET /fundamentals/{symbol}` 现已支持 `revenue_yoy`/`net_profit_yoy` 字段
3. **PE/PB 为 null**: 视为数据缺口，不代表基本面健康，需在输出中注明
4. **`/analysis/stock/a` 耗时**: 该接口调用 LLM，耗时 30-120s，需设置足够 timeout

---

## 飞书输出模板

```
【个股分析】{股票名称}（{代码}）
---
当前价: {price} | 涨跌: {pct}%
RSI(14): {rsi} | MA排列: {ma排列}

基本面:
PE: {pe} | PB: {pb} | 股息率: {dividend_yield}%
营收YoY: {revenue_yoy}% | 净利YoY: {profit_yoy}%
ROE: {roe_ttm}% | EPS: {eps_ttm}

【综合评级】: {A/B/C/D}
{备注}
```

---

## Common Pitfalls

1. **港股代码格式**: HK股票用 `1810.HK`（四点数字.HK），不要用 `01810.HK`
2. **RSI 计算**: 需要至少14天数据，数据不足时 RSI 置 null 并注明
3. **PE/PB 为 null**: 不要默认当作"正常"，需在摘要中标注"数据缺口"
4. **实时行情超时**: 腾讯/新浪行情API偶发超时，单次失败重试一次，第二次失败标注"行情获取失败"
5. **dividend_yield 格式**: API 返回原始小数（0.016），需转换为百分比（1.6%）再展示
6. **API 超时设置**: `/analysis/stock/a` 需要 120s 超时，否则可能返回空响应

---

## Verification Checklist

- [ ] 实时行情获取成功（price/pct 非空）
- [ ] RSI(14) 计算完成（需14天以上数据）
- [ ] 基本面字段完整展示（PE/PB/dividend_yield/revenue_yoy/profit_yoy/roe_ttm）
- [ ] PE或PB为null时标注数据缺口，不默认当作正常
- [ ] dividend_yield 从原始小数转换为百分比格式
- [ ] 输出包含 A/B/C/D 综合评级
- [ ] 飞书推送格式正确（chat_id: oc_8d67f97916478affc49b578c028152c2）
- [ ] API 超时设置正确（/analysis/stock/a 使用 120s）
- [ ] 测试端点可用（/test/positions, /test/signals, /test/trades, /test/reset）
