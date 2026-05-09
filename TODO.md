# TODO — A 股量化交易系统开发任务

> 评估日期：2026-05-08
> 当前状态：**P0/P1 全部完成** · 1011 测试通过 · 新增 77 个测试 · 0 回归
> 核心问题：**实装 ≠ 集成** — 多个核心模块（ExitEngine / VWAP / PortfolioOptimizer / CVaR）已实现但未真正接入生产路径 → **已修复**
> 评估详情见 `/home/sinter/.claude/plans/enchanted-roaming-pebble.md`

## 进度速查

| 任务 | 状态 | 关键交付 | 测试 |
|---|---|---|---|
| P0-1 ExitEngine 接入回测+主循环 | ✅ | `BacktestEngine.use_exit_engine` / `StrategyRunner.use_exit_engine` | 4 |
| P0-2 因子降级权重归一化 | ✅ | `pipeline_factory._safe_add` + `MIN_FACTORS_REQUIRED` 守卫 | 5 |
| P0-3 PortfolioOptimizer 接入 | ✅ | `RunnerConfig.enable_rebalance` + `_maybe_rebalance` | 6 |
| P0-4 Kelly + 回撤折扣 | ✅ | `OMS._drawdown_discount` + max_position_pct 截断 | 7 |
| P0-5 CVaR + Monte Carlo 调度 | ✅ | `scripts/daily_risk_report.py` + 15:30 Scheduler | 5 |
| P0-6 Futu OpenD 联调 | ⏳ 需真实环境 | — | — |
| P1-7 VWAP/TWAP intraday 路由 | ✅ | `IntradayMonitor._submit_with_routing` + ExecutionConfig | 7 |
| P1-8 ML 重训机制 | ✅ | `MLPredictionFactor` retrain_every 实装 + `scripts/ml_train_all.py` | 8 |
| P1-9 NLP 因子工业化 | ✅ | Parquet 优先读取 + `scripts/nlp_batch_score.py` | 4 |
| P1-10 配对交易接入主调度 | ✅ | Scheduler 周三 `_trigger_pairs_trading` | 5 |
| P1-11 回测一字涨跌停/退市 | ✅ | `simulate_limit_up_down` + `simulate_delisting` | 6 |
| P1-12 TCA 反馈闭环 | ✅ | `scripts/daily_tca.py` + 15:45 Scheduler + ImpactEstimator 校准 | 9 |
| P1-13 Regime 升级 | ✅ | 自适应 ATR 阈值 + MA60 斜率 + 切换冷却期 + 减仓目标 | 11 |

---

## 优先级说明

| 等级 | 含义 | 适用判断 |
|---|---|---|
| **P0** | 实盘前必修 — 不修复将导致系统正确性错误 | 回测/实盘行为不一致、风控被绕过、声明的功能未实装 |
| **P1** | Alpha 与执行质量 — 直接影响策略表现 | 因子未生产化、执行成本未优化、ML 未自适应 |
| **P2** | 生产化与工程债 — 长期运维质量 | 冗余、可观测性、CI、文档一致性 |

**推进方案**：
- **方案 A（保守，2–3 周）**：仅 P0
- **方案 B（推进，1.5–2 月）**：P0 + P1 — 达成"50–200 万实盘可承接"
- **方案 C（全面，3–4 月）**：P0 + P1 + P2 — 达成"小型私募级生产系统"

---

## P0 — 实盘前必须修复

### P0-1 ExitEngine 接入回测与策略主循环

**问题**：`core/exit_engine.py` 的 10 层退出体系仅在 `backend/services/intraday_monitor.py:1209` 调用；`StrategyRunner.run_once()` 与 `BacktestEngine` 都不走。导致回测出来的退出表现 ≠ 实盘 intraday_monitor 的退出表现，参数迭代基于失真信号。

- [ ] **改造 `core/strategy_runner.py`**：在 `run_once()` 末尾对每个持仓调用 `ExitEngine.generate(positions, market_data, pipeline_scores)`，按 `ExitPriority` 优先级提交卖单
- [ ] **改造 `core/backtest_engine.py`**：每根 bar 收盘后对持仓运行 ExitEngine，使用次日 open 成交（保持现有前视偏差修复）
- [ ] **抽取共享逻辑**：将 `intraday_monitor._run_exit_engine()` (lines 1267–1440) 中的核心逻辑下沉到 `ExitEngine` 类方法，消除三处重复
- [ ] **新增对比测试** `tests/test_exit_engine_consistency.py`：固定标的 + 时间窗，验证回测路径与盘中监控路径的退出信号序列完全一致

