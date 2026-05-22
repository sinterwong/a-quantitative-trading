---
name: trade-review
description: Use when reviewing daily/weekly trading activity — returns signal list, trade execution, and LLM recap. Inputs: date range (defaults to today). Triggers: "交易复盘", "今日成交", "复盘", "trade review", or any execution recap request.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [quant-trading, trade-review, execution, signal-recap, daily-recap]
    related_skills: [stock-analyst, position-health-check, dynamic-stock-screener]
---

# Trade Review（交易复盘）

## Overview

每日/每周收盘后复盘信号产生、订单执行、成交记录，输出结构化复盘报告，发现信号漂移或执行问题。是交易闭环的最后一步。

**输入**: 日期范围（默认当天）
**输出**: 信号列表 + 成交记录 + LLM 复盘建议
**推送**: 每日收盘后推送复盘报告到飞书

---

## API 调用链

```
1. GET /signals?date={today}             → 当日产生的信号
2. GET /orders/recent?limit=20            → 最近N笔订单（含状态）
3. GET /trades?date={today}               → 当日成交记录
4. GET /positions                         → 收盘持仓快照
5. GET /portfolio/summary                 → 收盘组合净值
```

**Base URL**: `http://localhost:5555`
**认证**: `X-API-Key` header（本地开发可省略）

---

## 快速调用示例

```bash
# 当日信号
curl -s "http://localhost:5555/signals?date=2026-05-22"

# 最近订单
curl -s "http://localhost:5555/orders/recent?limit=20"

# 当日成交
curl -s "http://localhost:5555/trades?date=2026-05-22"

# 收盘持仓
curl -s "http://localhost:5555/positions"

# 组合摘要
curl -s "http://localhost:5555/portfolio/summary"
```

```python
import requests
from datetime import date, timedelta

BASE = "http://localhost:5555"
TODAY = date.today().isoformat()

def get_signals(trade_date: str = TODAY) -> dict:
    return requests.get(f"{BASE}/signals", params={"date": trade_date}).json()

def get_recent_orders(limit: int = 20) -> dict:
    return requests.get(f"{BASE}/orders/recent", params={"limit": limit}).json()

def get_trades(trade_date: str = TODAY) -> dict:
    return requests.get(f"{BASE}/trades", params={"date": trade_date}).json()

def get_positions() -> dict:
    return requests.get(f"{BASE}/positions").json()

def get_portfolio_summary() -> dict:
    return requests.get(f"{BASE}/portfolio/summary").json()
```

---

## 复盘维度

### 1. 信号统计

| 指标 | 说明 |
|------|------|
| 信号总数 | 当日产生信号数量 |
| 买入信号 | Buy 信号数量 |
| 卖出信号 | Sell/Exit 信号数量 |
| 信号通过率 | LLM 审核通过率 |
| 被拒绝信号 | LLM 审核拒绝数及原因 |

### 2. 订单执行

| 指标 | 说明 |
|------|------|
| 订单总数 | 提交订单数 |
| 成交率 | 成交/提交比例 |
| 拒绝率 | 被 broker/风控拒绝比例 |
| 平均执行滑点 | 期望价 vs 成交价差异 |
| 未成交订单 | pending 状态订单 |

### 3. 收益归因

| 指标 | 说明 |
|------|------|
| 当日盈亏 | 当日浮盈亏变化 |
| 持仓盈亏 | 各持仓对总盈亏贡献 |
| 胜率 | 当日盈利交易/总交易 |
| 单笔盈亏 | 最大盈利/最大亏损 |

### 4. 问题发现

- 信号产生但未成交 → 排查 broker 拒绝或风控拦截
- LLM 审核拒绝率异常高 → 检查信号质量或审核参数
- 滑点过大 → 检查流动性或调整下单方式
- 持仓未按信号退出 → 检查 exit_engine 触发条件

---

## 飞书输出模板

```
【每日复盘】{日期}
---

📡 信号统计:
信号总数: {N} | 买入: {buy} | 卖出: {sell}
通过率: {pass_rate}% | 拒绝: {rejected}（{reasons}）

📋 订单执行:
提交: {submitted} | 成交: {filled} | 拒绝: {rejected}
未成交: {pending}（{reason}）

💰 收益:
当日盈亏: {daily_pnl}（{daily_pnl_pct}%）
持仓总盈亏: {total_pnl}（{total_pnl_pct}%）
胜率: {win_rate}%

📌 LLM 复盘建议:
{llm_advice}
```

---

## 组合方式

```
信号产生 → 订单执行 → trade-review → 飞书推送
                    ↓
            发现问题 → stock-analyst（深度分析问题标的）
                    ↓
            参数调优 → wfa-walkforward
```

---

## 已知数据缺口

1. **信号时间戳**: 信号产生时间是否精确到秒，影响与行情对应关系判断
2. **订单拒绝原因**: broker 返回的拒绝原因粒度是否足够细
3. **历史复盘**: 是否支持多日横向对比（目前默认当天）
4. **港股交易时间**: 港股收盘后复盘时间与A股不同步

---

## Common Pitfalls

1. **日期格式**: API 使用 `YYYY-MM-DD`，传入格式错误会返回空数据
2. **信号 vs 成交不对应**: 信号产生后不一定立即成交，注意时间对齐
3. **LLM 审核结果**: 需要在信号记录中关联审核结果（approve/reject + 原因）
4. **未成交订单**: pending 状态订单不代表最终状态，需跟踪后续是否成交或过期

---

## Verification Checklist

- [ ] `/signals` 返回当日信号（非空检查）
- [ ] `/orders/recent` 订单状态完整（filled/rejected/pending）
- [ ] `/trades` 成交记录与订单对应
- [ ] 收益数据与持仓快照一致
- [ ] LLM 审核拒绝原因已记录
- [ ] 飞书推送包含当日核心指标（信号数/成交率/盈亏）
- [ ] 复盘建议有具体操作性（不是空话）
