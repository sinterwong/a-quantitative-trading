# Roadmap & TODO
> **核心目标：以提升股市实际胜率为导向。**
> 所有任务按 Win Rate Optimization Roadmap（短期/中期/长期）排列。
> Phase 1-5 已完成。进行中：Win Rate 优化路线图。
> *Last updated: 2026-04-12*


## Phase 1: Backend Service ✅ DONE

### P0 — Core Backend
- [x] **Backend skeleton** — `backend/main.py` with werkzeug server
- [x] **Portfolio Service** — SQLite persistence (positions, trades, cash, signals, orders)
- [x] **HTTP API** — 16 endpoints with validation + OpenAPI spec at `/docs`

### P1 — Scheduling & Automation
- [x] **Background scheduler** — 15:10 CST daily analysis trigger
- [x] **Self-healing** — log rotation + restart on crash
- [ ] **Service lifecycle** — `start.bat` / `stop.bat` / `status.bat`

### P2 — API Quality
- [x] **OpenAPI docs** — `GET /docs` (16 endpoints)
- [x] **Request validation** — `@validate_fields` decorator on all POST
- [ ] **Rate limiting** — prevent abuse of `/orders` endpoint

---

## Phase 2: Broker Integration ✅ DONE

- [x] **Broker abstraction layer** — `backend/services/broker.py`
- [x] **Paper executor** — VWAP model, order lifecycle (submitted/filled/rejected/cancelled)
- [x] **Orders API** — `POST /orders/submit`, `GET /orders`, `GET /orders/<id>`, `POST /orders/<id>/cancel`
- [ ] **Real broker** — Futu/Tiger (awaiting account credentials from Sinter)

---

## Phase 3: Real-time Intelligence ✅ DONE

- [x] **Signal engine v2** — `signals.py`: A-share limit detection + RSI + volume ratio
- [x] **Bulk price fetch** — Tencent `qt.gtimg.cn` batch API (corrected field indices)
- [x] **IntradayMonitor** — daemon thread, 5-min polling 9:35-11:30 & 13:00-14:55 CST Mon-Fri
- [x] **Feishu push** — REST API direct push (appId/appSecret auth, tested ✅)
- [x] **Cooldown tracking** — 15-min per-symbol to prevent spam

**A-share 专用信号类型：**
- `LIMIT_UP` / `LIMIT_DOWN` — 涨跌停（含放量/缩量判断）
- `LIMIT_RISK_UP` / `LIMIT_RISK_DOWN` — 逼近涨跌停（<1%）
- `WATCH_LIMIT_UP` / `WATCH_LIMIT_DOWN` — 接近涨跌停（<3%）
- `RSI_BUY` / `RSI_SELL` — RSI 超买超卖 + 动量确认
- `WATCH_BUY` / `WATCH_SELL` — RSI 极端区域
- `VOLATILE` — 大幅波动警示（>3%）

---

## Phase 4: Research Infrastructure ✅ DONE

### Signal System
- [x] **Walk-Forward** — `walkforward_job.py` + `walkforward_persistence.py` + 季度自动重训
- [x] **Market regime** — `MarketRegimeSource` 已有 (MA200)
- [x] **News quality scoring** — `news_quality.py`（含糊词过滤 + 权威来源加分）

### Data Quality
- [x] **News quality scoring** — filter vague phrases (有望/或将/知情人士), weight official sources
- [x] **Volume-Price confirmation** — 内置于 LIMIT_UP 信号（放量=真拉升，缩量=诱多）

### Portfolio
- [x] **Multi-stock expansion** — `PortfolioEngineV3` 已支持多股 + 行业仓位限制
- [x] **Stop-loss module** — 日频止损检查（每交易日收盘价 vs 成本价）
- [x] **A-share circuit breaker** — 15% 组合回撤熔断（PortfolioEngineV3 `max_drawdown_limit=0.15`）

### Backtesting
- [x] **In-sample / out-of-sample** — `WalkForwardAnalyzer` 强制分离
- [x] **Monte Carlo simulation** — `monte_carlo.py`（2000次迭代，分位数统计，破产风险）
- [x] **Benchmark** — `benchmark.py`（沪深300 ETF 510310.SH 对比，Alpha/Beta/信息比率）

---

## Phase 5: Productization

- [x] **Web UI** — Streamlit dashboard (streamlit_app.py, 6 pages)
- [x] **Scheduled reports** — `report_sender.py`: 9:00 早报 + 15:30 晚报（已测试推送成功）
  - OpenClaw cron 已配置：Morning Report (0 9 * * 1-5) + Closing Report (30 15 * * 1-5)
- [x] **Strategy plugins** — `strategies/` 插件架构（RSI/MACD/BollingerBand）
  - `strategies/__init__.py` — 注册中心 `STRATEGY_REGISTRY` + `load_strategy()`
  - `strategies/base.py` — `BaseStrategy` 基类（含 `compute_rsi/compute_ema`）
  - `strategies/rsi_strategy.py` — RSI 超买超卖插件
  - `strategies/macd_strategy.py` — MACD 金叉死叉插件
  - `strategies/bollinger_strategy.py` — 布林带插件
  - `backend/services/strategy_loader.py` — 从 `params.json` 动态加载
  - `params.json` — 策略配置（symbols + params）
