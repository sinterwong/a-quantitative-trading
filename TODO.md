# TODO — 专业实盘级量化系统开发计划

> 评估日期：2026-04-22  
> 目标：从"可用的个人模拟系统（62分）"升级为"专业实盘级系统（90分）"  
> 策略：三阶段推进，每阶段可独立交付，优先解决数据+回测严谨性，再扩展策略，最后接实盘

---

## Phase 1 — 夯实地基：回测严谨性 + 数据质量（1~2个月）

> **目标分值**：62 → 75 分  
> **核心原则**：先把现有 1 个策略的回测做到 "无懈可击"，再谈扩张

### P1-A：修复回测引擎中的关键 Bug（最高优先）

- [ ] **[P0] 修复收盘价执行偏差（Look-ahead bias）**
  - 文件：`core/backtest_engine.py`，`_on_bar()` / `_generate_signals()` / `_process_signal()`
  - 问题：信号用当根 K 线的 `close` 价格生成 (`hist = df.loc[:dt]` 含当前 bar)，同时按该 close 价成交，等于偷用了收盘价下单
  - 修复：信号用 `df.loc[:dt-1bar]` 计算（排除当前 bar），成交价用 **下一根 bar 的 open**（或加 slippage）
  - 预期收益：消除 2~5% 的虚假 Alpha，回测更真实

- [ ] **[P0] 修复 holding_secs 计数逻辑**
  - 文件：`core/backtest_engine.py` L247 `pos.holding_secs += 1`
  - 问题：日线每 bar 加 1 秒，实际应加 86400（1天）
  - 修复：自动感知 bar 频率（daily/hourly/minute）并正确换算

- [ ] **[P0] 补齐 A 股卖出印花税**
  - 文件：`core/backtest_engine.py`，`_execute_sell()`
  - 问题：只扣佣金，未扣卖出印花税（当前 0.1%）
  - 修复：卖出成交时额外扣 `stamp_tax = value * 0.001`，买入不扣

- [ ] **[P1] 修复 Kelly 仓位硬编码**
  - 文件：`core/backtest_engine.py` L326-329
  - 问题：win_rate=0.55 / avg_win=0.02 / avg_loss=0.01 全部硬编码，非从历史推断
  - 修复：从前 N 笔历史交易动态计算 win_rate / avg_win / avg_loss，回退到等权时用 `max_position_pct`

- [ ] **[P1] 添加复权与停牌处理**
  - 文件：`core/data_layer.py`，`core/backtest_engine.py`
  - 问题：日线数据未验证是否已前复权；停牌日 bar 缺失但代码仍循环
  - 修复：加载时校验 qfq 标签；停牌日跳过开仓，持仓价值用停牌前收盘维持

### P1-B：Walk-Forward 多窗口统计验证

- [ ] **[P1] 增加 WFA 窗口数量至 ≥ 5 个**
  - 文件：`scripts/quant/walkforward.py`，`backend/services/walkforward_persistence.py`
  - 问题：当前仅 1 个有效窗口（2018-2019），Sharpe=0.467 统计意义弱
  - 方案：训练窗口 18 个月 + 测试窗口 6 个月 + 步进 6 个月，2013-2026 可产生 ≥ 10 个窗口
  - 输出：各窗口 OOS Sharpe 分布，检验 Sharpe > 0 的比例

- [ ] **[P1] 添加参数稳健性检验（Sensitivity Analysis）**
  - 验证 RSI(25/65) 在相邻参数 ±5 区间的 Sharpe 变化
  - 若参数敏感度高（峰值明显），需重新审视是否过拟合
  - 输出：参数热力图写入 `outputs/sensitivity_heatmap.png`

- [ ] **[P2] 扩展回测标的：沪深 300 成分股中选 10 支**
  - 当前仅在 HS300 ETF（510300）单一标的验证
  - 选取流动性前 10 的成分股分别回测，检验策略泛化能力
  - 合格标准：≥ 7/10 标的 OOS Sharpe > 0

### P1-C：数据层加固

- [ ] **[P1] 增加分钟 K 线回测支持**
  - 文件：`core/data_layer.py`，`core/backtest_engine.py`
  - 方案：通过 AKShare 获取分钟 K 线（免费，限 1 年历史）；BacktestEngine 自动区分 daily/minute 频率
  - 用途：在分钟级别验证信号真实触发时间，减少日线收盘偏差

- [ ] **[P2] 日线历史数据本地缓存（SQLite/Parquet）**
  - 问题：每次回测重新从腾讯 API 抓日线，耗时且有频率限制
  - 方案：首次下载后写入本地 Parquet 文件 (`data/bars/{symbol}.parquet`)，增量更新
  - 好处：回测速度提升 10x，离线可用

- [ ] **[P2] 数据质量检验模块**
  - 检测缺口（跳空日）、异常涨跌（±20% 以上）、成交量为 0 日
  - 异常 bar 自动标记或剔除，防止脏数据污染回测

### P1-D：工程质量

