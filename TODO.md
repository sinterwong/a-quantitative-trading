# TODO — A 股量化交易系统开发路线图

> 评估日期：2026-04-27  
> 当前状态：**~95 分**，系统完成三阶段专业化升级，829 个测试全部通过  
> 下一目标：实盘验证闭环 + 策略持续迭代 + 运营稳定性提升

---

## 已完成总览

| 阶段 | 核心内容 | 完成时间 | 得分贡献 |
|------|---------|---------|---------|
| Phase 1 | 回测引擎 Bug 修复、WFA、数据层加固、CI/CD | 2026-04-22 | 62 → 75 |
| Phase 2 | 多策略、多因子验证、动态权重、市场 Regime | 2026-04-23 | 75 → 85 |
| Phase 3 | BrokerBase、纸交易验证、TCA、异步执行 | 2026-04-23 | 85 → 90 |
| Phase A | Bug 修复精修 + 22 个多类别因子 | 2026-04-24 | 90 → 91 |
| Phase B | ML 价格预测、VWAP/TWAP 执行、Futu 纸交易 | 2026-04-25 | 91 → 93 |
| Phase C | MVO+BL 组合优化、NLP 情感因子、AlertManager | 2026-04-26 | 93 → 95 |

---

## Phase 4 — 实盘验证闭环（第 1-3 个月）

> **目标**：将系统从"功能完备的模拟系统"推进到"有实盘验证记录的量化平台"  
> **核心原则**：用真实市场数据验证每一个模块的假设，消灭"回测好看、实盘翻车"

### P4-A：Futu 纸交易运营

- [ ] **[P0] 部署 OpenD + 运行两周纸交易**
  - 前提：本机安装 Futu OpenD（`port 11111`，TrdEnv.SIMULATE）
  - 步骤：运行 `core/brokers/futu.py` connect() 验证连通；配置 `FutuPaperValidator`
  - 目标：信号一致率 ≥ 95%（`paper_trade_validator.py signal_match_target`）
  - 输出：每日 JSON 报告到 `outputs/paper_trade/`

- [ ] **[P0] 用真实数据训练 ML 模型**
  - 工具：`core/ml/price_predictor.py WalkForwardTrainer`
  - 数据：至少 500 交易日历史（AKShare 或 Futu 拉取）
  - 目标：OOS Sharpe > 0.15（walk-forward 252/63/21 窗口验证）
  - 保存：`ModelRegistry.save(model, '000001.SZ')`

- [ ] **[P1] 配置真实 AlertManager Webhook**
  - 企业微信 Webhook：`config/trading.yaml` → `alerting.wechat_webhook`
  - 钉钉 Webhook：`alerting.dingtalk_webhook`
  - 验证：`AlertManager().send_critical('测试')` 实际推送
  - 接入：`core/strategy_health.py` 触发点 → `alerting.send_critical()`
  - 接入：`core/risk_engine.py` 日亏损熔断 → `alerting.send_warning()`

- [ ] **[P1] 集成 AlertManager 到策略执行链**
  - `core/strategy_health.py HealthReport` → CRITICAL/WARNING 自动推送
  - `core/risk_engine.py PostTradeChecker` → 触发日亏损限制时立即告警
  - `core/daily_diff_reporter.py` → 每日收盘后发送 `send_daily_report()`
  - 验证：mock Webhook 下运行 1 个完整交易日无遗漏

### P4-B：数据管道稳定性

- [ ] **[P1] 基本面数据自动更新调度**
  - 文件：`core/fundamental_data.py`
  - 当前：手动调用 `fetch_*()`，无自动刷新
  - 升级：在 `backend/main.py Scheduler` 中注册季报更新任务（每季度末 + 财报发布日）
  - 缓存：Parquet 持久化，TTL 检测防止重复拉取

- [ ] **[P1] NLP 因子新闻质量验证**
  - 工具：`core/factors/nlp.py NewsSentimentFactor`
  - 统计：跑 1 个月历史数据，计算 `NewsSentiment` 因子 IC
  - 目标：IC 均值 > 0（相比市场噪声有正向预测力）
  - 输出：IC 时序图到 `outputs/factor_ic_nlp.png`

- [ ] **[P2] 行情数据 Websocket 推送订阅**
  - 文件：`core/data_layer.py`（新增 WebSocket 接入）
  - 依赖：Futu OpenD 行情推送 API（`quote_ctx.subscribe()`）
  - 升级：`AsyncStrategyRunner` 改为事件驱动（行情到达 → 立即触发 `run_once()`）
  - 当前轮询延迟：~300ms；目标：< 50ms
  - 条件：Futu OpenD 部署完成后实施

### P4-C：数据库与持久化

- [ ] **[P2] 迁移核心交易数据到 PostgreSQL**
  - 当前：`backend/services/portfolio.db`（SQLite）
  - 触发时机：trade 记录 > 10 万条 或 `portfolio.db` > 100MB
  - 迁移范围：positions / trades / signals / daily_meta 四张表
  - 工具：新建 `scripts/migrate_to_postgres.py`
  - 保留 SQLite 作为离线回退

