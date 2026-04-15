# TODO — 开发任务

> 最后更新：2026-04-15
> 当前基线：回测框架完成（backtest_cli.py），RSI WFA 验证通过（沪深300 Sharpe=0.467）

---

## P0 · 策略验证

### ✅ done — 回测引擎 CLI
- `scripts/quant/backtest_cli.py` — single / grid / compare / wf / fcompare 命令

### ✅ done — RSI 参数验证（沪深300）
- 最优：RSI(25/65)，SL=5%，TP=20%，ATR_threshold=0.85
- WFA 10 窗口 avg_sharpe=0.466，正收益窗口 60%

### 🔲 — ATR 阈值 WFA 精确化
- **新建** `scripts/quant/atr_wfa_scan.py`
- 对阈值 0.80/0.85/0.88/0.90/0.92 跑完整 WFA，取最优
- 更新 `params.json` 和 `backend/services/live_params.json`

### 🔲 — 宽基 ETF 泛化验证
- `python backtest_cli.py wf 159915.SZ`（创业板）
- `python backtest_cli.py wf 512690.SH`（酒ETF）
- 验收：Sharpe > 0.4 且正收益窗口 > 60%，否则记录失败原因

---

## P0 · 盘中信号引擎

### ✅ done — ATR ratio 计算
- `backend/services/signals.py` — `_compute_atr_ratio(symbol, period=14, lookback=20)`

### ✅ done — RSI BUY 高波动屏蔽
- `evaluate_signal()` — ATR ratio > 0.85 时 RSI BUY 变为 HOLD，reason 说明原因

### 🔲 — intraday_monitor 传入 atr_threshold
- `backend/services/intraday_monitor.py` — 从 `load_symbol_params()` 读取 atr_threshold 并传入 evaluate_signal

### 🔲 — ATR_ratio 字段暴露
- `evaluate_signal()` 返回值中 `SignalAlert` 增加 `atr_ratio: float` 字段，飞书推送时显示当前波动状态

---

## P1 · 信号质量

### 🔲 — confirm_signal_minute 集成
- `backend/services/intraday_monitor.py` — WATCH_BUY / RSI_BUY 信号调用 `confirm_signal_minute()` 二次确认
- 参数：minute_scale（默认 15），配置化

### 🔲 — 基本面过滤
- **新建** `backend/services/fundamentals.py`
- `fetch_fundamentals(symbol)` → `{'pe', 'pb', 'roe', 'revenue_growth'}`
- `evaluate_signal()` 前置：PE > 80 或 ROE < 0 标的跳过

### 🔲 — 北向共振信号
- `backend/services/northbound.py` — `check_northbound_crossover(symbol)` → (bool, float)
- `evaluate_signal()` 中 RSI_BUY + 北向单日净流入 > 50亿 → 信号强度 ×1.5
- 配置项：`northbound_threshold`（默认 50亿）

### 🔲 — 新闻情绪打分
- **新建** `scripts/quant/news_scorer.py`
- `score_news(news_list)` → `[{title, score, grade}]`，grade: A/B/C/D
- 关键词：利好/超预期 → 正；风险/减持 → 负；标题含数字 +8-40字 → 加分
- 集成进 `dynamic_selector.py` `calc_news_score()`

---

## P1 · 风控

### 🔲 — 组合熔断
- `backend/services/intraday_monitor.py` — `check_portfolio_risk(portfolio_value, peak)`
- 回撤 8% → 飞书警告 + 仓位收紧至 50%；回撤 12% → 清仓
- 配置项：`portfolio_max_drawdown_warn=0.08`，`portfolio_max_drawdown_stop=0.12`

### 🔲 — 行业集中度
- **新建** `scripts/quant/sector_map.json` — `{代码前缀: 行业名}` 手动映射
- `backend/services/portfolio.py` — `check_sector_concentration(positions)`
- 单一行业 > 40% → 强制减仓至 40%
- 配置项：`max_sector_pct=0.40`

### 🔲 — Kelly 仓位
- **新建** `scripts/quant/position_sizer.py`
- `compute_kelly(win_rate, avg_win, avg_loss) → float`（半 Kelly = Kelly × 0.5）
- 最小仓位 5%，最大 30%
- `intraday_monitor.py` 开仓前调用，更新 `max_position_pct`