- [ ] **[P1] 添加 CI/CD（GitHub Actions）**
  - 配置：推送 master 时自动运行 `tests/run_tests.py`（273 个测试）
  - 额外检查：代码语法检查（flake8），类型检查（mypy 重点模块）
  - 目标：每次提交有基础质量保障

- [ ] **[P2] 清理 scripts/ 目录**
  - 将 `scripts/test_*.py` 临时调试脚本移入 `tests/` 或删除
  - 将可复用的研究函数提取到 `core/research.py`
  - 建立 `scripts/README.md` 说明各脚本用途

---

## Phase 2 — 扩展 Alpha：多策略 + 多因子验证（2~4 个月）

> **目标分值**：75 → 85 分  
> **核心原则**：用同样严格的回测标准验证第 2、3 个策略

### P2-A：趋势跟踪策略（MACD）

- [ ] **[P1] 实现并验证 MACD 趋势跟踪策略**
  - 文件：`core/strategies/macd_trend.py`（新建）
  - 逻辑：MACD 金叉买入 + 死叉卖出；ATR 过滤（低波动期不交易）
  - 验证标准：WFA ≥ 5 窗口，OOS Sharpe > 0.3
  - 注意：MACD 趋势策略与 RSI 均值回归相关性低，组合后预期降低最大回撤

- [ ] **[P2] 策略相关性分析**
  - 计算 RSI策略 vs MACD策略的每日收益相关系数
  - 目标：相关系数 < 0.4（真正意义上的 Alpha 多样化）
  - 输出：相关矩阵图 `outputs/strategy_correlation.png`

### P2-B：订单流因子实战化

- [ ] **[P1] 将 Level 2 OI 因子接入实时信号引擎**
  - 文件：`core/factors/price_momentum.py`（OrderImbalance 因子已实现）
  - 问题：因子代码存在但未在策略中实际使用
  - 修复：在 `config/trading.yaml` 中将 OI 因子权重设为 0.2，并在 WFA 中验证 IC
  - 验证：Level 2 数据 OI 因子 IC > 0.03（日频）

- [ ] **[P2] 实时 Level 2 数据完整性验证**
  - 文件：`core/level2.py`
  - 目标：连续采集 5 个交易日的 5 档盘口数据，验证字段完整率 > 95%
  - 输出：数据质量报告 `outputs/level2_quality_report.md`

### P2-C：外盘领先信号验证

- [ ] **[P2] 验证 SP500 期货对 A 股次日开盘的领先效应**
  - 文件：`core/data_sources.py`（数据源已有）
  - 方法：计算 SP500 期货隔夜涨跌 vs 沪深 300 次日开盘涨跌的 Granger 因果检验
  - 合格标准：Granger p-value < 0.05，且 IC > 0.05
  - 用途：做为开盘方向的过滤条件（外盘大跌时，抑制 A 股买入信号）

- [ ] **[P2] 北向资金信号统计验证**
  - 文件：`backend/services/northbound.py`
  - 方法：验证北向净流入 > 50 亿当天，A 股指数次日涨跌分布
  - 需至少 100 个样本点
  - 合格标准：净流入 > 50 亿时次日上涨概率 > 55%

### P2-D：多因子组合优化

- [ ] **[P2] 实现动态因子权重（基于滚动 IC 加权）**
  - 文件：`core/factor_pipeline.py`
  - 当前：因子权重固定（config 中静态配置）
  - 升级：每月更新因子权重 = 滚动 3 月 IC / Σ(所有因子 3 月 IC)
  - 好处：自适应市场环境，低效因子自动降权

- [ ] **[P2] 因子 IC 时序分析**
  - 文件：`core/research.py`（新增）
  - 计算每个因子的月度 IC 时间序列
  - 分析 IC 在牛市/熊市/震荡市的稳定性
  - 输出：因子 IC 热力图（月份 × 因子）

### P2-E：市场状态自适应

- [ ] **[P1] 将市场 Regime 接入策略执行逻辑**
  - 文件：`scripts/quant/regime_detector.py`（已有 BULL/BEAR/VOLATILE/CALM 检测）
  - 问题：Regime 检测存在但未与 StrategyRunner 联动
  - 修复：BEAR 状态时仓位上限降至 50%，VOLATILE 时增大止损阈值
  - 在 StrategyRunner 的 `run_once()` 前先查询当前 Regime

- [ ] **[P2] Regime 分状态回测分析**
  - 对历史回测按 Regime 拆分，分析各 Regime 下的胜率、Sharpe、最大回撤
  - 输出：Regime 分层绩效表

---

## Phase 3 — 接入实盘：真实执行闭环（4~6 个月）

> **目标分值**：85 → 90 分  
> **核心原则**：先港股纸交易闭环，再 A 股实盘

### P3-A：完成 Futu 券商适配器

