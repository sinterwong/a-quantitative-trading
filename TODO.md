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

### ✅ done — MACD 策略验证
- `scripts/quant/backtest_cli.py` 新增 `macd-compare` 命令
- 参数：fast(12) / slow(26) / signal(9)
- `MACDSignalFunc` — MACD 零轴金叉/死叉
- `RSIPlusMACDSignalFunc` — RSI 金叉 + MACD histogram>0 确认
- 对比纯 RSI vs RSI+MACD 共振

### ✅ done — 新闻情绪打分
- **新建** `scripts/quant/news_scorer.py` — `NewsSentimentScorer`
  - 实时获取东方财富快讯（newsapi.eastmoney.com）
  - 关键词打分：利好/利空/中性（-100~+100）
  - 板块情绪识别（银行/电力/电子/医药/新能源等14个板块）
  - 集成进 `dynamic_selector.py` — `calc_all_scores()` 增加情绪分数加成

### 🔲 — 布林带策略验证
- 同 MACD，参数：period(20) / std_mult(1.5, 2.0, 2.5)

### ✅ done — 北向共振信号（部分）
- `backend/services/northbound.py` — `check_northbound_crossover(symbol)` 已存在
- RSI_BUY + 北向单日净流入 > 50亿 → 信号强度 ×1.5（待集成进 signals.py）

### 🔲 — 新闻情绪打分集成进早报
- `morning_runner.py` — 盘中加入 news_sentiment 模块
- `scripts/morning_report.py` — 报告中显示市场综合情绪

---

## P2 · 实盘对接

### ✅ done — 涨跌停熔断（position-aware）
- `signals.py` — `evaluate_signal()` 新增 `positions` 参数
  - LIMIT_UP + 有持仓 → WATCH_SELL（止盈预警）
  - LIMIT_UP + 无持仓 → LIMIT_UP（禁止追涨）
  - LIMIT_DOWN + 有持仓 → RSI_SELL（紧急逃生）
  - LIMIT_DOWN + 无持仓 → LIMIT_DOWN（禁止抄底）
- `intraday_monitor.py` — 两处 evaluate_signal 均传入 positions

### ✅ done — 行业集中度
- `backend/services/sector_map.json` — 88 个 A 股代码映射
- `portfolio.py` — `check_sector_concentration(positions, max_sector_pct=0.40)`
- `intraday_monitor.py` — `_check_sector_concentration()` 自动减仓
- 单一行业 > 40% → 飞书警告 + 自动减半

### ✅ done — 滑点监控
- `broker.py` — `OrderResult` 新增 `signal_price` + `slippage_bps` 字段
- `_simulate_fill()` 计算滑点：`slippage_bps = (fill_price - signal_price) / signal_price × 10000`
- `portfolio.py` — `record_trade()` 增加 `slippage_bps` 列，DB Schema 自动升级（ALTER TABLE）
- 晚报/早报中可查看每笔交易的滑点记录

### ✅ done — 压力测试
- `scripts/quant/backtest_cli.py` 新增 `crash-test` 命令
- 测试区间：2015-06~10（股灾）/ 2018全年（贸战）/ 2022-03~06（上海封控）
- 验收标准：Sharpe >= 0 且 MaxDD < 20%
- RSI(25/65) 在三个极端行情区间全部 PASS ✅

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