### 🔲 — 压力测试
- `scripts/quant/backtest_cli.py` 新增 `crash-test` 命令
- 测试区间：2015-06 ~ 2015-09（股灾）、2018 全年（贸易战）、2022-03 ~ 2022-06（上海封控）
- 输出：最大日亏损、连续亏损天数、RSI 假信号率

### 🔲 — 风控触发日志
- `backend/services/alert_history.py` — 新增风控事件类型 `stop_triggered` / `portfolio_cascade` / `sector_violation`
- 定期（每周）推送飞书风控复盘

---

## P2 · 策略多元化

### 🔲 — MACD 策略验证
- `scripts/quant/backtest_cli.py` 新增 `macd-compare` 命令
- 参数网格：fast(8,10,12) / slow(20,26,30) / signal(7,9,12)
- 对比：纯 RSI vs RSI+MACD 共振（两个信号同时 BUY 才开仓）
- 结果写入 `live_params.json`

### 🔲 — 布林带策略验证
- 同 MACD，参数：period(20) / std_mult(1.5, 2.0, 2.5)
- 对比布林带均值回归 vs RSI 动量

### 🔲 — 板块轮动策略
- `scripts/quant/backtest_cli.py` 新增 `sector-rotate` 命令
- 逻辑：资金流入最强板块 ETF 等权持有，每月 rebalance
- 对比：板块轮动 vs 沪深300 买入持有

---

## P2 · 实盘对接

### 🔲 — 涨跌停熔断
- `backend/services/signals.py` — `evaluate_signal()` 增加 LIMIT_UP / LIMIT_DOWN 语义
  - 涨停日已有持仓 → WATCH_SELL（不等开板）；无持仓 → 禁止追
  - 跌停日已有持仓 → WATCH_SELL（逃生）；无持仓 → 禁止抄
- `backend/services/broker.py` — 订单提交时拦截涨跌停标的

### 🔲 — 滑点监控
- `backend/services/broker.py` — 订单成交后对比「信号触发价 vs 实际成交价」
- `daily_journal.py` — 记录滑点到交易日志

### 🔲 — 完整交易日志
- 字段：signal_src / triggered_at / filled_at / fill_price / slippage_bps / commission
- `daily_journal.py` → `monthly_audit.py` 自动生成月度审计

---

## P3 · 架构

### 🔲 — PostgreSQL 迁移
- 设计 Schema（参考 `portfolio.db` 当前表）
- **新建** `scripts/migrate_sqlite_to_pg.py`
- `backend/services/portfolio.py` — SQLAlchemy + PostgreSQL

### 🔲 — Backend 崩溃自动拉起
- Windows Service（`pywin32` + `nssm`）
- 或 supervisord 跨平台
- `GET /health` → 非 200 则自动重启

### 🔲 — Redis 缓存层
- `backend/services/portfolio.py` — 持仓/信号状态缓存
- TTL：信号状态 5 分钟，持仓 1 分钟

---

## 任务状态总览

| 优先级 | 任务 | 文件 |
|---------|------|------|
| P0 | ATR 阈值 WFA 精确化 | `scripts/quant/atr_wfa_scan.py` |
| P0 | 宽基 ETF 泛化验证 | `backtest_cli.py wf` |
| P0 | intraday_monitor 集成 ATR | `backend/services/intraday_monitor.py` |
| P1 | 组合熔断（8%/12%回撤） | `backend/services/intraday_monitor.py` |
| P1 | 基本面过滤（PE/ROE） | `backend/services/fundamentals.py` |
| P1 | 涨跌停熔断 | `backend/services/signals.py` + `broker.py` |
| P1 | 行业集中度 | `sector_map.json` + `portfolio.py` |
| P2 | Kelly 仓位 | `scripts/quant/position_sizer.py` |
| P2 | 压力测试 | `backtest_cli.py crash-test` |
| P2 | MACD 策略验证 | `backtest_cli.py macd-compare` |
| P2 | 滑点监控 | `backend/services/broker.py` |
| P3 | PostgreSQL 迁移 | `scripts/migrate_sqlite_to_pg.py` |
| P3 | Backend 崩溃拉起 | Windows Service / supervisord |