**关键文件**：`core/exit_engine.py:140-523`、`core/strategy_runner.py`、`core/backtest_engine.py`、`backend/services/intraday_monitor.py:1267-1440`
**验证**：新增对比测试通过 + 回测年化收益与最大回撤数值发生预期变化（变化幅度记录到 PR description）

---

### P0-2 因子降级时权重重新归一化

**问题**：`core/pipeline_factory.py:60-85` 中基本面/宏观因子用 try-except 跳过，但若 `DynamicWeightPipeline.add()` 失败，剩余因子权重不归一化（理想 1.0 总权重可能只剩 0.65）。

- [ ] **审查 `core/factor_pipeline.py DynamicWeightPipeline`** 的权重归一化逻辑，确认失败因子是被剔除还是权重置零
- [ ] **修复 `core/pipeline_factory.py`**：失败时显式从 pipeline 移除该因子，并对剩余权重重新归一化（而非依赖隐式逻辑）
- [ ] **新增测试** `tests/test_pipeline_factory_degradation.py`：mock 基本面/宏观失败场景，断言剩余因子总权重 = 1.0

**关键文件**：`core/pipeline_factory.py:60-85`、`core/factor_pipeline.py`
**验证**：测试通过；离线 + 在线两种环境下打印 `pipeline.weights` 总和均为 1.0 ± 1e-6

---

### P0-3 PortfolioOptimizer + Allocator 接入主策略循环

**问题**：`core/portfolio_optimizer.py`（MVO/BL/风险平价/最大分散化）与 `core/portfolio_allocator.py:239 needs_rebalance()`（21 天 + 5% 阈值）已完整实现，但生产路径完全不调用 — `StrategyRunner` 与 `AsyncRunner` 都没有引用。

- [ ] **在 `core/strategy_runner.py.run_once()` 中嵌入再平衡检查**：每轮末尾调用 `allocator.needs_rebalance(current_market_value)`
- [ ] **触发时**：用 `PortfolioOptimizer.max_sharpe()` 或 `risk_parity()`（可配置）重计目标权重 → 与当前持仓比对 → 通过 OMS 下 rebalance order
- [ ] **配置项**：`config/trading.yaml` 新增 `portfolio.rebalance.method`、`portfolio.rebalance.period_days`、`portfolio.rebalance.drift_threshold`
- [ ] **新增 E2E 测试**：mock 持仓漂移到 6%，断言 `run_once()` 触发 rebalance order

**关键文件**：`core/strategy_runner.py`、`core/portfolio_allocator.py:239`、`core/portfolio_optimizer.py`、`config/trading.yaml`
**验证**：E2E 测试通过；运行一次盘后报告，`outputs/daily_ops/` 显示 rebalance 决策

---

### P0-4 Kelly 仓位与回撤上限联动

**问题**：`core/oms.py _kelly_shares()` 基于历史胜率计算仓位，不感知实时回撤。高胜率时可能突破 PreTrade 仓位上限，导致信号被拒后回退混乱。

- [ ] **改造 `core/oms.py _kelly_shares()`**：引入回撤折扣因子 `shares = kelly_fraction × (1 - current_drawdown / max_drawdown_limit) × capital / price`
- [ ] **新增辅助函数** `risk_engine.get_current_drawdown()` 从 portfolio service 读取实时净值并计算
- [ ] **配置项**：`config/trading.yaml` 新增 `risk.max_drawdown_limit`（默认 0.15）
- [ ] **新增测试**：构造高胜率 + 大回撤场景，断言仓位被压缩到合理水平

**关键文件**：`core/oms.py`、`core/risk_engine.py`
**验证**：测试通过；回测 2024 年 BEAR 时段，Kelly 触发的最大单仓位低于 PreTrade 限制

---

### P0-5 CVaR / Monte Carlo 压力测试实装

