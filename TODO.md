# TODO — A 股量化交易系统开发路线图

> 评估日期：2026-04-29  
> 当前状态：**~97 分**，系统完成六阶段专业化升级，841 个测试通过（1 个因缺少 `anthropic` 依赖报错）  
> 下一目标：实盘验证闭环（Futu OpenD 部署 + 真实 Webhook 配置）

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
| Phase D | AlertManager 执行链集成、因子衰减/相关性去重、行业轮动、配对交易、宏观因子、股东因子、合规审计、贝叶斯调参 | 2026-04-29 | 95 → 97 |
| Phase E | ExitEngine P0-P9 退出体系、pipeline_factory 生产流水线、主流程全模块接入、早报修复 | 2026-04-29 | 97（巩固）|

### 新增完成（Phase D/E，2026-04-29）

| 模块 | 文件 | 说明 |
|------|------|------|
| ExitEngine | `core/exit_engine.py` | P0-P9 优先级退出体系（EMERGENCY→TIME_STOP），统一卖出信号引擎 |
| PipelineFactory | `core/pipeline_factory.py` | 生产用因子流水线工厂（技术层/基本面层/宏观层） |
| 主流程集成 | `backend/api.py`, `backend/main.py` | 行业轮动/配对交易 API 端点，Scheduler 自动触发 |
| 早报修复 | `backend/morning_report.py` | 5 处设计缺陷修复，新增市场环境/开盘订单区块 |
| 监控闭环 | `backend/services/intraday_monitor.py` | 价格刷新链路修复，浮盈实时更新 |

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

- [x] **[P1] 集成 AlertManager 到策略执行链** *(2026-04-29 完成)*
  - `core/strategy_health.py HealthReport` → CRITICAL/WARNING 自动推送
  - `core/risk_engine.py PostTradeChecker` → 触发日亏损限制时立即告警
  - `core/daily_diff_reporter.py` → 每日收盘后发送 `send_daily_report()`

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

- [x] **[P2] 因子衰减检测与自动权重调整** *(2026-04-29 完成)*
  - `DynamicWeightPipeline` 新增 `decay_window`/`recovery_rate` 参数
  - 单因子 IC 连续 60 天 < 0 时自动降权至 0，IC 转正后逐步恢复

- [x] **[P2] 因子相关性去重** *(2026-04-29 完成)*
  - `core/research.py FactorCorrelationAnalyzer`：Spearman 相关 + Union-Find 聚类
  - 相关系数 > 0.7 的因子对仅保留 IC 较高的一个

### P5-B：新策略类型

- [x] **[P2] 行业轮动策略** *(2026-04-29 完成)*
  - 文件：`core/strategies/sector_rotation.py`（已实现）
  - 申万 ETF 动量排名，每周一 Scheduler 自动触发（`POST /analysis/sector_rotation`）
  - 验证：WFA 回测（train=18m/test=6m），目标 OOS Sharpe > 0.4

- [x] **[P2] 均值回归配对交易** *(2026-04-29 完成)*
  - 文件：`core/strategies/pairs_trading.py`（已实现）
  - 纯 numpy Engle-Granger 协整检验，`POST /analysis/pairs_trading` 接口
  - A 股限制处理：ETF + 个股组合代替纯做空

- [x] **[P3] 基于 ML 的因子动态选择** *(2026-04-30 完成)*
  - `core/ml/factor_selector.py`：`FactorICLabeler` + `FactorSelectorModel` + `WalkForwardFactorSelector` + `FactorSelector`
  - LightGBM 预测各因子在未来 21 天 IC 是否高于阈值，输出 `factor_weights` 字典
  - Walk-Forward 框架（252/63/21）防过拟合，LightGBM 缺失时等权降级

### P5-C：另类数据接入

- [x] **[P2] 融资融券完整数据流** *(2026-04-30 完成)*
  - `core/factors/sentiment.py MarginDataStore`：自动拉取 + Parquet 日更新时序（TTL=24h）
  - `MarginTradingFactor` / `ShortInterestFactor`：symbol 非空时自动调用 MarginDataStore
  - 向后兼容：显式传入 `sentiment_data` 时优先使用，AKShare 失败时降级全零

