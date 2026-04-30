# A 股量化交易系统

基于 A 股的专业化量化研究与全自动模拟交易平台，支持多因子选股、机器学习价格预测、算法订单执行、组合优化、实时盘中监控与告警。

> **系统状态**：920 个测试全部通过 | 29 个因子 | SimulatedBroker + FutuBroker 双模式 | 全自动无人值守模拟交易就绪 | Prometheus 监控就绪

---

## 目录

- [系统架构](#系统架构)
- [核心能力概览](#核心能力概览)
- [快速启动](#快速启动)
- [自动化交易闭环](#自动化交易闭环)
- [项目结构](#项目结构)
- [核心模块详解](#核心模块详解)
  - [多因子系统](#多因子系统)
  - [动态选股引擎](#动态选股引擎)
  - [机器学习框架](#机器学习框架)
  - [算法订单执行](#算法订单执行)
  - [组合优化器](#组合优化器)
  - [风控体系](#风控体系)
  - [券商适配层](#券商适配层)
  - [告警系统](#告警系统)
- [回测框架](#回测框架)
- [Web UI](#web-ui)
- [运行测试](#运行测试)
- [已知限制与路线图](#已知限制与路线图)
- [免责声明](#免责声明)

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        Web UI (Streamlit :8501)                          │
│  组合概览 · 实时信号 · 动态选股 · 回测分析 · 持仓详情 · 历史交易 · 策略健康  │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ HTTP
┌──────────────────────────────▼───────────────────────────────────────────┐
│                    Backend Service (Flask :5555)                          │
│  31 个 HTTP API 端点 · SQLite 持久化 · RotatingFileHandler 日志轮转        │
│                                                                           │
│  ┌──────────────────────┐  ┌──────────────────────────────────────────┐  │
│  │  Scheduler           │  │  IntradayMonitor                         │  │
│  │  每日 15:10 CST       │  │  盘中 5 分钟轮询 (09:30-15:00)            │  │
│  │  A 股节假日感知        │  │  止盈/止损/新仓检测 · 飞书推送            │  │
│  └──────────────────────┘  └──────────────────────────────────────────┘  │
└──────────────┬──────────────────────────────┬────────────────────────────┘
               │                              │
┌──────────────▼──────────────┐  ┌────────────▼───────────────────────────┐
│    数据层 (DataLayer)         │  │    策略执行层 (StrategyRunner)           │
│  ├─ AKShare 日线/分钟 K 线    │  │  ├─ AsyncStrategyRunner (asyncio)      │
│  ├─ Parquet 本地缓存          │  │  ├─ FactorPipeline + 动态 IC 加权       │
│  ├─ Level2 盘口（5 档）       │  │  ├─ Regime 自适应（4 种市场状态）        │
│  ├─ 基本面数据（财报季度）      │  │  └─ EventBus 信号分发                  │
│  ├─ 北向/融资融券实时数据       │  └────────────────┬───────────────────────┘
│  ├─ CircuitBreaker 熔断保护   │                   │
│  └─ DataQualityChecker       │  ┌────────────────▼───────────────────────┐
└─────────────────────────────┘  │    因子层 (22 个因子)                    │
                                  │  价格动量(5) · 技术(7) · 基本面(5)       │
                                  │  情绪(3) · ML 预测(1) · NLP 情感(1)     │
                                  └────────────────┬───────────────────────┘
                                                   │
           ┌───────────────────────────────────────▼───────────────────────┐
           │               执行与优化层                                       │
           │  OMS（启动时加载持仓）· VWAP/TWAP 执行 · ImpactEstimator        │
           │  PortfolioOptimizer (MVO/BL/风险平价/最大分散化)                 │
           │  PortfolioAllocator（多策略资金分配 + 再平衡）                   │
           └───────────────────────────────────────┬───────────────────────┘
                                                   │
     ┌─────────────────────────────────────────────▼─────────────────────┐
     │                   风控体系（三层 + 组合层）                           │
     │  RiskEngine (PreTrade/InTrade/PostTrade) · PositionBook 定期刷新   │
     │  PortfolioRiskChecker · CVaR · MonteCarloStressTest (5000 次)      │
     └─────────────────────────────────────────────┬─────────────────────┘
                                                   │
     ┌─────────────────────────────────────────────▼─────────────────────┐
     │                        券商适配层                                    │
     │  SimulatedBroker (A 股规则) · FutuBroker (OpenD 纸交易)             │
     │  TigerBroker stub · IBKRBroker stub                                │
     └─────────────────────────────────────────────┬─────────────────────┘
                                                   │
           ┌────────────────────────────────────────▼──────────────────┐
           │             告警与监控                                       │
           │  AlertManager (企业微信/钉钉/SMTP) · StrategyHealth         │
           │  DailyDiffReporter · TCA 交易成本分析                       │
           └───────────────────────────────────────────────────────────┘
```

---

## 核心能力概览

| 模块 | 关键特性 |
|------|---------|
| **多因子系统** | 29 个因子（价格/技术/基本面/情绪/ML/NLP/宏观），动态 IC 加权 + ML 因子选择 |
| **动态选股** | 五维评分（新闻热度35%+板块行情35%+资金流向25%+技术趋势15%+成分股一致性10%） |
| **ML 框架** | XGBoost Walk-Forward 训练（252/63/21 窗口），无模型时自动降级 |
| **算法执行** | VWAP/TWAP 拆单，Almgren-Chriss 市场冲击估算，A 股整手处理 |
| **组合优化** | 6 种方法：GMV/MaxSharpe/风险平价/BL/最大分散化/等权，Ledoit-Wolf 收缩 |
| **三层风控** | PreTrade/InTrade/PostTrade + CVaR + 蒙特卡洛压力测试，PositionBook 5 分钟定期刷新 |
| **自动化调度** | Scheduler（每日15:10，A股节假日感知）+ IntradayMonitor（盘中5分钟轮询） |
| **券商接入** | SimulatedBroker（完整 A 股规则）+ FutuBroker（OpenD SIMULATE 模式） |
| **熔断保护** | CircuitBreaker（3 次失败触发，300s 冷却，CLOSED→OPEN→HALF_OPEN 状态机） |
| **告警系统** | 企业微信/钉钉/SMTP 三渠道，频率限制，每日 P&L 报告 |
| **回测引擎** | 无前视偏差，A 股印花税/涨跌停/停牌，Walk-Forward 验证 |
| **异步执行** | `asyncio.gather()` 并发处理多标的，N 标的延迟从 N×200ms → 200ms |
| **退出引擎** | ExitEngine P0-P9 优先级体系（EMERGENCY→TIME_STOP），统一卖出信号引擎 |
| **生产流水线** | PipelineFactory 三层因子流水线（技术层/基本面层/宏观层），一键生成 |
| **运营报告** | DailyOpsReporter 每日 16:00 自动汇总 P&L/策略健康/告警/因子 IC |
| **监控看板** | Prometheus MetricsRegistry + `/metrics` 端点，Grafana 可直接接入 |
| **报告导出** | BacktestReportExporter PDF 导出（封面+净值曲线+回撤图+绩效表） |
| **ML 因子选择** | FactorSelector LightGBM 预测因子 IC，Walk-Forward 防过拟合 |
| **融资融券** | MarginDataStore 自动拉取 + Parquet 时序，MarginTrading/ShortInterest 因子 |

---

## 快速启动

### 1. 环境准备

```bash
# Python ≥ 3.10
git clone https://github.com/sinterwong/a-quantitative-trading.git
cd a-quantitative-trading

# 安装核心依赖
pip install -r requirements.txt

# 安装 ML 相关（可选，无此包时 MLPredictionFactor 自动降级为零）
pip install xgboost scikit-learn lightgbm

# 安装 Futu 纸交易（可选，需同时部署 OpenD 客户端）
pip install futu-api
```

### 2. 配置 .env

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
# LLM（用于 NLP 情感因子，可留空，因子自动降级为零）
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# 告警 Webhook（可留空，降级为本地日志）
WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx

# 飞书早晚报通知（可留空）
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_USER_OPEN_ID=
```

### 3. 启动后端服务

```bash
# 完整模式（API + 定时任务 + 盘中监控）
python backend/main.py --mode both

# 仅 API（开发调试）
python backend/main.py --mode api
```

验证：

```bash
curl http://127.0.0.1:5555/health
# → {"status":"ok","timestamp":"..."}
```

### 4. 启动 Web UI

```bash
streamlit run streamlit_app.py --server.port 8501
```

浏览器访问 `http://localhost:8501`

---

## 自动化交易闭环

系统实现了无人值守的全自动模拟交易闭环，由三层调度协同驱动：

### 调度架构

```
每日 15:10 CST（Scheduler）
    ↓
is_trading_day()  ← AKShare 交易日历（A 股节假日感知）
    ↓ 是交易日
POST /analysis/run → StrategyRunner.run_once()
    ├─ 固定标的扫描（config.live_symbols）
    └─ 结果写入 SQLite + 飞书通知

盘中每 5 分钟（IntradayMonitor，09:30-15:00）
    ↓
_check_and_push(now)
    ├─ _check_stop_losses()    止损检测
    ├─ _check_take_profits()   止盈检测
    ├─ _check_new_positions()  动态选股建仓（DynamicStockSelectorV2）
    ├─ _check_watchlist()      自选股异动
    └─ _check_market_index()   大盘异动
    ↓
CooldownTracker（同标的 15 分钟内最多触发一次）
    ↓
飞书/企业微信告警推送 + 模拟下单（trading_mode=live 时自动执行）
```

### 单轮信号生成流程

```
StrategyRunner.run_once()
    │
    ├─ get_regime() → BULL / BEAR / VOLATILE / CALM
    │   BEAR 状态：禁止新建多仓，阈值 ×1.4
    │   VOLATILE：阈值 ×1.2
    │
    ├─ FOR EACH symbol:
    │   ├─ DataLayer.get_bars(days=120) + get_realtime()
    │   ├─ FactorPipeline.run() → combined_score（z-score 加权）
    │   │   └─ dominant_signal: 'BUY' / 'SELL' / 'HOLD'
    │   ├─ RiskEngine.check()
    │   │   ├─ PositionBook（每 5 分钟自动刷新）
    │   │   ├─ 单标的仓位 ≤ 25%
    │   │   ├─ 日亏损熔断 ≤ 2%
    │   │   └─ 总净暴露 ≤ 90%
    │   └─ OMS.submit_from_signal()
    │       ├─ 启动时已从 Backend API 加载持仓快照
    │       ├─ Kelly 仓位计算（半 Kelly）
    │       └─ broker.send(order) → FillEvent
    │
    └─ 告警推送（AlertManager）
```

### 动态选股（DynamicStockSelectorV2）

五维度评分模型，每轮自动筛选最优标的：

| 维度 | 权重 | 数据来源 |
|------|------|---------|
| 板块行情 | 35% | 东方财富实时涨跌幅相对排名 |
| 新闻热度 | 35% | 政策/业绩/产品/资金/传闻加权 |
| 资金流向 | 25% | 北向资金 + 主力净流入排名 |
| 技术趋势 | 15% | 成分股涨跌幅信号 |
| 成分股一致性 | 10% | 板块内部联动强度 |

```python
# 可选：LLM 新闻情绪过滤（bearish confidence > 0.60 → 阻止建仓）
from scripts.dynamic_selector import DynamicStockSelectorV2

selector = DynamicStockSelectorV2()
ranked = selector.calc_all_scores()   # {symbol: score}
top5 = sorted(ranked, key=ranked.get, reverse=True)[:5]
```

### 异常容错设计

| 层级 | 异常场景 | 处理策略 |
|------|---------|---------|
| 单标的失败 | 数据缺失/因子报错 | 跳过该标的，继续其余 |
| 风控异常 | RiskEngine 崩溃 | 拒绝下单（不放行）+ ERROR 日志 |
| 网络抖动 | API 超时/限频 | CircuitBreaker 熔断（3次触发，300s冷却） |
| 告警失败 | Webhook 不可达 | 静默记录，5 分钟后重试下一条 |
| 主循环异常 | 未捕获异常 | 日志记录，等待下轮（不中断服务） |

---

## 项目结构

```
a-quantitative-trading/
│
├── core/                          # 核心业务逻辑（可测试、可组合）
│   ├── factors/
│   │   ├── base.py                # Factor 基类 + FactorCategory + Signal
│   │   ├── price_momentum.py      # RSI / Bollinger / MACD / ATR / OrderImbalance
│   │   ├── technical.py           # IntraVWAP / OpenGap / VolAcceleration 等 7 个
│   │   ├── fundamental.py         # PE / ROE / EarningsSurprise 等 5 个
│   │   ├── sentiment.py           # MarginTrading / NorthboundFlow / ShortInterest
│   │   └── nlp.py                 # NewsSentimentFactor（东财新闻 + Claude API）
│   │
│   ├── ml/
│   │   ├── feature_store.py       # 从 FactorRegistry 自动构建特征矩阵
│   │   ├── price_predictor.py     # XGBoostPredictor + WalkForwardTrainer
│   │   └── model_registry.py      # joblib 模型版本管理（data/ml_models/）
│   │
│   ├── execution/
│   │   ├── algo_base.py           # AlgoOrder 抽象基类 + OrderSlice + AlgoOrderResult
│   │   ├── vwap_executor.py       # VWAP 拆单（U 型分布 / 历史分布）
│   │   ├── twap_executor.py       # TWAP 均匀拆单（支持时间抖动）
│   │   └── impact_estimator.py    # Almgren-Chriss 市场冲击估算
│   │
│   ├── brokers/
│   │   ├── base.py                # BrokerBase ABC（12 个抽象方法）
│   │   ├── simulated.py           # SimulatedBroker（完整 A 股规则）
│   │   ├── futu.py                # FutuBroker（OpenD SIMULATE，离线优雅降级）
│   │   ├── tiger.py               # TigerBroker stub
│   │   └── ibkr.py                # IBKRBroker stub
│   │
│   ├── factor_registry.py         # 因子注册表（22 个内置因子）
│   ├── factor_pipeline.py         # FactorPipeline + DynamicWeightPipeline（滚动 IC 加权）
│   ├── portfolio_optimizer.py     # MVO + BL + 风险平价 + 最大分散化（6 种方法）
│   ├── portfolio_allocator.py     # 多策略资金分配 + 再平衡（时间/阈值触发）
│   ├── portfolio_risk.py          # CVaR / VaR / 行业集中度 / 蒙特卡洛压力测试
│   ├── risk_engine.py             # 三层风控 + PositionBook（5 分钟定期刷新）
│   ├── oms.py                     # OMS（启动加载持仓 + VWAP/TWAP 算法单）
│   ├── backtest_engine.py         # 事件驱动回测（无前视偏差，A 股完整规则）
│   ├── walkforward.py             # WFA + SensitivityAnalyzer（参数稳健性热力图）
│   ├── multi_symbol_backtest.py   # 沪深 300 成分股批量 WFA
│   ├── strategy_runner.py         # 策略主循环（RunnerConfig + Regime 自适应）
│   ├── async_runner.py            # AsyncStrategyRunner（asyncio 驱动）
│   ├── alerting.py                # AlertManager（企业微信/钉钉/SMTP + 频率限制）
│   ├── strategy_health.py         # 健康度监控（Rolling Sharpe / 连续亏损 / 换手率）
│   ├── paper_trade_validator.py   # 纸交易 vs 回测一致性验证 + FutuPaperValidator
│   ├── tca.py                     # TCA 交易成本分析（Implementation Shortfall）
│   ├── daily_diff_reporter.py     # 每日回测 vs 实盘对比报告
│   ├── research.py                # FactorICAnalyzer / RegimeBacktestAnalyzer
│   ├── regime.py                  # 市场 Regime 识别（BULL/BEAR/VOLATILE/CALM）
│   ├── data_layer.py              # 统一数据接口（DataLayer + 三层缓存）
│   ├── data_quality.py            # 数据质量检验（跳空/异常涨跌/零成交量）
│   ├── fundamental_data.py        # 基本面数据管道（AKShare 财报，TTL 缓存）
│   ├── hk_data_source.py          # 港股数据源适配
│   ├── level2.py                  # Level2 盘口数据结构 + 实时因子
│   ├── level2_quality.py          # Level2 数据完整性采集与报告
│   ├── external_signal.py         # SP500 Granger 检验 / 北向资金统计验证
│   ├── event_bus.py               # EventBus（线程安全）
│   └── config.py                  # TradingConfig（统一 YAML + 环境变量）
│
├── backend/
│   ├── main.py                    # 入口（Flask API + Scheduler + IntradayMonitor）
│   │                              #   · RotatingFileHandler 日志（100MB/5备份）
│   │                              #   · is_trading_day() AKShare 节假日日历
│   ├── api.py                     # 31 个 HTTP API 端点
│   └── services/
│       ├── intraday_monitor.py    # 盘中监控（5 分钟轮询，止损/止盈/新仓）
│       ├── portfolio.py           # PortfolioService（SQLite 持久化）
│       ├── broker.py              # Broker 服务层
│       ├── signals.py             # 信号存储与查询
│       ├── circuit_breaker.py     # CircuitBreaker（熔断器状态机）
│       ├── data_cache.py          # 多级数据缓存 + 降级链
│       ├── northbound.py          # 北向资金实时数据
│       ├── fund_flow.py           # 资金流向聚合
│       ├── fundamentals.py        # 基本面服务层
│       ├── performance.py         # 绩效统计
│       ├── alert_history.py       # 告警历史 JSON 持久化
│       ├── report_sender.py       # 飞书/邮件报告发送
│       ├── watchlist.py           # 自选股管理
│       ├── strategy_loader.py     # 策略配置热加载
│       ├── walkforward_persistence.py  # WFA 结果持久化
│       ├── fetchers/              # 分数据源抓取器
│       ├── channels/              # 告警渠道适配
│       └── llm/                   # LLM 情绪分析服务
│
├── scripts/                       # 运营脚本（可独立运行）
│   ├── morning_runner.py          # 早盘全自动纸交易闭环
│   ├── morning_report.py          # 每日早报生成（飞书推送）
│   ├── afternoon_report.py        # 收盘复盘报告
│   ├── dynamic_selector.py        # DynamicStockSelectorV2（五维评分选股）
│   ├── regime_wfa.py              # 市场环境 + Walk-Forward 参数优化
│   ├── walkforward_job.py         # WFA 批量作业
│   ├── sensitivity_job.py         # 策略参数敏感性分析
│   └── stock_data_only.py         # 数据拉取（调试用）
│
├── tests/                         # 920 个单元测试（40 个测试文件）
│   ├── test_strategy_runner.py    # 策略主循环 + Regime 联动
│   ├── test_alerting.py           # AlertManager（36 个测试）
│   ├── test_algo_execution.py     # VWAP/TWAP/ImpactEstimator（40 个测试）
│   ├── test_broker_base.py        # BrokerBase/SimulatedBroker（40 个测试）
│   ├── test_futu_broker.py        # FutuBroker 离线降级（43 个测试）
│   ├── test_ml_predictor.py       # XGBoost/WalkForward（50 个测试）
│   ├── test_nlp_factor.py         # NewsSentimentFactor（27 个测试）
│   ├── test_portfolio_optimizer.py # MVO+BL（45 个测试）
│   ├── test_technical_factors.py  # 技术因子（35 个测试）
│   ├── test_fundamental_factors.py # 基本面因子（35 个测试）
│   ├── test_sentiment_factors.py  # 情绪因子（30 个测试）
│   ├── test_dynamic_selector.py   # 动态选股引擎
│   ├── test_async_runner.py       # 异步策略执行
│   ├── test_factor_selector.py    # ML 因子选择
│   ├── test_metrics.py            # Prometheus 监控
│   ├── test_report_exporter.py    # PDF 报告导出
│   ├── test_daily_ops_reporter.py # 每日运营报告
│   ├── test_margin_data_store.py  # 融资融券数据
│   ├── test_scheduler_fundamental.py # 基本面调度
│   └── ...（其余 21 个测试文件）
│
├── config/
│   └── trading.yaml               # 统一策略配置（多策略/风控/数据源/环境切换）
│
├── streamlit_app.py               # Web UI（7 个页面）
├── requirements.txt
└── .env.example
```

---

## 核心模块详解

### 多因子系统

系统内置 **29 个因子**，覆盖价格动量、技术微观结构、基本面、情绪、AI 信号和宏观经济六大类别：

| 类别 | 因子名称 | 文件 |
|------|---------|------|
| 价格动量 | RSI / BollingerBands / MACD / ATR / OrderImbalance | `factors/price_momentum.py` |
| 技术微观结构 | IntraVWAP / OpenGap / VolAcceleration / BidAskSpread / BuyingPressure / SectorMomentum / IndexRelativeStrength | `factors/technical.py` |
| 基本面 | PEPercentile / ROEMomentum / EarningsSurprise / RevenueGrowth / CashFlowQuality / ShareholderConcentration | `factors/fundamental.py` |
| 情绪 | MarginTrading / NorthboundFlow / ShortInterest | `factors/sentiment.py` |
| 宏观经济 | PMI / M2Growth / CreditImpulse | `factors/macro.py` |
| ML 预测 | MLPrediction（XGBoost 上涨概率） | `ml/price_predictor.py` |
| NLP 情感 | NewsSentiment（东财新闻 + LLM API） | `factors/nlp.py` |

```python
from core.factor_pipeline import FactorPipeline, DynamicWeightPipeline

# 静态权重流水线
pipeline = FactorPipeline()
pipeline.add('RSI',            weight=0.3)
pipeline.add('MACD',           weight=0.2)
pipeline.add('SectorMomentum', weight=0.2)
pipeline.add('NewsSentiment',  weight=0.15)
pipeline.add('MLPrediction',   weight=0.15)

result = pipeline.run(symbol='000001.SZ', data=df, price=15.0)
print(result.combined_score)   # float，正=偏多，负=偏空
print(result.dominant_signal)  # 'BUY' / 'SELL' / 'HOLD'

# 动态 IC 加权（推荐生产使用）
# 权重每 21 天根据滚动 IC 自动调整，全 IC ≤ 0 时退回等权
dyn = DynamicWeightPipeline(update_freq_days=21)
dyn.add('RSI', weight=0.2)
dyn.add('MACD', weight=0.2)
```

### 动态选股引擎

`DynamicStockSelectorV2` 实现基于五维评分的自动标的筛选，完全内置于 IntradayMonitor 闭环中：

```python
from scripts.dynamic_selector import DynamicStockSelectorV2

selector = DynamicStockSelectorV2()
scores = selector.calc_all_scores()
# → {'510310.SH': 0.82, '600900.SH': 0.74, '300750.SZ': 0.61, ...}

# 可选：LLM 新闻情绪过滤（bearish confidence > 0.60 → 阻止建仓）
# 无 API Key 时自动跳过 LLM 步骤
```

五维评分权重：

```
板块行情分  35%  ← 东方财富实时涨跌幅相对排名
新闻热度分  35%  ← 政策/业绩/产品/资金/传闻加权
资金流向分  25%  ← 北向净买入 + 主力净流入排名
技术趋势分  15%  ← 成分股涨跌幅信号
成分股一致性 10%  ← 板块内部联动强度
```

### 机器学习框架

```python
from core.ml.price_predictor import MLPredictionFactor, WalkForwardTrainer
from core.ml.feature_store import FeatureStore

# 训练（Walk-Forward，防止过拟合）
# 训练窗口 252 天 / 验证窗口 63 天 / 步长 21 天
factor = MLPredictionFactor(forward_days=2)
wf_result = factor.fit(historical_data, use_walk_forward=True)
print(f"OOS Sharpe: {wf_result.oos_sharpe:.3f}")

# 推断（无模型时自动返回全零，不崩溃）
factor.load('000001.SZ')
scores = factor.evaluate(recent_data)   # pd.Series，z-score

# 特征工程（自动从所有注册因子构建）
fs = FeatureStore(forward_days=2)
X, y = fs.build(data, symbol='000001.SZ')
# 特征包含：所有因子值 + 时间特征（星期/月份 sin/cos 编码 + 季末标志）
```

### 算法订单执行

```python
from core.oms import OMS

# OMS 初始化时自动从 Backend API 加载现有持仓快照
oms = OMS(broker=simulated_broker)

# VWAP 拆单（基于 U 型历史成交量分布，A 股整手处理）
result = oms.submit_algo_order(
    algo='VWAP',
    symbol='600519.SH',
    direction='BUY',
    total_shares=1000,
    duration_minutes=60,
    reference_price=1800.0,
)
print(f"成交率: {result.fill_rate:.1%}, 滑点: {result.slippage_bps:.1f} bps")

# 市场冲击估算（Almgren-Chriss 简化版）
from core.execution.impact_estimator import ImpactEstimator
est = ImpactEstimator()
perm, temp = est.decompose(5000, 200_000)
# 永久冲击 = 5 × sqrt(参与率)，临时冲击 = 5 × 参与率（单位 bps）
```

### 组合优化器

```python
from core.portfolio_optimizer import PortfolioOptimizer

optimizer = PortfolioOptimizer(
    returns=daily_returns_df,   # shape: (n_days, n_assets)
    cov_method='ledoit_wolf',   # Ledoit-Wolf 收缩协方差
    max_weight=0.25,
    min_weight=0.0,
)

w_gmv = optimizer.min_variance()        # 全局最小方差
w_msr = optimizer.max_sharpe()          # 最大 Sharpe 比率
w_rp  = optimizer.risk_parity()         # 等风险贡献（ERC）
w_md  = optimizer.max_diversification() # 最大分散化比率
w_ew  = optimizer.equal_weight()        # 等权基准

# Black-Litterman（融入策略因子观点）
views = {'000001.SZ': 0.08, '600519.SH': 0.12}
confs = {'000001.SZ': 0.7,  '600519.SH': 0.8}
w_bl  = optimizer.black_litterman(views, confs)

# 换手率约束（月度换手率 ≤ 30%）
w_adj = optimizer.apply_turnover_constraint(w_bl, w_current, max_turnover=0.30)
```

### 风控体系

三层单标的风控 + 组合层风控：

```python
from core.risk_engine import RiskEngine, RiskConfig

cfg = RiskConfig(
    max_position_pct=0.25,    # 单标的最大仓位 25%
    daily_loss_limit=0.02,    # 日亏损熔断 2%
    chandelier_atr_mult=3.0,  # Chandelier Exit：3×ATR 止损
)
engine = RiskEngine(cfg, data_layer=dl)
# PositionBook 在后台每 5 分钟自动同步最新持仓快照

# PreTrade：开仓前（仓位/行业集中度/VaR 检查）
result = engine.check(signal)
if not result.passed:
    print(result.reason)  # 异常时返回 False，不放行

# InTrade：持仓中（Chandelier Exit / 浮亏止损）
actions = engine.in_trade_check(positions, current_prices)

# PostTrade：收盘后（日亏损 / 最大回撤 / CVaR）
report = engine.post_trade_check(portfolio_snapshot)
```

```python
from core.portfolio_risk import PortfolioRiskChecker, MonteCarloStressTest

# 组合层风控
checker = PortfolioRiskChecker(var_limit=0.03, max_drawdown=0.15)
result = checker.check_before_buy(snapshot)

# 蒙特卡洛压力测试（5000 次模拟，bootstrap / 参数法）
stress = MonteCarloStressTest(n_simulations=5000)
report = stress.run(portfolio_returns, horizon_days=20)
print(f"P5 最大回撤: {report.p5_max_drawdown:.1%}")
print(f"Expected Shortfall (95%): {report.expected_shortfall:.1%}")
```

### 券商适配层

所有券商实现 `BrokerBase` ABC（12 个标准方法），策略代码无需感知底层差异：

```python
# SimulatedBroker — 完整 A 股规则（印花税/整手/涨跌停/停牌）
from core.brokers.simulated import SimulatedBroker
broker = SimulatedBroker(initial_cash=1_000_000)
fill = broker.submit_order('000001.SZ', 'BUY', shares=100, price=15.0)

# FutuBroker — 需安装 futu-api + 部署 OpenD
from core.brokers.futu import FutuBroker
futu = FutuBroker(host='127.0.0.1', port=11111, trd_env='SIMULATE')
if futu.connect():
    positions = futu.get_positions()   # 实时持仓（含浮动盈亏）
    account = futu.get_account()
    fill = futu.submit_order('HK.00700', 'BUY', 100, 350.0)
else:
    # OpenD 未运行时：所有方法返回安全默认值，不崩溃
    print("已降级为离线模式")
```

纸交易一致性验证：

```python
from core.paper_trade_validator import FutuPaperValidator

validator = FutuPaperValidator(signal_match_target=0.95)
validator.connect()  # 自动 fallback 到 SimulatedBroker
validator.validate_signals(signals)
report = validator.generate_daily_report('outputs/paper_trade/2026-04-28.json')
```

### 告警系统

```python
from core.alerting import AlertManager, get_alert_manager

am = AlertManager(
    wechat_webhook='https://qyapi.weixin.qq.com/...',
    dingtalk_webhook='https://oapi.dingtalk.com/...',
    min_level='WARNING',       # INFO / WARNING / CRITICAL
    rate_limit_sec=300,        # 同内容 5 分钟内不重复推送
)

am.send_critical('日亏损超过 2%，触发熔断')
am.send_warning('RSI 策略信号与回测偏差 > 5%')

# 每日收盘报告（自动穿透频率限制）
am.send_daily_report({
    'date': '2026-04-28',
    'total_pnl': 3200.0,
    'pnl_pct': 0.032,
    'n_trades': 8,
    'positions': {'000001.SZ': {'pnl': 1800, 'pct': 0.012}},
})

# 全局单例，跨模块共享同一个实例
get_alert_manager().send_info('策略启动完成')
```

---

## 回测框架

无前视偏差的事件驱动回测引擎：

```python
from core.backtest_engine import BacktestEngine, BacktestConfig

cfg = BacktestConfig(
    initial_capital=1_000_000,
    bar_freq='daily',           # daily / minute
    stamp_tax_rate=0.001,       # A 股卖出印花税 0.1%
    commission_rate=0.0003,     # 双边手续费 0.03%
    slippage_pct=0.001,         # 滑点 0.1%
    adj_type='qfq',             # 前复权
)
# 成交价：下一根 bar 的 open（修复前视偏差）

engine = BacktestEngine(cfg)
result = engine.run(symbol='000001.SZ', data=df, pipeline=pipeline)
print(f"Sharpe: {result.sharpe:.2f}, MaxDD: {result.max_drawdown:.1%}")
```

Walk-Forward 验证（防止过拟合）：

```python
from core.walkforward import WalkForwardAnalyzer, SensitivityAnalyzer

# 22 个滚动窗口（13 年数据，train=18m/test=6m/step=6m）
wfa = WalkForwardAnalyzer(train_months=18, test_months=6, step_months=6)
summary = wfa.run(symbol='000001.SZ', pipeline=pipeline)
print(f"OOS 正 Sharpe 比例: {summary.positive_sharpe_ratio:.1%}")

# 参数稳健性热力图
analyzer = SensitivityAnalyzer()
analyzer.grid_search(
    symbol='000001.SZ',
    param_grid={'rsi_period': [7, 14, 21], 'atr_mult': [2, 3, 4]},
)
analyzer.plot_heatmap('outputs/sensitivity.png')
```

多标的批量验证：

```python
from core.multi_symbol_backtest import MultiSymbolBacktest, DEFAULT_CSI300_TOP10

# 沪深 300 前 10 大成分股：茅台/宁德/招行/平安/东财/五粮液/比亚迪/迈瑞/立讯/恒瑞
mbt = MultiSymbolBacktest()
result = mbt.run(symbols=DEFAULT_CSI300_TOP10, years=5)
result.print_report()  # 合格标准：≥ 7/10 OOS Sharpe > 0
```

---

## Web UI

Streamlit 界面，共 7 个页面：

| 页面 | 功能 |
|------|------|
| 组合概览 | 持仓、现金、总资产、盈亏曲线 |
| 实时信号 | 因子评分、RSI 预警、北向资金共振 |
| 动态选股 | 五维评分排名（新闻/板块/资金/技术/一致性） |
| 回测分析 | Walk-Forward + 蒙特卡洛压力测试 |
| 持仓详情 | 个股因子值 / 量比 / 距涨跌停 |
| 历史交易 | 成交记录与 TCA 绩效归因 |
| 策略健康 | Rolling Sharpe 折线图 / CVaR / AlertManager 历史 |

---

## 运行测试

```bash
# 全量测试（829 个，约 50 秒）
conda run -n quantitative-trading python -m pytest tests/ -q

# 按模块运行
pytest tests/test_alerting.py              # 告警系统（36）
pytest tests/test_algo_execution.py        # VWAP/TWAP 执行（40）
pytest tests/test_ml_predictor.py          # ML 框架（50）
pytest tests/test_portfolio_optimizer.py   # 组合优化（45）
pytest tests/test_futu_broker.py           # Futu 券商（43）
pytest tests/test_broker_base.py           # SimulatedBroker（40）
pytest tests/test_nlp_factor.py            # NLP 情感因子（27）
pytest tests/test_technical_factors.py     # 技术因子（35）
pytest tests/test_fundamental_factors.py   # 基本面因子（35）
pytest tests/test_dynamic_selector.py      # 动态选股引擎
pytest tests/test_strategy_runner.py       # 策略主循环
```

测试设计原则：
- 所有外部 HTTP 请求通过 `unittest.mock.patch` 隔离，不依赖网络
- ML/NLP/Futu 模块在无依赖包或无 API Key 时自动降级，测试始终可运行
- FutuBroker 测试使用端口 19999，确保不意外连接真实 OpenD

---

## 已知限制与路线图

### 当前限制

| 限制 | 说明 | 计划解决 |
|------|------|----------|
| Futu 纸交易待验证 | FutuBroker 代码完整，需真实 OpenD 环境运行 | Phase 4-A |
| ML 模型未用真实数据训练 | XGBoost 框架完备，待接入真实历史数据 | Phase 4-A |
| NLP 因子 IC 未统计 | 待运行 1 个月历史回测验证 IC > 0 | Phase 4-B |
| PostgreSQL 未迁移 | SQLite 满足需求，触发条件：10 万条交易记录 | Phase 4-C |
| 无指数退避重试 | HTTP 调用失败依赖熔断器，无 exponential backoff | Phase 4-A |
| 无进程守护配置 | 无 systemd/supervisord，进程崩溃需手动重启 | Phase 4-A |

### 未来路线图

详见 [TODO.md](TODO.md)：
- **Phase 4**（1-3 月）：Futu 纸交易运营、ML 训练、重试机制、进程守护 ← **当前阶段**
- **Phase 5**（3-9 月）：因子 IC 全量验证、新策略拓展
- **Phase 6**（9-18 月）：港股/美股多市场扩展

### 已完成里程碑（Phase D/E，2026-04-29—04-30）

| 模块 | 文件 | 说明 |
|------|------|------|
| ExitEngine | `core/exit_engine.py` | P0-P9 优先级退出体系（EMERGENCY→TIME_STOP），统一卖出信号引擎 |
| PipelineFactory | `core/pipeline_factory.py` | 生产用因子流水线工厂（技术层/基本面层/宏观层） |
| DailyOpsReporter | `core/daily_ops_reporter.py` | 每日 16:00 自动运营报告，AlertManager 推送 |
| MarginDataStore | `core/factors/sentiment.py` | 融资融券自动拉取 + Parquet 时序（TTL=24h） |
| MetricsRegistry | `core/metrics.py` | Prometheus 监控看板，`/metrics` 端点 |
| BacktestReportExporter | `core/report_exporter.py` | PDF 导出（封面+净值+回撤+绩效+交易统计） |
| FactorSelector | `core/ml/factor_selector.py` | LightGBM 因子选择，Walk-Forward 防过拟合 |
| 基本面调度 | `backend/main.py` | 季报数据自动刷新调度（Scheduler 集成） |
| 早报修复 | `scripts/morning_report.py` | 5 处设计缺陷修复，新增市场环境/开盘订单区块 |
| 主流程集成 | `backend/api.py` | 行业轮动/配对交易 API 端点，全模块接入主流程 |
| 架构修复 | `core/*.py` | 启动竞态、线程安全、风控覆盖等 10+ 处隐患修复 |

---

## 免责声明

本系统仅供研究与教育目的。回测结果不代表未来收益，所有数据仅供参考，不构成任何投资建议。实盘交易请自行承担风险。

---

## 协议

MIT License