- [ ] **[P3] 分钟级数据接入 TimescaleDB**
  - 条件：PostgreSQL 迁移完成后
  - 用途：存储 AKShare 1/5/15 分钟 K 线历史（当前仅内存缓存）
  - 压缩比：原始 Parquet 约 200MB/年/标的 → TimescaleDB 压缩后 ~30MB
  - 收益：支持分钟级因子批量回测，无需每次重新拉取

---

## Phase 5 — 策略深化与 Alpha 拓展（第 3-9 个月）

> **目标**：在已有 22 个因子基础上，持续验证和筛选高 IC 因子，引入新策略类型  
> **核心原则**：IC > 0.03 + IR > 0.5 才纳入实盘权重

### P5-A：因子 IC 系统验证

- [ ] **[P1] 对所有 22 个因子运行历史 IC 检验**
  - 工具：`core/research.py FactorICAnalyzer`
  - 数据：沪深 300 前 50 成分股，2020-2026 日线
  - 输出：`outputs/factor_ic_report_2026.json` + 月度 IC 热力图
  - 目标：筛出 IC > 0.02 且 IR > 0.3 的"有效因子"子集
  - 决策：有效因子纳入 `DynamicWeightPipeline`，无效因子降权至 0.05 以下

- [ ] **[P2] 因子衰减检测与自动权重调整**
  - 当前：`DynamicWeightPipeline` 每 21 天更新权重
  - 升级：检测单因子 IC 连续 60 天 < 0 时自动将权重降至 0（因子失效保护）
  - 恢复：IC 转正后逐步恢复（不超过 1/N 等权权重）
  - 输出：因子状态日志到 `outputs/factor_status.json`

- [ ] **[P2] 因子相关性去重**
  - 工具：`core/research.py StrategyCorrelationAnalyzer`（扩展为因子相关性版本）
  - 问题：`RSI` / `BollingerBands` / `MACD` 三者高度相关（同属价格动量）
  - 方案：相关系数 > 0.7 的因子对仅保留 IC 较高的一个
  - 输出：因子聚类树状图 `outputs/factor_cluster.png`

### P5-B：新策略类型

- [ ] **[P2] 行业轮动策略**
  - 思路：基于 `SectorMomentumFactor` 跨行业 ETF 排名，持有动量最强的前 3 个行业
  - 数据：AKShare 行业 ETF 日线（已在 `SectorMomentumFactor` 中接入）
  - 标的：沪深 300 行业 ETF（28 个申万一级行业 ETF）
  - 验证：WFA 回测（train=18m/test=6m），目标 OOS Sharpe > 0.4
  - 文件：`core/strategies/sector_rotation.py`（新建）

- [ ] **[P2] 均值回归配对交易**
  - 思路：同行业两只高相关股票，价差偏离均值 2σ 时反向做多/做空价差
  - 数据：`StrategyCorrelationAnalyzer` 筛选 corr > 0.85 的股票对
  - A 股限制：无日内做空，用 ETF + 个股组合代替（如 510050 + 个股）
  - 验证：历史 500 天回测，目标胜率 > 60%
  - 文件：`core/strategies/pairs_trading.py`（新建）

- [ ] **[P3] 基于 ML 的因子动态选择**
  - 思路：用 LightGBM 预测"未来 21 天哪些因子 IC 较高"，自适应调整权重
  - 特征：市场 Regime / 波动率水平 / 行业资金流向 / 宏观指标
  - 依赖：`core/ml/feature_store.py`（已有）+ 新增 Regime 特征
  - 文件：`core/ml/factor_selector.py`（新建）

### P5-C：另类数据接入

- [ ] **[P2] 融资融券完整数据流**
  - 当前：`MarginTradingFactor` 依赖 AKShare 单日接口，无连续时序
  - 升级：接入 `stock_margin_detail()` 构建日更新时序，存 Parquet
  - 目标：融资余额变化率 IC 验证 > 0.02

- [ ] **[P2] 股东变动（大股东增减持）因子**
  - 数据：AKShare `stock_hold_num_cninfo()` 季度股东人数变动
  - 因子：股东人数减少（筹码集中）→ 正向信号
  - 文件：`core/factors/fundamental.py` 新增 `ShareholderConcentrationFactor`

- [ ] **[P3] 宏观经济因子**
  - 指标：PMI / CPI / M2 同比增速 / 社融规模
  - 数据：AKShare `macro_china_pmi_monthly()` 等
  - 因子化：宏观因子作为 Regime 判断辅助，非直接选股信号
  - 文件：`core/factors/macro.py`（新建）

---

## Phase 6 — 多市场扩展（第 9-18 个月）

> **目标**：从 A 股扩展到港股、美股，实现跨市场对冲  
> **前提**：A 股实盘验证通过，系统运营稳定 ≥ 6 个月

### P6-A：港股接入

- [ ] **[P0] 完善 FutuBroker 港股功能**
  - 当前：`core/brokers/futu.py` 已实现基础接口，待真实 OpenD 验证
  - 扩展：支持 HK 市场（`TrdMarket.HK`），港元计价持仓
  - 特殊规则：港股无涨跌停、最小手数（lot_size 各股不同）
  - 验证：港股纸交易 2 周，信号一致率 ≥ 95%