- [x] **[P2] 股东变动（大股东增减持）因子** *(2026-04-29 完成)*
  - `core/factors/fundamental.py ShareholderConcentrationFactor`（已实现）
  - AKShare `stock_hold_num_cninfo()` 季度股东人数变动，筹码集中 → 正向信号

- [x] **[P3] 宏观经济因子** *(2026-04-29 完成)*
  - `core/factors/macro.py`（已实现）：PMI / M2Growth / CreditImpulse 三类因子
  - `core/data_layer.py get_macro_data()` TTL 24h 缓存
  - `core/pipeline_factory.py` 宏观层已接入生产流水线（无数据时自动降级）

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

- [x] **Prometheus + Grafana 监控看板** *(2026-04-30 完成)*
  - `core/metrics.py MetricsRegistry`：净值/盈亏/持仓/现金/信号/订单延迟/API 请求/因子 IC
  - `backend/api.py GET /metrics`：Prometheus text 格式，Grafana 可直接接入
  - prometheus_client 缺失时静默降级，不影响主业务

- [x] **合规审计日志** *(2026-04-29 完成)*
  - `core/audit_log.py`（已实现）：append-only JSONL + SHA-256 篡改检测
  - 每笔交易记录：时间戳、信号来源、因子值、风控检查结果

- [x] **回测报告 PDF 导出** *(2026-04-30 完成)*
  - `core/report_exporter.py BacktestReportExporter`：封面 + 净值曲线 + 回撤图 + 绩效表 + 交易统计
  - 可选：WFA 结果表 + 因子 IC 汇总（参数传入）
  - 工具：`reportlab` + `matplotlib`，无需外部服务

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

- [x] **自动化每日运营报告** *(2026-04-30 完成)*
  - `core/daily_ops_reporter.py DailyOpsReporter`：汇聚 P&L / 策略健康 / 告警摘要 / 因子 IC
  - Scheduler 每日 16:00 自动触发（`_trigger_daily_ops_report()`）
  - JSON 输出到 `outputs/daily_ops/ops_{date}.json` + AlertManager 推送

- [x] **参数自动优化（贝叶斯调参）** *(2026-04-29 完成)*
  - `scripts/bayesian_optimize.py`（已实现）：optuna + Walk-Forward 框架
  - `scripts/walkforward_job.py --bayesian --n-trials N` 参数接入
  - 目标参数：RSI 周期、MACD 快/慢/信号线、ATR 倍数

---

## 评分追踪

| 评估时间 | 综合得分 | Alpha 因子 | 执行层 | 组合优化 | 风险管理 | ML 集成 | 生产就绪 |
|---------|---------|-----------|-------|---------|---------|---------|---------|
| 2026-04-22（初始）| **62** | 18 | 48 | 32 | 65 | 5 | 12 |
| 2026-04-23（Phase 1-3）| **90** | 45 | 70 | 55 | 78 | 15 | 65 |
| 2026-04-27（Phase A-C）| **~95** | 72 | 82 | 85 | 82 | 60 | 68 |
| 2026-04-29（Phase D-E）| **~97** | 85 | 88 | 88 | 85 | 65 | 72 |
| Phase 4 完成后（预计）| **~98** | 85 | 92 | 88 | 88 | 75 | 88 |
| Phase 5 完成后（预计）| **~98** | 88 | 88 | 90 | 88 | 85 | 85 |
| Phase 6 完成后（预计）| **~99** | 92 | 92 | 92 | 90 | 88 | 92 |

---

---

## Phase 7 — 港股打新分析系统 IPO Stars

> **定位**：单次分析报告工具，不进入仓位/风控/操盘体系。  \
> **目标**：聚合多源数据，交叉验证，给出精准的**限价单建议成交价**（暗盘/首日挂单价）。  \
> **触发方式**：港交所有新招股时按需生成，或手动指定股票代码触发。  \
> **输出**：一份结构化分析报告 → 飞书推送。操盘决策由 Sir 自行决定。