**问题**：`ARCHITECTURE.md:50` 宣称"5000 次 MC 压力测试"，实际仅 `core/risk_engine.py` 注释提及，无对应函数。声明与实现不一致。

- [ ] **新增 `core/portfolio_risk.py.run_monte_carlo_stress(positions, n=10000)`**：基于历史协方差矩阵蒙特卡洛模拟次日 P&L 分布
- [ ] **计算 95% CVaR**：对模拟分布取最坏 5% 期望损失
- [ ] **每日盘后调度**：在 Scheduler 注册 15:30 任务，输出到 `outputs/risk_daily/risk_{date}.json`
- [ ] **与 hard SL 联动**：CVaR 超阈值时 AlertManager 推送 CRITICAL，第二日开盘减仓
- [ ] **新增测试** `tests/test_portfolio_risk_mc.py`：固定随机种子，断言数值稳定且分布合理

**关键文件**：`core/portfolio_risk.py`、`core/risk_engine.py`、`backend/main.py`（Scheduler 注册）、`core/alerting.py`
**验证**：测试通过；连续运行 5 个交易日，`outputs/risk_daily/` 累积报告

---

### P0-6 Futu OpenD 真实联调（实盘验证闭环）

**问题**：`core/brokers/futu.py` 框架完整、有离线降级，但订单回调链路从未真实跑通。这是从"模拟系统"过渡到"实盘可承接"的最后一环。

- [ ] **本机部署 Futu OpenD**（`port 11111`，TrdEnv.SIMULATE）
- [ ] **跑 `core/brokers/futu.py.connect()` + 简单买卖单 round-trip 验证**
- [ ] **配置 `core/paper_trade_validator.py FutuPaperValidator`**：连续运行 2 周，输出 JSON 报告到 `outputs/paper_trade/`
- [ ] **目标**：信号一致率 ≥ 95%（`signal_match_target`）
- [ ] **完善订单状态机**：补全 PENDING → PARTIALLY_FILLED → FILLED / REJECTED / CANCELLED 异步回调
- [ ] **新增集成测试**：mock OpenD 响应，覆盖部分成交、撤单失败、被拒三种异常路径

**关键文件**：`core/brokers/futu.py`、`core/paper_trade_validator.py`、`core/oms.py`
**验证**：2 周纸交易日报均显示信号一致率 ≥ 95%；异常路径测试通过

---

## P1 — Alpha 与执行质量提升

### P1-7 VWAP/TWAP 进入 intraday_monitor 真实下单路径

**问题**：`core/execution/*` 与 `core/oms.py:535 submit_algo_order()` 完整实现，但仅 `streamlit_app.py:1214` 调用。生产盘中下单仍走 `OMS.submit_order()` 市价/限价单。

- [ ] **改造 `backend/services/intraday_monitor.py` 的下单分发逻辑**：订单金额 > 50 万 或 股数 > 1 万股时自动路由到 TWAP（默认 30 分钟内均匀拆分）
- [ ] **校准 Almgren-Chriss 系数**：用过去 30 个交易日实际成交滑点回归校准 `core/execution/impact_estimator.py PERMANENT_COEFF` 与 `TEMPORARY_COEFF`（当前硬编码 5bps）
- [ ] **历史成交量分布**：替换 `_default_volume_profile()` 的 U 型半正弦近似为标的过去 20 日的实际分钟成交量分布
- [ ] **配置项**：`config/trading.yaml` 新增 `execution.algo_threshold_amount`、`execution.algo_threshold_shares`
- [ ] **新增测试**：单笔 100 万买单应被切成 N 个子单

**关键文件**：`backend/services/intraday_monitor.py`、`core/oms.py:535`、`core/execution/{vwap,twap,impact_estimator}.py`
**验证**：测试通过；TCA 报告显示算法订单的 IS 优于普通限价单

---

### P1-8 ML 模型重训机制 + 多标的扩张 + FactorSelector 接入

**问题**：仅一支 ETF 模型 `data/ml_models/510310_SH/xgboost/20260428.joblib`；`retrain_every` 参数存在但 `_bars_since_train` 计数器不递增；OOS accuracy=0.5（无预测力）；`core/ml/factor_selector.py` 已实现但 `pipeline_factory` 不引用。

