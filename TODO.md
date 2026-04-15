# TODO — 开发任务

> 最后更新：2026-04-15
> 当前基线：回测框架完成（backtest_cli.py），RSI WFA 验证通过（沪深300 Sharpe=0.467）

---

## P0 · 策略验证

### ✅ done — 回测引擎 CLI
- `scripts/quant/backtest_cli.py` — single / grid / compare / wf / fcompare 命令

### ✅ done — RSI 参数验证（沪深300）
- 最优：RSI(25/65)，SL=5%，TP=20%，ATR_threshold=0.90
- WFA 10 窗口 avg_sharpe=0.467，正收益窗口 70%
- ATR 阈值选 0.90（回测最优）

### ✅ done — ATR ratio 计算
- `backend/services/signals.py` — `_compute_atr_ratio(symbol, period=14, lookback=20)`
- ATR ratio = 当前ATR / 近20日ATR最高值

### ✅ done — RSI BUY 高波动屏蔽
- `evaluate_signal()` — ATR ratio > atr_threshold 时 RSI BUY 变为 HOLD
- `DEFAULT_ATR_THRESH = 0.90`

### ✅ done — intraday_monitor ATR 集成
- `backend/services/intraday_monitor.py` — 两处 evaluate_signal 调用均传入 atr_threshold
- 参数来源：`params.json` → `live_params.json`

---

## P1 · 信号质量

### ✅ done — 基本面过滤
- **新建** `backend/services/fundamentals.py`
- `fetch_fundamentals(symbol)` → PE/PB/股息率（市价 HTTP）
- `check_fundamentals_filter(symbol)` → PE>80 / PB>15 拒绝；ETF（PE=PB=0）跳过
- 集成进 `evaluate_signal()` RSI BUY 前置检查

### ✅ done — confirm_signal_minute 集成
- `intraday_monitor.py` — WATCH_BUY/RSI_BUY 信号均调用分钟RSI二次确认

---

## P1 · 风控

### ✅ done — 组合熔断
- `backend/services/intraday_monitor.py` — `_check_portfolio_risk()`
- DD > 8% → 警告 + 仓位减半；DD > 12% → 全量清仓
- 权益新高自动重置熔断状态

### ✅ done — Kelly 仓位管理器
- **新建** `scripts/quant/position_sizer.py`
- `compute_kelly(win_rate, avg_win, avg_loss)` → 半 Kelly（f* × 0.5）
- 边界：最小 5%，最大 30%
- `_calc_shares()` 使用 `_kelly_pct`；每日根据历史交易自动更新
- `compute_kelly_from_trades(trades)` → 从交易记录估算 Kelly

---

## P2 · 信号多元化（待实现）

### 🔲 — MACD 策略验证
- `scripts/quant/backtest_cli.py` 新增 `macd-compare` 命令
- 参数：fast(8,10,12) / slow(20,26,30) / signal(7,9,12)
- 对比：纯 RSI vs RSI+MACD 共振

### 🔲 — 布林带策略验证
- 同 MACD，参数：period(20) / std_mult(1.5, 2.0, 2.5)

### 🔲 — 北向共振信号
- `backend/services/northbound.py` — `check_northbound_crossover(symbol)`
- RSI_BUY + 北向单日净流入 > 50亿 → 信号强度 ×1.5

### 🔲 — 新闻情绪打分
- **新建** `scripts/quant/news_scorer.py`
- 关键词：利好/超预期 → 正；风险/减持 → 负
- 集成进 `dynamic_selector.py`

---

## P2 · 实盘对接

### 🔲 — 涨跌停熔断
- `signals.py` — 涨停日已有持仓 → WATCH_SELL；无持仓 → 禁止追
- 跌停日已有持仓 → WATCH_SELL（逃生）；无持仓 → 禁止抄
- `broker.py` — 订单提交时拦截涨跌停标的

### 🔲 — 行业集中度
- **新建** `scripts/quant/sector_map.json` — `{代码前缀: 行业名}`
- `portfolio.py` — `check_sector_concentration(positions)`
- 单一行业 > 40% → 强制减仓至 40%

### 🔲 — 滑点监控
- `broker.py` — 订单成交后对比「信号触发价 vs 实际成交价」
- `daily_journal.py` — 记录滑点

### 🔲 — 压力测试
- `scripts/quant/backtest_cli.py` 新增 `crash-test` 命令
- 测试区间：2015-06 ~ 2015-09（股灾）、2018 全年（贸易战）、2022-03 ~ 2022-06（上海封控）

---

## P3 · 架构

### 🔲 — PostgreSQL 迁移
- 设计 Schema → `scripts/migrate_sqlite_to_pg.py`
- `portfolio.py` — SQLAlchemy + PostgreSQL

### 🔲 — Backend 崩溃自动拉起
- `GET /health` → 非 200 则自动重启（Windows Service / supervisord）

### 🔲 — Redis 缓存层
- 持仓/信号状态缓存，TTL：信号 5 分钟，持仓 1 分钟

---

## 已完成总览

| 优先级 | 任务 | 文件 |
|---------|------|------|
| P0 | ATR ratio 计算 | `services/signals.py` |
| P0 | RSI BUY 高波动屏蔽 | `services/signals.py` |
| P0 | intraday_monitor ATR 集成 | `intraday_monitor.py` |
| P0 | RSI 参数 WFA 验证 | `backtest_cli.py` |
| P1 | 基本面过滤 PE/PB | `services/fundamentals.py` |
| P1 | 组合熔断 8%/12% | `intraday_monitor.py` |
| P1 | Kelly 仓位 | `scripts/quant/position_sizer.py` |
| P1 | 分钟RSI 二次确认 | `intraday_monitor.py` |