---

### 资深 IPO 分析师框架（系统设计核心逻辑）

**一个资深 IPO 分析师判断"该股暗盘/首日该挂什么价"的核心思维链：**

```
①  同类可比 IPO 定价锚点
    └─▶  近 3~6 个月同行业新股，首日收盘涨幅分布（p25/p50/p75）
    └─▶  当前打新情绪：火热/一般/冰冷

②  发行条款性价比
    └─▶  PS(市销率) vs 行业均值折让/溢价多少？
    └─▶  募资规模：5~15 亿最易被炒作，< 3亿 或 > 30亿 冷门
    └─▶  定价区间宽窄（宽 = 发行人没信心，窄 = 信心足）

③  机构持仓结构（基石/锚定）
    └─▶  是否有顶级机构站台：高瓴/红杉/淡马锡/中金资本
    └─▶  禁售期安排：6个月/12个月
    └─▶  公开发售占比：散户筹码多少

④  市场窗口与情绪
    └─▶  恒生科技指数近期表现（科技情绪温度计）
    └─▶  同一主题近期上市新股表现（板块动量）
    └─▶  打新资金面：近期新股认购倍数（中位倍数 → 资金供需）
    └─▶  VIX：市场恐慌程度

⑤  故事与预期差
    └─▶  公司讲的故事够不够大（"中国的 XXX"类比有效性）
    └─▶  已知大鳞鱼在什么价格入场（pre-IPO 成本 → 锚定效应）
    └─▶  投行是否在路演时主动引导价格预期

⑥  挂单策略（最终输出）
    └─▶  暗盘开盘价区间预测
    └─▶  首日合理成交价范围
    └─▶  建议限价单挂单价（保守/中性/进取三档）
    └─▶  止损参考价（破发多少考虑认输离场）
```

---

### 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    IPO Stars 单次分析报告                         │
│                                                                  │
│  触发 ──▶ IPOScanner（扫描到新招股）OR Sir 手动指定股票代码        │
│                  │                                               │
│                  ▼                                               │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               IPODataSourceMulti                         │    │
│  │                                                          │    │
│  │  多源并行抓取 ──▶ 交叉验证 ──▶ 数据质量评分               │    │
│  │                                                          │    │
│  │  东方财富IPO数据中心 ──▶ 港交所披露易                      │    │
│  │  新浪财经              ──▶ 财联社/36氪                   │    │
│  │  彭博/路透             ──▶ 承销商研报                    │    │
│  │  辉立暗盘               ──▶ 富途/老虎暗盘                 │    │
│  │  行业协会数据           ──▶ 竞品上市公开数据              │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                    │
│                             ▼                                    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │            IPOAnalystEngine（资深分析师框架）              │    │
│  │                                                          │    │
│  │  ① 可比 IPO 定价锚点（同类新股历史定价/涨幅分布）          │    │
│  │  ② 发行条款性价比（PS折让/募资规模/定价区间）             │    │
│  │  ③ 机构持仓结构（基石/锚定/禁售期）                      │    │
│  │  ④ 市场窗口与情绪（恒生科技/板块动量/认购倍数/VIX）       │    │
│  │  ⑤ 故事与预期差（商业对标/大鳞鱼成本/投行预期引导）       │    │
│  │  ⑥ 挂单策略（暗盘开盘预测/首日合理价/三档限价建议）       │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                    │
│                             ▼                                    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              IPOAnalysisReport                            │    │
│  │                                                          │    │
│  │  综合评级 | 定价锚点 | 三档限价单建议 | 风险提示          │    │
│  │  数据可信度评分（哪些字段经过多源交叉验证）               │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                    │
│                             ▼                                    │
│                 IPOReportRenderer ──▶ Feishu 推送                │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

### P7-0：系统核心（无操盘）

