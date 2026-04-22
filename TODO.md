# TODO — 专业实盘级量化系统开发计划

> 评估日期：2026-04-22  
> 目标：从"可用的个人模拟系统（62分）"升级为"专业实盘级系统（90分）"  
> 策略：三阶段推进，每阶段可独立交付，优先解决数据+回测严谨性，再扩展策略，最后接实盘

---

## Phase 1 — 夯实地基：回测严谨性 + 数据质量（1~2个月）

> **目标分值**：62 → 75 分  
> **核心原则**：先把现有 1 个策略的回测做到 "无懈可击"，再谈扩张

### P1-A：修复回测引擎中的关键 Bug（最高优先）

- [x] **[P0] 修复收盘价执行偏差（Look-ahead bias）** ✅ 2026-04-22
  - 文件：`core/backtest_engine.py`，`_on_bar()` / `_generate_signals()` / `_process_signal()`
  - 修复：信号用 `df.iloc[:idx]` 历史生成（排除当前 bar），成交价改为下一根 bar 的 open

- [x] **[P0] 修复 holding_secs 计数逻辑** ✅ 2026-04-22
  - 文件：`core/backtest_engine.py`
  - 修复：BacktestConfig.bar_freq + _bar_secs()，daily=86400 / hourly=3600 / minute=60

- [x] **[P0] 补齐 A 股卖出印花税** ✅ 2026-04-22
  - 文件：`core/backtest_engine.py`，`_execute_sell()`
  - 修复：BacktestConfig.stamp_tax_rate=0.001，卖出时扣 0.1% 印花税

- [x] **[P1] 修复 Kelly 仓位硬编码** ✅ 2026-04-22
  - 文件：`core/backtest_engine.py`，`_calc_kelly_params()` / `_calc_shares_price()`
  - 修复：历史 ≥10 笔时动态计算 win_rate/avg_win/avg_loss，不足时退回保守默认值

- [x] **[P1] 添加复权与停牌处理** ✅ 2026-04-22
  - 文件：`core/backtest_engine.py`
  - 修复：load_data adj_type 校验（qfq/hfq/none）；volume=0 自动标 is_suspended；停牌日跳过开仓

### P1-B：Walk-Forward 多窗口统计验证

- [x] **[P1] 增加 WFA 窗口数量至 ≥ 5 个** ✅ 2026-04-22
  - 文件：`core/walkforward.py`（新建，替代 scripts 版）
  - 修复：train=18m/test=6m/step=6m，13年数据产生 22 个滚动窗口
  - 输出：WFASummary（OOS Sharpe 分布、正 Sharpe 比例）

- [x] **[P1] 添加参数稳健性检验（Sensitivity Analysis）** ✅ 2026-04-22
  - 文件：`core/walkforward.py`，`SensitivityAnalyzer` 类
  - 双参数网格扫描 → Sharpe 热力图（PNG/CSV），peak_sensitivity_ratio() 量化稳健度

- [ ] **[P2] 扩展回测标的：沪深 300 成分股中选 10 支**
  - 当前仅在 HS300 ETF（510300）单一标的验证
  - 选取流动性前 10 的成分股分别回测，检验策略泛化能力
  - 合格标准：≥ 7/10 标的 OOS Sharpe > 0

### P1-C：数据层加固

- [x] **[P1] 增加分钟 K 线回测支持** ✅ 2026-04-22
  - 文件：`core/data_layer.py`，`DataLayer.get_minute_bars()`
  - 通过 AKShare 获取 1/5/15/30/60 分钟 K 线；BacktestEngine bar_freq='minute' 支持

- [x] **[P2] 日线历史数据本地缓存（Parquet）** ✅ 2026-04-22
  - 文件：`core/data_layer.py`，`ParquetCache` 类
  - data/bars/{symbol}.parquet，upsert 增量更新，缓存新鲜时跳过网络请求

- [x] **[P2] 数据质量检验模块** ✅ 2026-04-22
  - 文件：`core/data_quality.py`（新建）
  - DataQualityChecker：跳空/异常涨跌/零成交量检测，质量评分，drop_anomalies()

