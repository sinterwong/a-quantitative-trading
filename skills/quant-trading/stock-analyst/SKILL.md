---
name: stock-analyst
description: Use when analyzing a single A-share or HK stock — returns technical summary, fundamental metrics, and LLM rating. Inputs: symbol code (e.g. 600900.SH or 1810.HK). Triggers: user asks "analyze XXX", "帮我分析 XXX", or any single-stock inquiry.
version: 1.0.0
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

## API 调用链

```
1. GET  /data/realtime/{symbol}        → 当前价格、涨跌幅、成交量
2. GET  /data/daily/{code}?days=60     → 近60日K线（计算RSI/MACD）
3. GET  /fundamentals/basic?symbol=XXX → PE/PB/ROE/营收增速/净利润增速
4. POST /analysis/stock_research       → LLM 综合研报（可选，视数据质量）
```

**Base URL**: `http://localhost:5555`
**认证**: `X-API-Key` header（本地开发可省略）

---

## 快速调用示例

### 通过 curl

```bash
# 实时行情
curl -s "http://localhost:5555/data/realtime/600900.SH"

# 日线历史（取60天）
curl -s "http://localhost:5555/data/daily/600900.SH?days=60"

# 基本面
curl -s "http://localhost:5555/fundamentals/basic?symbol=600900.SH"
```

### 通过 Python requests

```python
import requests

BASE = "http://localhost:5555"
HEADERS = {"X-API-Key": "your-api-key"}  # 本地可省略

def get_realtime(symbol: str) -> dict:
    return requests.get(f"{BASE}/data/realtime/{symbol}", headers=HEADERS).json()

def get_daily(symbol: str, days: int = 60) -> dict:
    return requests.get(f"{BASE}/data/daily/{symbol}", params={"days": days}, headers=HEADERS).json()

def get_fundamentals(symbol: str) -> dict:
    return requests.get(f"{BASE}/fundamentals/basic", params={"symbol": symbol}, headers=HEADERS).json()
```

---

## 输出格式

### 技术面摘要（计算得到）

从日线数据计算：
- **RSI(14)**: 超过70=超买，低于30=超卖
- **MACD**: DIF/DEA 交叉信号
- **MA5/MA20/MA60**: 均线多头/空头排列
- **最近支撑/压力位**: 近期高低点

### 基本面字段（来自 API）

| 字段 | 说明 | 正常范围 |
|------|------|----------|
| pe_ttm | 市盈率（TTM） | A股平均10-25 |
| pb | 市净率 | 1-5 |
| roe | 净资产收益率 | >10% 为优 |
| revenue_yoy | 营收同比增速 | >0% |
| net_profit_yoy | 净利润同比增速 | >0% |
| dividend_yield | 股息率 | >2% |
| industry | 所属行业 | — |

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

## 已知数据缺口

1. **港股基本面**: AkShare 港股基本面数据零覆盖（PE/PB/ROE 等字段可能为 null），港股分析结果仅供参考
2. **PE/PB 为 null**: 视为数据缺口，不代表基本面健康，需在输出中注明
3. **营收/净利 YoY 连续为负**: 实质风险，必须在摘要中突出警示

---

## 飞书输出模板

```
【个股分析】{股票名称}（{代码}）
---
当前价: {price} | 涨跌: {change_pct}%
RSI(14): {rsi} | MACD信号: {macd_signal}
MA排列: {ma排列}

基本面:
PE: {pe} | PB: {pb} | ROE: {roe}%
营收YoY: {revenue_yoy}% | 净利YoY: {net_profit_yoy}%

【综合评级】: {A/B/C/D}
{foundation_remark}
```

---

## Common Pitfalls

1. **港股代码格式**: HK股票用 `1810.HK`（四点数字.HK），不要用 `01810.HK`
2. **RSI 计算**: 需要至少14天数据，数据不足时 RSI 置 null 并注明
3. **PE/PB 为 null**: 不要默认当作"正常"，需在摘要中标注"数据缺口"
4. **连续亏损**: 营收YoY和净利YoY同时为负 → 必须输出 C/D 评级
5. **实时行情超时**: 腾讯/新浪行情API偶发超时，单次失败重试一次，第二次失败标注"行情获取失败"

---

## Verification Checklist

- [ ] 实时行情获取成功（price/change_pct 非空）
- [ ] RSI(14) 计算完成（需14天以上数据）
- [ ] 基本面字段完整展示（PE/PB/ROE/YoY）
- [ ] PE或PB为null时标注数据缺口，不默认当作正常
- [ ] 营收/净利YoY连续为负 → 突出警示
- [ ] 输出包含 A/B/C/D 综合评级
- [ ] 飞书推送格式正确（chat_id: oc_8d67f97916478affc49b578c028152c2）