- [ ] **[P0] 删除原 IPOBook / IPORiskEngine 设计**
  - 不进入 PositionBook，独立于现有风控体系
  - 系统定位：纯分析报告工具，操盘决策归 Sir

- [ ] **[P0] 手动触发 + 扫描触发双入口**
  - `scheduler/ipo_scanner.py`：每日 09:00 扫描港交所，发现新招股自动生成报告
  - `POST /ipo/analyze?stock_code=01810`：Sir 手动指定股票代码触发分析
  - 输出：一次性分析报告，不留仓

- [ ] **[P0] EventBus 简化扩展**
  - 只需 `IPOAnalysedEvent` 一个事件（分析完成后 → 推送飞书）
  - 现有 `MarketEvent` / `SignalEvent` / `AlertEvent` 完全不变

- [ ] **[P0] AlertManager 复用**
  - 复用现有 `AlertManager`，IPO 渲染器输出 Markdown → `send_info()` 推送
  - 频率限制、渠道配置完全复用

---

### P7-1：多源数据层（核心优先，重中之重）

> **目标**：每个字段至少 2~3 个数据源交叉验证，确保信息准确度高。

#### 数据源 A：东方财富 IPO 数据中心（P0）

- 招股日程（申购时间、上市日期）
- 发行价区间、募资规模
- 保荐人、承销商
- 行业分类（机器人/AI/医疗/EV 等主题标签）
- 认购倍数（甲组/乙组分开）
- 数据质量：发布及时，覆盖全面，主力数据源

#### 数据源 B：港交所披露易 HKEXnews（P0）

- 聆讯资料集（状态跟踪）
- 招股书全文（提取基石投资者、股东结构、pre-IPO 成本）
- 承销协议关键条款
- 数据质量：最权威，但解析难度大，需处理 PDF

#### 数据源 C：财联社/36氪/新浪财经（P1）

- IPO 舆情新闻（路演反馈、机构认购意向）
- 投行研究报告摘要（发行区间调整信息）
- 关键新闻催化剂（政策利好/行业热点）
- 数据质量：补充市场情绪和"故事"层面信息

#### 数据源 D：辉立/富途/老虎暗盘行情（P1）

- 暗盘实时行情（上市前一交易日 16:15-18:30）
- 暗盘成交价、成交量、买卖价差
- **这是最直接的挂单参考**：暗盘成交价 = 次日上市开盘价的重要锚点
- 数据质量：直接决定限价单精准度

#### 数据源 E：行业协会/招股书补充数据（P1）

- 同行业上市公司公开财报（用于 PS/PE 横向对比）
- 行业市场规模与增速数据
- 竞争格局（龙头公司对比）

#### 数据源 F：承销商研报与投资逻辑（P2）

- 各承销商（海通/国君/中金/摩根士丹利）IPO 投资价值报告
- 目标价区间（承销商给的目标价是重要锚点）
- 数据质量：信息密度高，但发布时机不稳定

#### 数据源 G：历史新股数据库 IPORecordStore（P0）

- 2020~2026 所有港股 IPO 历史数据
- 首日收盘涨幅、暗盘收盘涨幅
- 按行业/募资规模/保荐人分类统计
- **用于可比 IPO 定价锚点计算**（最核心的定价参考）

#### 数据源 H：市场情绪数据（复用现有 P0）

- 恒生科技指数（科技情绪温度计）→ 直接 import 现有
- 恒生指数近期表现（判断大盘趋势）
- VIX 恐慌指数
- 南向资金净流入
- 近 30 天港股打新胜率（滚动基准）

---

### P7-2：资深分析师分析引擎（核心输出）

#### 模块 ①：同类可比 IPO 定价锚点引擎

```python
class ComparableIPOEngine:
    """
    核心定价逻辑：找到真正的可比 IPO，锚定定价区间。

    步骤：
    1. 行业匹配：同行业（证监会行业分类二级）
    2. 规模匹配：募资额 ±50% 范围内
    3. 时间匹配：近 3~6 个月优先（市场情绪相近）
    4. 加权计算：近期 + 同行业 + 同规模 综合得出锚点涨幅

    输出：
      - p25 / p50 / p75 首日涨幅预测
      - 可比公司列表（含各自首日表现）
      - 信心评分（样本量越多越高）
    """
```

