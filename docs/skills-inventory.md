# Skills 盘点：工具化 · 可组合

**定位**: 原子化工具 + 飞书推送 + 可串成闭环
**分支**: `docs/skills-inventory`

---

## 核心理念

- **分**: 独立工具，拿起来就用（一个 Skill 做一件事）
- **合**: 工具之间可组合 + 飞书推送 = 完整业务闭环
- **推送**: 所有 Skills 输出优先推送到飞书（second-bot, chat_id: oc_8d67f97916478affc49b578c028152c2）

---

## 原子化 Tools（独立 Skills）

每个 Skill 输入输出清晰，可独立调用，也可串成工作流。

---

### 1. 个股分析 `stock-analyst`

**输入**: 股票代码（如 `600900.SH`）
**输出**: 技术面摘要 + 基本面指标 + LLM 综合评级（A/B/C/D）

**API**:
```
GET  /data/daily?symbol=600900.SH&days=60
GET  /data/real_time?symbol=600900.SH
GET  /fundamentals/basic?symbol=600900.SH
POST /analysis/stock_research
```

**飞书输出格式**: 股票名称 + 当前价 + 涨跌 + 关键指标（PE/PB/ROE/营收增速）+ 综合评级卡片

---

### 2. 个股回测 `stock-backtest`

**输入**: 股票代码 + 参数组合（RSI 阈值/止损/持仓周期）
**输出**: 回测曲线 + 夏普比率 + 最大回撤 + 胜率

**API**:
```
POST /backtest/run
GET  /research/factor_ic?factor=RSI
```

**飞书输出格式**: 回测收益曲线（ASCII 图）+ 关键指标摘要

---

### 3. 动态选股 `dynamic-stock-screener`

**输入**: 筛选条件组（技术面/基本面/资金面）
**输出**: 符合条件的股票列表 + 评分排名

**API**:
```
GET  /data/market_flow?direction=stock  # 资金流向筛选
GET  /analysis/sector_rotation           # 板块轮动选强
POST /watchlist                           # 录入自选股
```

**组合链**: `dynamic-stock-screener` → `stock-analyst` → `stock-backtest` → 决定发信号

---

### 4. 板块轮动 `sector-rotation`

**输入**:（空，或指定时间范围）
**输出**: 当日强势板块排名 + 资金流入流出

**API**:
```
GET /analysis/sector_rotation
GET /data/market_flow?direction=sector
```

**用途**: 辅助行业配置决策，识别动量切换

---

### 5. 因子 IC 监控 `factor-ic-monitor`

**输入**:（空，实时查）
**输出**: 各因子 IC 值序列 + 趋势（趋势向上/向下/衰减告警）

**API**:
```
GET /research/factor_ic
GET /research/factor_effectiveness
```

**告警**: IC 衰减超过阈值时推送飞书告警

---

### 6. 持仓健康检查 `position-health-check`

**输入**:（空，查当前持仓）
**输出**: 各持仓浮亏/浮盈状态 + RSI 超买超卖 + 预警信号

**API**:
```
GET /portfolio/summary
GET /data/real_time?symbol=600900.SH,...
```

**告警条件**: 单只浮亏超 X%、RSI 超过阈值、持仓太久未动

---

### 7. 宏观北向资金 `macro-north-capital`

**输入**:（空）
**输出**: 当日北向资金净流入/流出 + 沪深港通持股变化

**API**:
```
GET /market/north_capital
GET /data/market_flow?direction=north
```

---

### 8. 交易信号复盘 `trade-review`

**输入**: 时间范围（默认当日）
**输出**: 今日信号列表 + 成交记录 + LLM 复盘建议

**API**:
```
GET /signals?date=today
GET /trades?date=today
GET /orders?date=today
```

**用途**: 每日收盘后复盘，发现信号漂移或执行问题

---

### 9. 参数优化 `wfa-walkforward`

**输入**: 股票代码 + 因子参数范围
**输出**: WFA 最优参数组合 + 预测期表现

**API**:
```
POST /research/wfa
GET  /research/param_optimize
PATCH /params/{symbol}
```

---

### 10. 新闻舆情 `news-sentiment`

**输入**: 股票代码（可选）
**输出**: 最新财经新闻 + LLM 情感评分

**API**:
```
GET /data/news
GET /data/news?symbol=600900.SH
```

---

## 组合闭环示例

### 闭环 A：每日收盘分析 → 推送 → 信号录入
```
dynamic-stock-screener     # 筛出强势股
  → stock-analyst          # 分析候选股
    → stock-backtest       # 验证参数
      → 发送信号 (POST /signals)
        → 订单执行 (POST /orders)
          → trade-review  # 复盘
            → 飞书推送摘要
```

### 闭环 B：持仓管理
```
position-health-check      # 检查所有持仓
  → 若预警 → 飞书告警
  → 若 RSI 超买 → 触发 exit 逻辑
  → 每日生成持仓报告 → 飞书推送
```

---

## 优先级排序（建议开发顺序）

| 优先级 | Skill | 理由 |
|--------|-------|------|
| 🔴 高 | `stock-analyst` | 最常用，API 最成熟，直接产出价值 |
| 🔴 高 | `position-health-check` | 解决持仓跟踪痛点 |
| 🔴 高 | `trade-review` | 每日复盘刚需 |
| 🟡 中 | `dynamic-stock-screener` | 需要 `sector_rotation` 配合 |
| 🟡 中 | `factor-ic-monitor` | 量化系统核心，但当前 IC 数据不稳定 |
| 🟡 中 | `sector-rotation` | 辅助配置决策 |
| 🟢 低 | `stock-backtest` | 回测框架已有，API 需确认 |
| 🟢 低 | `wfa-walkforward` | 依赖参数系统成熟度 |
| 🟢 低 | `news-sentiment` | 数据源质量待确认 |
| 🟢 低 | `macro-north-capital` | 已有 `north_capital` 接口 |

---

## 待确认问题

1. `stock-backtest` 的 `/backtest/run` 接口是否完整可用？需实际调用验证
2. `factor-ic-monitor` 的 IC 数据当前质量如何？是否有断点？
3. `dynamic-stock-screener` 的筛选条件组——技术面/基本面/资金面各支持哪些具体指标？
4. `news-sentiment` 数据源是哪家？质量是否可用？

---

## 飞书推送格式

统一使用飞书卡片消息，推送至 `second-bot`（chat_id: oc_8d67f97916478affc49b578c028152c2）

卡片结构：
- 标题：`[Skill名] 推送时间`
- 内容：数据摘要（表格或列表）
- 操作按钮：查看详情 → 跳转后端 Dashboard 链接