- [ ] **修复 `core/ml/price_predictor.py` 重训逻辑**：实装 `_bars_since_train` 递增 + 触发条件 + 自动调用 `WalkForwardTrainer`
- [ ] **Scheduler 注册周末重训任务**（周六 02:00），覆盖 watchlist 全部标的
- [ ] **质量门控**：OOS Sharpe < 0.05 时不更新现有模型（保留旧版本）
- [ ] **接入 FactorSelector**：`pipeline_factory.build_pipeline()` 调用 `FactorSelector.predict()` 输出 `factor_weights` 字典覆盖默认权重
- [ ] **批量训练脚本** `scripts/ml_train_all.py`：按 watchlist 跑全量训练，输出训练摘要到 `outputs/ml_training/`

**关键文件**：`core/ml/price_predictor.py`、`core/ml/factor_selector.py`、`core/pipeline_factory.py`、`backend/main.py`
**验证**：watchlist 全标的有可用模型；FactorSelector 接入后 IC 加权与默认权重不同；Sharpe 门控生效

---

### P1-9 NLP 因子工业化或剔除

**问题**：`core/factors/nlp.py NewsSentimentFactor` 实时调用 Claude API，延迟与成本不可控；TODO 计划"跑 1 个月历史数据 IC 检验"未执行。

- [ ] **离线批处理脚本** `scripts/nlp_batch_score.py`：每日 06:00 批量抓取新闻 + Claude 打分 → 缓存为 Parquet
- [ ] **改造 `NewsSentimentFactor.evaluate()`**：仅读 Parquet 缓存，不再实时调用 API
- [ ] **跑 1 个月历史 IC 检验**：调用 `core/research.py FactorICAnalyzer`，输出 `outputs/factor_ic_nlp.png` + JSON
- [ ] **决策门控**：IC > 0.02 → 接入 `pipeline_factory`（权重 0.05）；IC ≤ 0.02 → 保留代码但从 pipeline 剔除并标记 deprecated

**关键文件**：`core/factors/nlp.py`、`scripts/nlp_batch_score.py`（新建）、`core/pipeline_factory.py`
**验证**：1 个月 IC 报告生成；批处理脚本正常出缓存；pipeline `evaluate()` 不触发任何网络 API

---

### P1-10 行业轮动 / 配对交易接入主调度

**问题**：`core/strategies/{sector_rotation,pairs_trading}.py` 完整实现，但仅通过 HTTP API 端点暴露（`/analysis/sector_rotation`、`/analysis/pairs_trading`），未接入 `DynamicWeightPipeline` 或多策略组合。

- [ ] **Scheduler 注册周一 09:30 行业轮动任务**：输出超额信号到 `outputs/sector_rotation/`
- [ ] **配对交易常驻监测**：每日 14:30 扫描候选对，发现 z-score 突破 ±2σ → 推送告警
- [ ] **多策略组合**：`PortfolioAllocator` 注册三个子策略（多因子 / 行业轮动 / 配对交易），按风险预算分配资金
- [ ] **WFA 验证**：跑行业轮动策略 18 月训练 + 6 月测试 WFA，目标 OOS Sharpe > 0.4

**关键文件**：`backend/main.py`、`core/portfolio_allocator.py`、`core/strategies/{sector_rotation,pairs_trading}.py`、`scripts/walkforward_job.py`
**验证**：Scheduler 任务每周触发；多策略组合在每日运营报告中显示三路 P&L

---

### P1-11 回测引擎补全：停牌、退市、跌停板模拟

**问题**：`core/backtest_engine.py` 仅 `volume=0` 跳过开仓，但跌停板封单未模拟（涨停买不到、跌停卖不出）；退市未触发清仓。导致回测结果过于乐观。

- [ ] **跌停板模拟**：BUY 信号遇 `close == high == open`（一字涨停）时跳过；SELL 遇一字跌停时排队
- [ ] **退市清仓**：标的从交易所摘牌时按最后交易日 close 强制平仓
- [ ] **停牌期间持仓估值**：用最后交易日 close（已实装），但需补充复牌后跳空 gap 风险测试
- [ ] **新增数据源** `core/data_layer.get_suspension_calendar(symbol)`：返回停牌/退市日期表
- [ ] **新增测试**：构造一字涨跌停 + 退市场景，断言策略行为正确