#### 模块 ②：机构持仓结构分析

```python
class InvestorStructureAnalyzer:
    """
    基石/锚定投资者分析。

    关键信号：
      - 高瓴/红杉/淡马锡/中金资本 → 强背书，上涨概率↑
      - pre-IPO 成本 vs 发行价：折让大 → 上市后有抛压
      - 禁售期 12 个月 → 上市初期流通盘少，易炒作
      - 公开发售占比：散户筹码越多，上市初期博弈越激烈
    """
```

#### 模块 ③：发行条款性价比评分

```python
class TermsValuationScorer:
    """
    发行条款综合评分。

    维度：
      - PS(市销率) vs 行业均值：折让越多性价比越高
      - 募资规模：5~15 亿最易被炒作；< 3亿 冷门；> 30亿 机构主导
      - 定价区间宽度：窄（< 10%）= 发行人有定价权，信心足
      - 乙组认购门槛：门槛低 → 散户参与多 → 炒作概率高
    """
```

#### 模块 ④：市场情绪窗口评估

```python
class MarketWindowEvaluator:
    """
    市场窗口综合评分（直接复用 CompositeMarketDataSource）。

    维度：
      - 恒生科技指数近 5 日表现：> 3% → 科技 IPO 情绪火热
      - VIX：< 15 → 风险偏好高；> 25 → 谨慎
      - 近 30 天港股打新胜率：> 60% → 好窗口；< 40% → 差窗口
      - 同主题近期新股表现：板块内有没有涨停新股 → 带动效应
      - 资金面：认购倍数 > 10x → 供需失衡，上市后上涨概率高
    """
```

#### 模块 ⑤：挂单策略生成器（最终输出）

```python
@dataclass
class IPOAnalysisReport:
    """单只新股完整分析报告。"""

    # 基本信息
    stock_code: str
    name_cn: str
    name_en: str
    listing_date: date
    issue_price_range: Tuple[float, float]  # 港元
    mid_price: float

    # 综合评级
    overall_rating: Literal['BUY', 'NEUTRAL', 'SKIP']
    confidence: float  # 0~1，数据完整度和交叉验证质量

    # 定价锚点（来自 ComparableIPOEngine）
    comparable_ipos: List[ComparableIPO]  # 可比公司列表
    predicted_first_day_return_p50: float  # 首日预测涨幅（中性，p50）
    predicted_first_day_return_p75: float  # 乐观情况（p75）
    predicted_first_day_return_p25: float  # 保守情况（p25）

    # 机构结构信号
    cornerstone_signals: List[str]  # ['高瓴资本', '6个月禁售']
    retail_float_ratio: float  # 公开发售占比

    # 发行条款评分
    terms_score: float  # 0~1
    ps_discount_vs_sector: float  # PS折让%
    optimal_scale: bool  # 是否在 5~15 亿募资最优区间

    # 市场情绪
    market_sentiment_score: float  # 0~1
    hstech_recent_change: float  # 恒生科技近期表现
    recent_ipo_win_rate: float  # 近30天打新胜率
    theme_momentum: float  # 主题动量

    # ★ 核心输出：三档限价单建议
    dark_pool_recommendation: LimitOrderRec
        # target_price: 暗盘合理挂单价
        # logic: 为什么是这个价（锚定可比 IPO p50）
        # stop_price: 暗盘止损参考价

    first_day_recommendation: LimitOrderRec
        # target_price: 首日合理成交价区间
        # logic: 为什么是这个价
        # stop_price: 首日止损参考价

    risk_factors: List[str]  # 主要风险提示
    key_positive_signals: List[str]  # 主要利好信号
    data_quality_score: Dict[str, float]  # 各字段数据质量评分


@dataclass
class LimitOrderRec:
    """限价单建议。"""
    conservative_price: float   # 保守档（破发概率低）
    neutral_price: float         # 中性档（推荐参考）
    aggressive_price: float      # 进取档（高胜率时可追）
    logic: str                   # 定价逻辑说明
    anchor_comparable: str       # 锚定的可比公司
    stop_price: float            # 止损参考价（发行价 × (1 - 止损%))
    stop_loss_pct: float         # 止损比例
```