### P1-D：工程质量

- [x] **[P1] 添加 CI/CD（GitHub Actions）** ✅ 2026-04-22
  - 配置：`.github/workflows/ci.yml`，推送 master 自动运行 500 个测试
  - flake8 lint（core 重点模块）+ mypy 类型检查 + pytest Phase 1 新测试
  - Linux(3.10/3.11/3.12) + Windows(3.11) 四矩阵并行

- [x] **[P2] 清理 scripts/ 目录** ✅ 2026-04-22
  - `scripts/test_core_arch/phase2~5.py` → `tests/test_legacy_*.py`；`test_backtest_engine.py`（旧版）删除
  - 调试脚本 `test_em_l2_depth.py` / `test_level2.py` 保留在 scripts/（非 unittest）
  - 新建 `scripts/README.md` 说明所有脚本用途

---

## Phase 2 — 扩展 Alpha：多策略 + 多因子验证（2~4 个月）

> **目标分值**：75 → 85 分  
> **核心原则**：用同样严格的回测标准验证第 2、3 个策略

### P2-A：趋势跟踪策略（MACD）

- [x] **[P1] 实现 MACD 趋势跟踪策略** ✅ 2026-04-22
  - 文件：`core/strategies/macd_trend.py`（已新建）
  - 实现：`MACDTrendFactor(Factor)` — 金叉买入 + 死叉卖出，ATR ratio > threshold 时抑制 BUY
  - 接口：`make_macd_trend_pipeline()` 工厂函数，可直接接入 WFA
  - WFA 验证待运行（需真实历史数据，目标 OOS Sharpe > 0.3）

- [x] **[P2] 策略相关性分析** ✅ 2026-04-22
  - 文件：`core/research.py`，`StrategyCorrelationAnalyzer` 类
  - 接受 `{策略名: BacktestResult}` 字典，计算日收益相关矩阵
  - `plot_heatmap()` 输出 `outputs/strategy_correlation.png`
  - 待跑：真实历史数据验证 RSI vs MACD 相关系数 < 0.4

### P2-B：订单流因子实战化

- [x] **[P1] 将 OI 因子接入实时信号引擎** ✅ 2026-04-22
  - 文件：`core/factors/price_momentum.py`，新增 `OrderImbalanceFactor`
  - 实现：OHLCV 代理版 OI（阳线成交量占比，window=10），z-score 归一化
  - 注册至 `core/factor_registry.py`（名称 "OrderImbalance"）
  - 已在 `config/trading.yaml` RSI 策略中加入 weight=0.2（RSI:0.4/ATR:0.2/MACD:0.2/OI:0.2）
  - 待跟进：L2 盘口版（真实 5 档 OI）接入；IC 统计验证 > 0.03

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

- [x] **[P2] 实现动态因子权重（基于滚动 IC 加权）** ✅ 2026-04-22
  - 文件：`core/factor_pipeline.py`，`DynamicWeightPipeline` 类（继承 `FactorPipeline`）
  - 每 `update_freq_days`（默认 21）更新权重 = max(IC, 0) / Σmax(IC,0)
  - 全 IC ≤ 0 时自动退回等权；`weight_history_df()` / `current_weights()` 方便诊断
  - 不依赖 scipy，用 numpy rank + corrcoef 实现 Spearman IC

- [x] **[P2] 因子 IC 时序分析** ✅ 2026-04-22
  - 文件：`core/research.py`，`FactorICAnalyzer` 类
  - `analyze()` 输出月度 IC 序列、IC均值/IR/IC>0占比、按 Regime 分层 IC
  - `plot_heatmap()` 输出 `outputs/factor_ic_heatmap.png`（月份 × 因子）
  - `summary_table()` 返回多因子汇总 DataFrame

### P2-E：市场状态自适应