**关键文件**：`core/backtest_engine.py`、`core/data_layer.py`
**验证**：测试通过；2024 年回测对比新旧版本，年化收益预期下降 1–3%（反映真实摩擦）

---

### P1-12 TCA 反馈闭环

**问题**：`core/tca.py` 计算 IS、Market Impact 完整，但仅 Streamlit 手动调用；无每日自动统计；无反馈到执行参数的闭环。

- [ ] **新增 `scripts/daily_tca.py`**：盘后从 portfolio.db 读取当日成交记录 → 计算 IS / impact / slippage → 写 `outputs/tca_daily/tca_{date}.json`
- [ ] **Scheduler 注册 15:45 自动触发**
- [ ] **月度 TCA 汇总报告**：每月 1 日生成 `outputs/tca_monthly/tca_{YYYY-MM}.pdf`，含按"标的 / 时段 / Regime / 因子信号强度"的成本分解
- [ ] **反馈闭环**：滚动 20 日 IS 偏离基线 > 1.5σ 时自动调整 `ImpactEstimator` 系数；TWAP 子单数随实现 IS 反向调整

**关键文件**：`core/tca.py`、`scripts/daily_tca.py`（新建）、`backend/main.py`
**验证**：连续 5 日生成 TCA 报告；月度报告 PDF 可查；ImpactEstimator 系数自动调整有日志记录

---

### P1-13 Regime 升级：主动减仓 + 阈值自适应

**问题**：`core/regime.py:37-110` 仅 4 状态固定阈值（ATR_VOLATILE_THRESHOLD=0.85 硬编码）；BEAR 时仅"禁止新买"无主动减仓。

- [ ] **BEAR 触发主动减仓**：进入 BEAR 时减持仓位 25%（保留 75% 等价 cash），出 BEAR 后逐步加回
- [ ] **ATR 阈值自适应**：用滚动 252 日的 90 分位数代替固定 0.85，每月更新
- [ ] **MA20/MA60 趋势判定**：增加 MA60 斜率维度，避免横盘震荡误判
- [ ] **Regime 切换冷却期**：5 个交易日不重复切换，减小抖动
- [ ] **新增测试**：构造 2018 / 2022 / 2024 三段历史，验证 regime 切换时点合理

**关键文件**：`core/regime.py`
**验证**：测试通过；回测 2018 BEAR 期间最大回撤显著优于无主动减仓版本

---

## P2 — 生产化与工程债

### P2-14 Broker 实现冗余清理

**问题**：`core/brokers/EventDrivenPaperBroker`（commit a40b1e8 改名）与 `backend/services/broker.PaperBroker` 两套并存，分工不清。

- [x] **梳理两套 PaperBroker 的差异**：分工已澄清——`backend/services/broker.PaperBroker` 是同步 PortfolioService DB 写入器（生产链路）；`core/oms.EventDrivenPaperBroker` 是事件驱动 OMS 撮合器（回测/单测）；`core/brokers/SimulatedBroker` 是 BrokerBase 接口模拟（实盘验证前的接口对齐）
- [x] **抽取共享撮合工具** `core/brokers/fill_simulator.py`：滑点/涨跌停/佣金 4 个纯函数被三处复用，避免逻辑漂移
- [ ] **完整结构合并**（深度重构）：保留 EventDrivenPaperBroker 为唯一实装、backend 退化为薄包装。当前评估为高风险（事件驱动 vs 同步语义不同），暂缓——已有共享工具保证滑点/佣金一致
- [x] **新增测试** `tests/test_fill_simulator.py` 14 用例

**关键文件**：`core/brokers/{paper,simulated}.py`、`backend/services/broker.py`
**验证**：现有测试全部通过；生产 paper 模式行为不变

---

### P2-15 运行器收敛

**问题**：`StrategyRunner`（456 行同步）与 `AsyncStrategyRunner`（494 行异步）共享 RunnerConfig 但实现分歧，维护两套增加 Bug 风险。

- [ ] **`AsyncStrategyRunner` 设为默认生产运行模式**
- [ ] **`StrategyRunner` 降级为回测/单测专用**（注释标明，新代码不应使用）
- [ ] **统一两者的事件发射格式**，便于后续完全迁移
- [ ] **更新 `backend/main.py`** 启动逻辑