---

### P7-3：交叉验证与数据质量评分

> **这是区别于其他 IPO 工具的关键**。系统不只是聚合数据，还要量化每个字段的可信度。

```python
class DataCrossValidator:
    """
    多源数据交叉验证。

    规则：
      - 发行价区间：东方财富 vs HKEX，两者一致才高可信
      - 募资规模：东方财富 vs 港交所招股书，误差 > 10% 触发警告
      - 行业分类：东方财富 vs 港交所，不一致时取更细粒度
      - 保荐人：东方财富 vs 港交所，不一致触发人工复核
      - 基石投资者：港交所招股书 vs 新闻报道，不一致时以招股书为准

    输出：
      每个字段一个 confidence_score (0~1)
      整体报告 confidence = 加权平均
    """
```

---

### P7-4：定时任务 + 手动触发

- [ ] **[P0] 每日 09:00 新股扫描**
  - 对比 `IPORecordStore`，已有分析的跳过
  - 新股 → 触发全量分析 → 推送飞书报告

- [ ] **[P0] 手动分析端点**
  - `POST /ipo/analyze?stock_code=01810`
  - Sir 随时指定股票代码，触发一次完整分析

- [ ] **[P1] 暗盘日自动追加**
  - 上市前一交易日 16:00 检测到暗盘行情后，追加暗盘快报到原报告
  - 暗盘实际成交价 vs 系统预测对比 → 持续校准模型

---

### P7-5：飞书报告渲染器

- [ ] **[P0] 完整分析报告渲染**
  - Markdown 格式，适配飞书
  - 结构：综合评级 → 核心逻辑 → 三档限价单建议 → 风险提示 → 数据可信度

- [ ] **[P1] 暗盘快报渲染**
  - 暗盘实际涨幅 vs 预测涨幅对比
  - 是否需要调整首日挂单价建议

- [ ] **[P1] 分析复盘**
  - 首日结果出来后，自动追加"预测 vs 实际"复盘到原报告
  - 用于持续校准模型准确度

---

### 新增文件清单（Phase 7）

| 文件 | 优先级 | 说明 |
|------|--------|------|
| `core/ipo_data_source.py` | P0 | 多源数据获取（东方财富/HKEX/新闻/暗盘/历史库） |
| `core/ipo_cross_validator.py` | P0 | 多源交叉验证 + 数据质量评分 |
| `core/ipo_analyst_engine.py` | P0 | 资深分析师分析引擎（5个分析模块） |
| `core/ipo_report.py` | P0 | 分析报告数据结构（核心输出） |
| `core/ipo_store.py` | P0 | 历史新股数据库（Parquet，2020~2026） |
| `scheduler/ipo_scanner.py` | P0 | 每日 09:00 新股扫描 |
| `reports/ipo_renderer.py` | P0 | 飞书 Markdown 渲染器 |
| `backend/api.py` 扩展 | P0 | 新增 `POST /ipo/analyze` 端点 |
| `core/event_bus.py` 扩展 | P0 | 新增 `IPOAnalysedEvent` |

---

### 与现有系统集成方式

| 现有组件 | 集成方式 |
|---------|---------|
| `CompositeMarketDataSource` | 直接 import，市场情绪数据 0 成本 |
| `AlertManager` | IPO 报告 → `send_info()` 推送飞书 |
| `DataLayer` | Parquet 存储 `data/ipo/` 平行目录 |
| `Backend API` | 新增 `/ipo/analyze` 端点 |
| EventBus | 仅新增 `IPOAnalysedEvent`，不改现有任何事件 |
| PositionBook / RiskEngine | **完全不接入**，纯分析工具定位 |