- [x] **[P1] 将市场 Regime 接入策略执行逻辑** ✅ 2026-04-22
  - 新建：`core/regime.py`（独立无副作用，进程内日缓存，含 `RegimeInfo` 数据类）
  - 修改：`core/strategy_runner.py`，`RunnerConfig.regime_aware=True`
  - 效果：BEAR → 禁止新多仓 + 信号阈值 ×1.4；VOLATILE → 阈值 ×1.2；每轮 run_once() 检测一次
  - 暴露 `runner.current_regime` 属性，方便外部监控当前 Regime

- [x] **[P2] Regime 分状态回测分析** ✅ 2026-04-22
  - 文件：`core/research.py`，`RegimeBacktestAnalyzer` 类
  - `build_regime_series(data)` 从 OHLCV 计算历史 Regime 标签序列
  - `analyze(result, regime_series)` 按 BULL/BEAR/VOLATILE/CALM 分层统计
  - 输出：`RegimeAnalysisResult.to_dataframe()` / `print_report()`

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

- [x] **[P1] 实现 TCA（交易成本分析）模块** ✅ 2026-04-22
  - 文件：`core/tca.py`（新建）
  - `TCARecord`：单笔 IS / 总成本 bps 计算；`TCAAnalyzer`：按标的/方向/Regime/时段/月份统计
  - `from_backtest_result()` / `from_trade_dicts()` 两种数据源接入
  - `recommended_slippage_bps` 自动推荐参数；`save_monthly_report()` 输出 JSON

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

- [x] **[P1] 添加策略健康度实时监控** ✅ 2026-04-22
  - 文件：`core/strategy_health.py`（新建，独立无 broker 依赖）
  - `StrategyHealthMonitor.check()`：Rolling Sharpe(20d/60d) 下降 >30%、单日亏损 >2%、连续亏损 >5 天、换手率异常
  - `check_series()` 返回逐日时序 DataFrame（供 Streamlit 折线图）
  - `HealthReport.to_feishu_text()` 一键生成飞书告警文本

- [ ] **[P1] 实现每日回测 vs 实盘对比报告**
  - 每个交易日收盘后，自动对比当日回测信号 vs 实际纸交易信号
  - 记录任何不一致（信号方向差异、触发时间差异）
  - 输出：`reports/daily_bt_live_diff_{date}.json`

- [x] **[P2] 添加 Dashboard 实盘监控页面** ✅ 2026-04-22
  - 文件：`streamlit_app.py`（新增第 8 页「🏥 策略健康」）
  - 内容：健康状态卡 / Rolling Sharpe 折线图 / 胜率时序 / TCA 成本分析 / CVaR + 蒙特卡洛
  - 数据源：backend API 降级到 SQLite portfolio.db

---

## 持续优化项（Backlog，无固定时间表）

- [ ] **完成 Tiger / IBKR 券商适配器**（适合美股/港股多市场）
- [ ] **期权策略框架**（隐波动率曲面、Put/Call 对冲）
- [ ] **新闻情感因子完整验证**（LLM 打分 IC 统计）
- [ ] **多账户支持**（策略组合层面的资金分配）
- [x] **CVaR / Expected Shortfall** ✅ 2026-04-22（`core/portfolio_risk.py`，`check_cvar()` 方法）
- [x] **蒙特卡洛压力测试** ✅ 2026-04-22（`core/portfolio_risk.py`，`MonteCarloStressTest` 类，bootstrap/参数法，5000次模拟，P5/P50/P95/ES/最大回撤分布）

---

## 评分追踪

| 评估时间 | 综合得分 | 架构 | 数据 | 策略 | 回测严谨性 | 风控 | 生产就绪 |
|---------|---------|------|------|------|-----------|------|---------|
| 2026-04-22（当前）| **62/100** | 23/30 | 8/20 | 9/20 | 7/15 | 8/10 | 7/15 |
| Phase 1 完成后（预计）| **75/100** | 25/30 | 13/20 | 10/20 | 12/15 | 9/10 | 6/15 |
| Phase 2 完成后（预计）| **85/100** | 27/30 | 15/20 | 16/20 | 13/15 | 9/10 | 5/15 |
| Phase 3 完成后（预计）| **90/100** | 28/30 | 16/20 | 16/20 | 13/15 | 9/10 | 12/15 |