**关键文件**：`core/strategy_runner.py`、`core/async_runner.py`、`backend/main.py`
**验证**：生产路径切换到 async 后 7 日运行无异常

---

### P2-16 数据稳定性

**问题**：基本面季度数据手动拉取；分钟 K 线仅 AKShare 免费源（限流风险）。

- [x] **基本面季度自动更新**：Scheduler 已在季度末（3/6/9/12 月 25 日起）+ 财报季首周（1/4/7/10 月 1-7 日）周一调用 `_refresh_fundamentals` → `FundamentalDataManager.invalidate()` + 强制重拉
- [ ] **分钟 K 线主源切换 Futu**：暂缓——本机无 Futu OpenD，无法验证；当前 AKShare 路径已有熔断保护
- [x] **CircuitBreaker 增强**：`core/circuit_breaker.py` 通用熔断器（closed/open/half_open 三态，连续 N 次失败 → cooldown 期短路），已在 `_http_get`（tencent/sina）+ `_fetch_minute_bars_akshare` 接入；on_open 回调可触发告警
- [x] **新增测试** `tests/test_circuit_breaker.py` 13 用例

**关键文件**：`core/data_layer.py`、`core/fundamental_data.py`、`backend/main.py`
**验证**：模拟主源失败，备份源生效；季度财报披露日自动拉取生效

---

### P2-17 告警统一（飞书 + 分级路由）

**问题**：README 与 ARCHITECTURE 均提"飞书推送"，但 `core/alerting.py` 仅 wechat / dingtalk / email — 实现与文档不一致。

- [ ] **实装飞书 webhook**（结构与企业微信相同，复用 `_http_post`）
- [ ] **告警分级路由**：CRITICAL → 飞书 + 邮件；WARNING → 飞书；INFO → 仅日志
- [ ] **配置项**：`config/trading.yaml` 新增 `alerting.feishu_webhook`
- [ ] **新增测试** `tests/test_alerting_feishu.py`

**关键文件**：`core/alerting.py`、`config/trading.yaml`
**验证**：测试通过；本机配置真实 webhook 后实际推送到飞书群

---

### P2-18 可观测性补齐

**问题**：Prometheus metrics（`core/metrics.py`）已采集净值/现金/持仓/订单延迟，但缺关键风险指标；审计日志（`core/audit_log.py`）未覆盖订单取消、强平等关键事件。

- [x] **Prometheus 新增指标**：VaR/CVaR/drawdown/MC P95、broker 在线状态、订单状态分布、数据源失败计数（已在 OMS / data_layer / futu / daily_risk_report 接入）
- [ ] **审计日志补全**：订单取消原因、强平触发理由、参数变更前后值、ML 模型重训记录
- [ ] **Grafana 看板模板** `monitoring/grafana_dashboard.json`：净值曲线 + P&L 热力图 + 风险指标 + 系统健康

**关键文件**：`core/metrics.py`、`core/audit_log.py`、`monitoring/grafana_dashboard.json`（新建）
**验证**：`/metrics` 端点暴露新指标；Grafana 导入看板可看到完整视图

---

### P2-19 Scheduler 节假日感知 + 并发锁

**问题**：当前 Scheduler 仅按周一~周五运行，非交易日（节假日 + 周末调班）会空转 5 分钟轮询；15:00 afternoon_report 与 15:10 日终分析存在 portfolio.db 写入并发风险。

- [x] **接入 AKShare 交易日历**：`tool_trade_date_hist_sina()` 模块级集合缓存（已存在）
- [x] **Scheduler 非交易日 sleep 至次日 08:25**：`_seconds_until_next_check()` 计算休眠秒数，避免 30 秒空转
- [x] **portfolio.db 写入并发安全**：启用 WAL + busy_timeout=5000 + 进程内 `_WRITE_LOCK` 写互斥
- [x] **新增测试** `tests/test_scheduler_holiday_lock.py`：mock 节假日 / WAL 模式 / 并发写不丢失

**关键文件**：`backend/main.py`、`backend/services/portfolio.py`
**验证**：测试通过；2026 年五一节假日期间无任务触发