---

### 输出示例（Sir 想看到的东西）

```
## 🎯 小米集团-W（01810）IPO 分析报告
**综合评级：BUY** | 置信度：82% | 生成时间：2026-05-04 19:00

---

### 📊 核心结论

| 指标 | 值 |
|------|-----|
| 发行价区间 | 16.6~18.2 港元 |
| 中间价 | 17.4 港元 |
| 预测首日涨幅（p50） | +18% |
| 预测首日涨幅（p75） | +35% |
| 预测首日涨幅（p25） | -3% |

---

### 🎯 三档限价单建议

#### 暗盘挂单（上市前一交易日）
| 档位 | 建议价格 | 逻辑 |
|------|---------|------|
| 保守 | 17.8 港元 | 锚定同类 p25，略高于发行价 |
| **中性（推荐）** | **18.5 港元** | **锚定同类 p50，发行价 +6%** |
| 进取 | 19.5 港元 | 锚定同类 p75，高热度时追入 |

#### 首日挂单（上市当日）
| 档位 | 建议价格 | 逻辑 |
|------|---------|------|
| 保守 | 18.2 港元 | 发行价 +5%，保守止损线 |
| **中性（推荐）** | **19.0 港元** | **暗盘收盘锚点，首日合理区间下沿** |
| 进取 | 21.0 港元 | 主题火热时目标，参考同类 p75 |

---

### 📈 关键依据

**① 可比 IPO 锚点**
- 近 3 个月机器人/AI 行业新股 4 只，平均首日涨幅 +22%
- 最可比：某机器人（02063）：发行价 15元，首日收盘 +28%
- 锚定定价：18.5 港元（+20% 溢价中性估计）

**② 机构持仓信号**
- ✅ 高瓴资本 锚定认购（最强背书）
- ✅ 6 个月禁售期（初期流通盘仅 15%）
- ✅ 公开发售占比 12%（散户筹码少，易拉升）

**③ 发行条款**
- PS 折让：行业均值 PS 8x，该股 5.2x，折让 35% → 性价比高
- 募资规模：12 亿港元 → 落入最优炒作区间
- 定价区间宽度：9% → 合理，发行人有定价信心

**④ 市场情绪**
- 恒生科技近 5 日：+4.2% → 科技 IPO 情绪火热 ✅
- 近 30 天港股打新胜率：72% → 好窗口 ✅
- 同主题近期新股：无直接竞品上市，带动效应强 ✅

**⑤ 风险提示**
- ⚠️ pre-IPO 成本 14.5 港元（约 17% 折让），大鳞鱼或有抛压
- ⚠️ 保荐人历史胜率 58%（中金平均），无显著优势
- ⚠️ 机器人行业商业化进度存疑（PE 无法估算）

---

### 📋 数据可信度

| 字段 | 来源数 | 可信度 |
|------|--------|--------|
| 发行价区间 | 东方财富 ✅ + 港交所 ✅ | 高（2源一致） |
| 基石投资者 | 港交所 ✅ + 新闻 ✅ | 高（2源一致） |
| 行业分类 | 东方财富 ✅ + 港交所 ✅ | 高（2源一致） |
| 募资规模 | 东方财富 ✅ + 港交所 ⚠️ | 中（误差 8%） |
| 暗盘预测 | 统计模型 ⚠️ | 中（样本量有限） |

---

> 由 IPO Stars 量化分析系统自动生成 | 仅供参考，不构成投资建议
```

---

### Sir 确认的 3 个问题更新答案

1. **数据源优先级**：东方财富 + 港交所 + 历史库作为 P0 三大核心源（覆盖招股信息 + 权威验证 + 定价锚点），其余源作为 P1/P2 扩展补充。
2. **仓位/操盘**：完全不需要，系统定位为纯分析报告工具。
3. **历史回测**：P2 优先级，打新周期性明显，Sir 判断准确——系统价值在于交叉验证信息，而非预测模型本身。
