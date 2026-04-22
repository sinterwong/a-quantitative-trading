# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased] — Phase 2 Alpha 扩展

### Added

#### P2-E — Regime → StrategyRunner 联动
- **`core/regime.py`** — 独立市场环境检测模块（无副作用，进程内日缓存）
  - `RegimeInfo` 数据类：`regime / close / ma20 / ma60 / atr_ratio / atr / reason`
  - 属性快捷方式：`is_bull / is_bear / is_volatile / is_calm`
  - 风控参数：`position_cap / signal_threshold_multiplier / allow_new_buys`
  - `get_regime(force_refresh=False)` — 进程内同天缓存，避免重复请求
- **`core/strategy_runner.py`** — Regime 联动
  - `RunnerConfig.regime_aware: bool = True` — 可关闭
  - `run_once()` 每轮开头检测一次 Regime（AkShare 上证日线）
  - BEAR → 禁止新开多仓（返回 SKIPPED）+ 信号阈值 ×1.4
  - VOLATILE → 信号阈值 ×1.2
  - `runner.current_regime` 属性暴露当前环境供监控使用

#### P2-A — MACD 趋势跟踪策略模块
- **`core/strategies/macd_trend.py`** — `MACDTrendFactor(Factor)`
  - 金叉（直方图负→正）→ BUY；死叉（直方图正→负）→ SELL
  - ATR 过滤：`atr_ratio > atr_threshold`（高波动期）时抑制 BUY 信号
  - 参数全部可网格搜索（`fast / slow / signal / atr_threshold`）
  - 直接对接 `WalkForwardAnalyzer(factor_class=MACDTrendFactor, ...)`
  - `make_macd_trend_pipeline()` 工厂函数，一行创建独立 pipeline

#### P2-B — OrderImbalance 因子接入实时信号引擎
- **`core/factors/price_momentum.py`** — 新增 `OrderImbalanceFactor(Factor)`
  - 基于 OHLCV 的买方压力代理（阳线成交量占比，rolling window=10）
  - `evaluate()` → z-score 归一化，> 0 = 买方压力高于均值
  - `signals()` → BUY（z > buy_z=0.5）/ SELL（z < sell_z=-0.5）
- **`core/factor_registry.py`** — 注册 `OrderImbalance`（`window=10` 默认）
- **`config/trading.yaml`** — RSI 策略加入 OI 因子 weight=0.2
  - 权重调整：RSI 0.5→0.4，MACD 0.3→0.2，ATR 0.2 不变，OI 新增 0.2

### Changed
- `RunnerConfig` 新增 `regime_aware: bool = True` 字段
- `StrategyRunner._process_symbol()` 信号阈值改为动态计算（含 Regime 乘数）

---

## [1.0.0] — 2026-04-12

### Added

#### Phase 1 — Backend Service
- **Backend skeleton** (`backend/main.py`) — werkzeug HTTP server, persistent process
- **Portfolio Service** (`backend/services/portfolio.py`) — SQLite persistence for positions, trades, cash, signals, orders
- **HTTP API** (`backend/api.py`) — 16 endpoints with field validation and OpenAPI spec at `/docs`
- **Swagger / OpenAPI** (`backend/openapi.json`) — importable into Postman/SwaggerUI
- **Request validation** — `@validate_fields` decorator on all POST endpoints
- **Background scheduler** — 15:10 CST daily analysis trigger
- **Service lifecycle scripts** — `start.bat`, `stop.bat`, `status.bat`

#### Phase 2 — Broker Integration
- **Broker abstraction layer** (`backend/services/broker.py`) — Broker interface
- **Paper executor** — VWAP model, full order lifecycle (submitted/filled/rejected/cancelled)
- **Orders API** — `POST /orders/submit`, `GET /orders`, `GET /orders/<id>`, `POST /orders/<id>/cancel`

#### Phase 3 — Real-time Intelligence
- **Signal engine v2** (`backend/services/signals.py`) — A-share specific signals:
  - `LIMIT_UP` / `LIMIT_DOWN` — 涨跌停（含放量/缩量判断）
  - `LIMIT_RISK_UP` / `LIMIT_RISK_DOWN` — 逼近涨跌停（<1%）
  - `WATCH_LIMIT_UP` / `WATCH_LIMIT_DOWN` — 接近涨跌停（<3%）
  - `RSI_BUY` / `RSI_SELL` / `WATCH_BUY` / `WATCH_SELL` — RSI 超买超卖 + 动量确认
  - `VOLATILE` — 大幅波动警示（>3%）