---

### P2-20 HTTP API 认证 + 限流

**问题**：`backend/api.py` 30+ 端点完全裸跑，无认证、无限流。如对外暴露存在风险。

- [x] **API Key 认证**：环境变量 `TRADING_API_KEY`，全局 `before_request` 校验 `X-API-Key`（公共路径 `/health` `/docs` `/metrics` 豁免）；env 未设置时关闭以兼容 dev/测试
- [x] **per-IP 限流**：自实现内存桶，env `TRADING_RL_PER_MIN`（默认 120/min，0 关闭），公共路径不计入配额
- [ ] **本地访问豁免**：127.0.0.1 / ::1 跳过校验（保留 Streamlit 调用）
- [ ] **更新 `backend/openapi.json`** 反映认证要求

**关键文件**：`backend/api.py`、`backend/openapi.json`
**验证**：未带 Key 的 POST 请求返回 401；超限请求返回 429

---

### P2-21 CI 升级 + 遗留测试清理

**问题**：`.github/workflows/ci.yml` 仅 py_compile + flake8，未运行完整 pytest；`tests/test_legacy_phase[3-5].py` 4 个遗留测试占总数 ~1%，价值低。

- [ ] **CI 增加 `pytest tests/ -x -q` 完整运行**（Linux + Python 3.10/3.11/3.12 矩阵）
- [ ] **集成 codecov**：上传覆盖率报告，目标 ≥ 75%
- [ ] **删除 `tests/test_legacy_phase[3-5].py`** 4 个遗留测试
- [ ] **新增 E2E 集成测试** `tests/test_e2e_morning_to_afternoon.py`：模拟完整交易日流程

**关键文件**：`.github/workflows/ci.yml`、`tests/test_legacy_phase*.py`、`tests/test_e2e_*.py`（新建）
**验证**：CI 全绿；codecov 报告生成；E2E 测试 < 60s 完成

---

### P2-22 文档与代码一致性修正

**问题**：
- `README.md:117` 与 `ARCHITECTURE.md:94` 引用 `make_a_stock_pipeline()`，**该函数不存在**（实为 `build_pipeline()`）
- `TODO.md`（旧）声明 841 测试，实际 934
- `ARCHITECTURE.md` 提"飞书推送"但 `alerting.py` 不支持（与 P2-17 关联）

- [x] **修正 `README.md`**：`make_a_stock_pipeline` → `build_pipeline`
- [x] **修正 `ARCHITECTURE.md`**：同上
- [x] **TODO.md 进度速查更新**：P0-1/2/3/4/5 + P1-7/8/9/10/11/12/13 全完成
- [ ] **建立文档检查脚本** `scripts/check_docs_consistency.py`（推迟）
- [ ] **CI 增加文档检查步骤**（推迟）

**关键文件**：`README.md`、`ARCHITECTURE.md`、`scripts/check_docs_consistency.py`（新建）、`.github/workflows/ci.yml`
**验证**：文档检查脚本通过；新 CI 步骤通过

---

## 验证清单（每个任务完成时检查）

- [ ] 关联测试通过（`pytest tests/<test_file>.py -x -q`）
- [ ] 全量测试不回退（`pytest tests/ -x -q`）
- [ ] 影响生产路径的变更：本地 `backend/main.py` 启动后人工烟测 1 个交易日
- [ ] 配置变更：同步更新 `config/trading.yaml.example` 与 `README.md`
- [ ] 涉及 schema 变更：附数据迁移脚本 + 回滚说明
- [ ] PR description 注明：解决的问题、关键文件、测试结果

---

## 不在范围内（明确排除）

| 能力 | 排除原因 |
|---|---|
| 毫秒级高频交易 | 需要 co-location 专用硬件，日线策略不需要 |
| 50+ 付费另类数据源 | 年费 100 万+，规模不到不经济 |
| T+0 做空 A 股 | 监管制度限制 |
| 期货套利（CTA） | 需要期货账户与保证金管理 |
| 机构级合规系统 | 需要牌照，个人/小型私募暂不适用 |
| PostgreSQL 迁移 | `portfolio.db` 当前 151KB，触发条件未满足（>10 万条 trade 或 >100MB） |