- [ ] **[P0] 实现 FutuBroker（港股纸交易）**
  - 文件：`core/brokers/futu.py`（当前仅 stub）
  - 依赖：安装 futu-api（`pip install futu-api`），部署 OpenD 客户端
  - 实现方法：`connect()`, `get_positions()`, `get_cash()`, `submit_order()`, `cancel_order()`
  - 测试：在港股纸交易账户完整执行一笔买卖，验证仓位、现金、订单状态同步正确
  - 安全：确保 `dry_run=True` 时绝对不向 Futu API 发送真实订单请求

- [ ] **[P0] 纸交易 vs 回测一致性验证**
  - 运行纸交易 2 周，记录实际信号触发时间、成交价、滑点
  - 与同期回测结果对比：成交价偏差应 < 20 bps
  - 记录并归因任何 > 50 bps 的偏差（流动性/延迟/订单类型）

- [ ] **[P1] 实现 TCA（交易成本分析）模块**
  - 文件：`core/tca.py`（新建）
  - 功能：对每笔成交计算 Implementation Shortfall（决策价 vs 成交价）
  - 按标的、时段、市场环境分类统计平均隐性成本
  - 输出：每月 TCA 报告，反馈用于调整 slippage_bps 参数

### P3-B：异步事件循环升级

- [ ] **[P2] 将 StrategyRunner 升级为 asyncio 驱动**
  - 文件：`core/strategy_runner.py`，`core/event_bus.py`
  - 当前：同步 `time.sleep()` 轮询，EventBus 同步分发
  - 升级：`asyncio` 事件循环 + `asyncio.Queue` 替换同步 EventBus，支持真正并发的数据获取和信号处理
  - 好处：多标的并行获取行情，延迟从 200ms 降至 20ms 量级

- [ ] **[P2] 行情数据推送订阅（Websocket）**
  - 文件：`core/data_layer.py`
  - 当前：每次轮询 HTTP 拉取，延迟高
  - 升级：接入 Futu/Tiger WebSocket 推送，行情到达时立即触发事件
  - 条件：仅在 Futu 券商接入后启用

### P3-C：数据库升级

- [ ] **[P2] 迁移核心交易数据到 PostgreSQL**
  - 当前：`backend/services/portfolio.db`（SQLite）
  - 迁移：positions / trades / signals / daily_meta 表迁至 PostgreSQL
  - 保留 SQLite 作为本地离线模式回退
  - 触发时机：纸交易数据量超过 10 万条 trade 记录时

- [ ] **[P3] 分钟级数据接入 TimescaleDB**
  - 条件：日交易数据量升级后再做
  - 用途：存储分钟 K 线历史，支持 tick 级因子计算
  - 压缩比：原始 CSV 约 500MB/天 → TimescaleDB 压缩后 ~50MB

### P3-D：监控与告警完善

- [ ] **[P1] 添加策略健康度实时监控**
  - 文件：`backend/services/intraday_monitor.py`
  - 新增检查：策略 rolling 20 日 Sharpe 下降 > 30% 时触发告警
  - 新增检查：单日亏损 > 2% 时触发飞书告警并暂停自动交易

- [ ] **[P1] 实现每日回测 vs 实盘对比报告**
  - 每个交易日收盘后，自动对比当日回测信号 vs 实际纸交易信号
  - 记录任何不一致（信号方向差异、触发时间差异）
  - 输出：`reports/daily_bt_live_diff_{date}.json`

- [ ] **[P2] 添加 Dashboard 实盘监控页面**
  - 文件：`streamlit_app.py`（新增第 7 个 tab）
  - 内容：实时持仓、当日 P&L、策略信号历史、风控状态、TCA 统计
  - 数据源：从 PostgreSQL 读取（降级到 SQLite）

---

## 持续优化项（Backlog，无固定时间表）

- [ ] **完成 Tiger / IBKR 券商适配器**（适合美股/港股多市场）
- [ ] **期权策略框架**（隐波动率曲面、Put/Call 对冲）
- [ ] **新闻情感因子完整验证**（LLM 打分 IC 统计）
- [ ] **多账户支持**（策略组合层面的资金分配）
- [ ] **CVaR / Expected Shortfall**（替换简化历史模拟 VaR）
- [ ] **蒙特卡洛压力测试**（对极端市场情境的组合韧性分析）

---

## 评分追踪

| 评估时间 | 综合得分 | 架构 | 数据 | 策略 | 回测严谨性 | 风控 | 生产就绪 |
|---------|---------|------|------|------|-----------|------|---------|
| 2026-04-22（当前）| **62/100** | 23/30 | 8/20 | 9/20 | 7/15 | 8/10 | 7/15 |
| Phase 1 完成后（预计）| **75/100** | 25/30 | 13/20 | 10/20 | 12/15 | 9/10 | 6/15 |
| Phase 2 完成后（预计）| **85/100** | 27/30 | 15/20 | 16/20 | 13/15 | 9/10 | 5/15 |
| Phase 3 完成后（预计）| **90/100** | 28/30 | 16/20 | 16/20 | 13/15 | 9/10 | 12/15 |