- **Bulk price fetch** — Tencent `qt.gtimg.cn` batch API with corrected field indices
- **IntradayMonitor** — daemon thread, 5-min polling (9:35–11:30 & 13:00–14:55 CST Mon–Fri)
- **Feishu push** — REST API direct push (tenant_access_token auth, tested)
- **Cooldown tracking** — 15-min per-symbol to prevent spam

#### Phase 4 — Research Infrastructure
- **Walk-Forward Analysis** (`scripts/quant/walkforward.py`) — Rolling train/test split
- **Walk-Forward persistence** (`backend/services/walkforward_persistence.py`) — SQLite storage + latest params
- **Walk-Forward job** (`scripts/walkforward_job.py`) — Single-run and daemon modes
- **News quality scoring** (`scripts/quant/news_quality.py`) — Vague phrase filter (有望/或将/知情人士), official source bonus
- **Dynamic stock selector v2** (`scripts/dynamic_selector.py`) — Five-dimension scoring (news 15% + sector 35% + flow 25% + tech 15% + consistency 10%)
- **Monte Carlo simulator** (`scripts/quant/monte_carlo.py`) — 2000 iterations, bootstrap resampling, percentile stats
- **CSI 300 Benchmark** (`scripts/quant/benchmark.py`) — Alpha/Beta/Information Ratio vs 510310.SH
- **SignalGenerator parameter aliasing** — `rsi_buy/rsi_sell` ↔ `oversold/overbought`
- **PortfolioEngineV3** — Multi-stock support, 15% circuit breaker, per-day stop-loss check

#### Phase 5 — Productization
- **Streamlit Web UI** (`streamlit_app.py`) — 6 pages:
  - Portfolio overview with P&L metrics
  - Real-time signals with limit-up/limit-down warnings
  - Dynamic stock selection (五维评分)
  - Backtest analysis (WFA + Monte Carlo)
  - Position details with RSI/volume ratio
  - Historical trades
- **Scheduled reports** (`backend/services/report_sender.py`):
  - Morning report (9:00 CST, market overview + holdings)
  - Closing summary (15:30 CST, index performance + P&L + signals)
  - OpenClaw cron jobs configured: `0 9 * * 1-5` and `30 15 * * 1-5`
- **Strategy plugin architecture** (`strategies/`):
  - `strategies/__init__.py` — `STRATEGY_REGISTRY` + `load_strategy()`
  - `strategies/base.py` — `BaseStrategy` abstract class with `compute_rsi/compute_ema`
  - `strategies/rsi_strategy.py` — RSI oversold/overbought plugin
  - `strategies/macd_strategy.py` — MACD golden/death cross plugin
  - `strategies/bollinger_strategy.py` — Bollinger Bands plugin
  - `backend/services/strategy_loader.py` — Dynamic strategy loading from `params.json`

### Changed
- **Circuit breaker threshold** — 50% → 15% portfolio drawdown
- **PortfolioEngineV3** — Daily stop-loss check on closing price vs cost basis
- **Default RSI parameters** — `rsi_buy`: 35, `rsi_sell`: 70
- **params.json** — Restructured with `strategies` section for plugin loader

### Fixed
- **Tencent field indices** — Corrected to [3]=price, [4]=prev_close, [31]=chg, [32]=pct
- **Symbol transformation** — `'600519.SH'` → `'sh600519'` (was `.replace('.SH','sh')` breaking suffix)
- **BlackListFilter** — Added missing `up_limit_discount` attribute
- **Report sender** — Fixed portfolio field names (`total_equity`, `entry_price` vs `total_value`, `market_value`)
- **Report sender** — Replaced timing-out futures API with A-share accessible indices (gold ETF)

### Dependencies
- Added: `streamlit>=1.30.0`, `plotly>=5.20.0`
- Core: `pandas`, `numpy`, `requests`, `flask>=3.0.0`, `werkzeug>=3.0.0`