- [ ] **[P1] 港股因子适配**
  - `HKDataSource`（已有 `core/hk_data_source.py`）→ 接入港股日线
  - 调整：无涨跌停过滤、加入港股成交量稀疏处理
  - 新增：恒生行业分类（`core/factors/technical.py SectorMomentumFactor` 港股版）

### P6-B：美股接入

- [ ] **[P2] 完善 IBKRBroker 适配器**
  - 文件：`core/brokers/ibkr.py`（当前为 stub）
  - 依赖：`ib_insync` 库（`pip install ib_insync`）
  - 实现：`connect()` / `get_account()` / `submit_order()` / `cancel_order()`
  - 验证：IBKR Paper Trading 账户两周运行

- [ ] **[P2] 完善 TigerBroker 适配器**
  - 文件：`core/brokers/tiger.py`（当前为 stub）
  - 依赖：`tigeropen` 库
  - 适用：美股 + 港股双市场通道

### P6-C：跨市场策略

- [ ] **[P3] A 股 + 港股 AH 价差套利**
  - 思路：同一公司 A/H 股价差过大时，买入折价端
  - 工具：`PortfolioOptimizer.black_litterman()` 融入 AH 溢价观点
  - 风险：汇率风险、流动性差异

- [ ] **[P3] A 股 + 美股领先滞后策略**
  - 工具：`core/external_signal.py SP500GrangerAnalyzer`（已有）
  - 实盘：美股收盘后判断次日 A 股开盘偏向，提前布局

---

## Backlog（无固定时间表）

### 工程基础设施

- [ ] **Prometheus + Grafana 监控看板**
  - 指标：当日净值 / 持仓数量 / 信号延迟 / API 响应时间
  - 文件：`core/metrics.py`（新建，暴露 `/metrics` 端点）

- [ ] **合规审计日志**
  - 每笔交易记录：时间戳、信号来源、因子值、风控检查结果
  - 格式：结构化 JSON，不可篡改（append-only）
  - 用途：事后复盘、监管合规

- [ ] **回测报告 PDF 导出**
  - 工具：`reportlab` 或 `weasyprint`
  - 内容：净值曲线、Sharpe/MaxDD/Calmar、WFA 热力图、因子 IC 汇总

### 策略研究

- [ ] **强化学习策略框架**
  - 环境：`gymnasium` 标准化 RL 环境（状态=因子值，动作=仓位比例）
  - 算法：PPO（近端策略优化），适合连续动作空间
  - 风险：RL 样本效率低，需大量历史数据

- [ ] **期权对冲组合**
  - 条件：需要期权交易权限（50ETF 期权）
  - 思路：持多头 + 买入认沽期权作为尾部风险对冲
  - 依赖：期权定价模型（Black-Scholes / local vol）

- [ ] **高频因子（1分钟级）**
  - 当前：所有因子基于日线
  - 目标：接入分钟级订单流数据，计算实时 VWAP 偏离因子
  - 前提：TimescaleDB 分钟数据存储完成

### 运营

- [ ] **自动化每日运营报告**
  - 内容：因子 IC 日报 / 策略健康度 / AlertManager 发送摘要
  - 触发：`backend/main.py Scheduler` 每日 16:00
  - 输出：企业微信 / 钉钉 Markdown 格式

- [ ] **参数自动优化（贝叶斯调参）**
  - 工具：`optuna`（贝叶斯超参数优化）
  - 目标参数：RSI 周期、MACD 快/慢/信号线、ATR 倍数
  - 约束：在 Walk-Forward 框架内优化，防止过拟合

---

## 评分追踪

| 评估时间 | 综合得分 | Alpha 因子 | 执行层 | 组合优化 | 风险管理 | ML 集成 | 生产就绪 |
|---------|---------|-----------|-------|---------|---------|---------|---------|
| 2026-04-22（初始）| **62** | 18 | 48 | 32 | 65 | 5 | 12 |
| 2026-04-23（Phase 1-3）| **90** | 45 | 70 | 55 | 78 | 15 | 65 |
| 2026-04-27（Phase A-C）| **~95** | 72 | 82 | 85 | 82 | 60 | 68 |
| Phase 4 完成后（预计）| **~97** | 72 | 88 | 85 | 85 | 75 | 85 |
| Phase 5 完成后（预计）| **~98** | 88 | 88 | 90 | 88 | 85 | 85 |
| Phase 6 完成后（预计）| **~99** | 92 | 92 | 92 | 90 | 88 | 92 |

---

## 不在范围内的能力（明确排除）

| 能力 | 排除原因 |
|------|---------|
| 毫秒级高频交易 | 需要 co-location 专用硬件，日线策略不需要 |
| 50+ 付费另类数据源 | 年费 100 万+，规模不到不经济 |
| T+0 做空 A 股 | 监管制度限制 |
| 期货套利（CTA） | 需要期货账户与保证金管理，超出当前范围 |
| 机构级合规系统 | 需要牌照，个人/小型私募暂不适用 |