- [ ] **PostgreSQL** — upgrade from SQLite for multi-user
- [x] **Changelog + License**

---

## Win Rate Optimization Roadmap
> 目标：以提升股市实际胜率为导向。所有任务按 "信号质量 × 风控 × 执行" 框架排列。

### 短期（1-4 周）— 直接提升信号精度
> 核心：减少信号噪声，提高每笔交易的期望值

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | **WFA 定时化** | 当前 WFA 已能跑，但需手动触发。接入 cron 每季度自动重训，保持参数跟得上市场变化 |
| P0 | **分钟级信号源** | 日线 RSI 是近似，引入 15min/60min K线作为信号确认层，减少假信号 |
| P0 | **止损优化（ATR）** | 固定 % 止损在低波动的长江电力上偏大，在高波动的小票上偏小。改用 ATR 动态止损 |
| P1 | **新闻量化升级** | 关键词 → 语义 embedding 检索（成本可控：使用 BGE-small 或 Jina embeddings 本地运行），减少标题党干扰 |
| P1 | **放量确认强化** | 当前用腾讯 vol_ratio，是日内代理。用分钟量与 5 日均量对比，放量突破确认更可靠 |
| P2 | **机构席位数据** | 东方财富主力净流入、大单追踪（Level2 数据暂不需要，先用现有免费接口） |

**短期验收标准：**
- [ ] WFA 每季度自动运行，最新参数写入 `live_params.json`
- [ ] RSI + 放量共振作为买入信号（减少日线 RSI 假突破）
- [ ] ATR 止损 vs 固定 % 止损的回测对比报告（期望：降低极端亏损频率）

---

### 中期（1-3 个月）— 信号 → 交易闭环
> 核心：把研究信号转化为可执行的交易逻辑，并验证期望值为正

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | **多空信号体系** | 当前仅做多（买入）。引入空头信号（融券/卖出），在 bear market 中也能获利 |
| P0 | **持仓上限扩展** | 从 2-3 只扩展到 5-8 只，降低单只黑天鹅风险，同时分散 |
| P1 | **季节性因子** | A 股存在明显季节效应（春节躁动、春季行情、年底博弈）。用历史数据验证，在高胜率季节增加仓位 |
| P1 | **Market Regime 接入实盘** | MA200 快线/慢线区分牛熊，在 bear market 自动收紧仓位上限 |
| P1 | **Paper Trade 闭环验证** | 每日记录信号 → 执行 → 结果，对比胜率统计（非回测，是真实样本） |
| P2 | **Walk-Forward 结果分析** | 从 WFA 输出中提取最优参数分布，判断参数稳定性（方差过大说明策略脆弱） |
| P2 | **动态选股时效** | 盘中选股结果缓存 5 分钟，超时重新抓取，避免用过期热门板块数据 |

**中期验收标准：**
- [ ] Paper Trade 胜率统计（>50% Sharpe > 0.5）
- [ ] bear market 中空头信号有效（对冲多头亏损）
- [ ] 持仓扩展到 5 只以上，最大持仓 <25%

---

### 长期（3-12 个月）— 专业化与风控升级
> 核心：逼近专业量化基金水平，降低最大回撤，提升夏普

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | **真实券商接入** | Futu OpenAPI 或 Tiger Trade，打通信号 → 订单 → 成交 → 持仓完整链路 |
| P0 | **动态仓位管理** | Kelly 公式 + ATR 当前仓位，当前为固定 %。升级为根据波动率自适应 |
| P1 | **事件驱动信号** | 业绩公告、并购重组、政策文件（财政蓝皮书等）纳入信号体系 |
| P1 | **多因子模型** | 引入 PB、PE、ROE、营收增速等价值因子，与技术信号叠加 |
| P1 | **回撤控制精细化** | 个股止损（ATR）+ 组合止损（15%熔断）+ 行业止损（单一行业 <40%）三层 |
| P2 | **Level2 行情** | 十档买卖盘、逐笔委托数据，接入后信号精度再上一个台阶 |
| P2 | **压力测试** | 对持仓组合做历史极端行情（2015 股灾、2018 贸易战、2022 上海封控）模拟 |
| P3 | **组合优化器** | 均值-方差优化（MVO），在给定风险上限下最大化预期收益 |

**长期验收标准：**
- [ ] 实盘（真实资金，小仓位）连续运行 3 个月
- [ ] Sharpe > 1.0（年化）
- [ ] 最大回撤 < 15%
- [ ] 胜率（盈利交易/总交易）> 55%

---

## Icebox
- TradingView chart embedding
- Email reports
- Dark mode for web UI
- Multi-language (EN/CN)
- Mobile push via Server酱

---

*Last updated: 2026-04-12*
