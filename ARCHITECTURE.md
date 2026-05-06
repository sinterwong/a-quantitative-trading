# 系统架构

> 本文件描述系统当前生产就绪状态的架构，不含开发过程记录。

---

## 分层架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                    Web UI (Streamlit :8501)                     │
│  组合概览 · 实时信号 · 动态选股 · 回测分析 · 持仓详情 · 历史交易   │
└────────────────────────────┬─────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼─────────────────────────────────────┐
│               Backend Service (Flask :5555)                        │
│  30+ HTTP API 端点 · SQLite 持久化 · RotatingFileHandler 日志    │
│                                                                      │
│  ┌──────────────────────┐  ┌─────────────────────────────────┐  │
│  │ Scheduler             │  │ IntradayMonitor                  │  │
│  │ 每日 15:10 CST 触发   │  │ 盘中 5 分钟轮询 (09:30–15:00)    │  │
│  │ 节假日感知            │  │ 止盈/止损/新仓检测 + 飞书推送    │  │
│  └──────────────────────┘  └─────────────────────────────────┘  │
└──────────────┬─────────────────────────────┬──────────────────────┘
               │                             │
┌──────────────▼──────────────┐  ┌───────────▼──────────────────────┐
│     数据层 (DataLayer)        │  │   策略执行层 (StrategyRunner)     │
│  ├─ AKShare 日线/分钟 K 线   │  │  ├─ AsyncStrategyRunner (asyncio) │
│  ├─ Parquet 本地缓存         │  │  ├─ FactorPipeline + 动态 IC 加权  │
│  ├─ Level2 盘口（5 档）      │  │  ├─ Regime 自适应（4 种市场状态）  │
│  ├─ 基本面（财报/融资融券）   │  │  └─ EventBus 信号分发             │
│  ├─ 北向资金实时             │  └────────────┬────────────────────┘
│  └─ CircuitBreaker 熔断     │               │
└─────────────────────────────┘  ┌────────────▼────────────────────┐
                                 │        因子层（22 个因子）         │
                                 │  价格动量(5) · 技术(7) · 基本面(5) │
                                 │  情绪(3) · ML预测(1) · NLP情感(1) │
                                 └────────────┬────────────────────┘
                                              │
         ┌────────────────────────────────────▼─────────────────────┐
         │                  执行与优化层                              │
         │  OMS · VWAP/TWAP · ImpactEstimator                       │
         │  PortfolioOptimizer (MVO/BL/风险平价/最大分散化)          │
         │  PortfolioAllocator（多策略资金分配 + 再平衡）            │
         └────────────────────────────────────┬─────────────────────┘
                                              │
         ┌────────────────────────────────────▼─────────────────────┐
         │                   风控体系（三层 + 组合层）                │
         │  RiskEngine (PreTrade/InTrade/PostTrade)                 │
         │  CVaR · MonteCarloStressTest (5000 次)                   │
         └────────────────────────────────────┬─────────────────────┘
                                              │
         ┌────────────────────────────────────▼─────────────────────┐
         │                        券商适配层                          │
         │  SimulatedBroker · FutuBroker · IBKRBroker(stub)        │
         │  TigerBroker(stub)                                       │
         └──────────────────────────────────────────────────────────┘
```

---

## 核心设计原则

### 1. EventBus 作为中央总线
所有模块通过事件通信：`MarketEvent` → `SignalEvent` → `OrderEvent` → `FillEvent`。新增因子/策略无需修改核心。

### 2. 回测 = 实盘
同一因子接口、同一信号格式，回测引擎和实盘运行器共享 `FactorPipeline`，消除策略从回测到实盘的差异。

### 3. 券商适配层
`BrokerAdapter` 接口抽象所有券商，切换券商只需替换适配器实例，策略和风控逻辑零改动。

---

## 关键模块

### EventBus (`core/event_bus.py`)

单例事件总线，支持同步/异步两种模式。

```python
from core.event_bus import EventBus, MarketEvent

bus = EventBus.global_bus()
bus.on('MarketEvent', my_handler)
bus.emit(MarketEvent(symbol='000001.SH', close=10.0))
```

### 因子流水线 (`core/pipeline_factory.py`)

```python
from core.pipeline_factory import make_a_stock_pipeline

pipeline = make_a_stock_pipeline(symbol="000001.SH")
result = pipeline.run()  # {'composite_score': 0.72, 'signal': 'BUY', 'weights': {...}}
```

### 回测引擎 (`core/backtest_engine.py`)

```python
from core.backtest_engine import BacktestEngine
from core.strategies.rsi_strategy import RSIStrategy

engine = BacktestEngine(strategy=RSIStrategy(), start="20200101", end="20251231")
report = engine.run()
```

### 组合优化 (`core/portfolio.py`)

```python
from core.portfolio import PortfolioOptimizer

opt = PortfolioOptimizer(method='black_litterman')
weights = opt.optimize(returns=return_df, views={'000001.SH': 0.05})
```

### 风控 (`core/risk_engine.py`)

三层风控：PreTrade（下单前）、InTrade（持仓中）、PostTrade（收盘后）。

```python
from core.risk_engine import RiskEngine

risk = RiskEngine()
result = risk.check_pre_trade(order=order, portfolio=portfolio)
```

### 港股打新分析 (`core/ipo_analyst_engine.py`)

feature/ipo-stars 分支可用。详见 `reports/ipo_renderer.py`。

```python
from core.ipo_analyst_engine import IPOAnalystEngine

engine = IPOAnalystEngine()
report = engine.analyze(stock_code='01236', multi_source_data={}, validated_data={})
```

---

## 数据流

```
AKShare / Futu / Browser
        │
        ▼
  DataLayer（缓存 + 质量检查）
        │
        ▼
  FactorPipeline（22 因子并行计算）
        │
        ▼
  CompositeSignalEngine（IC 动态加权）
        │
        ├─▶ BacktestEngine（历史验证）
        │
        └─▶ StrategyRunner（实盘执行）
                    │
                    ▼
              RiskEngine（三层风控）
                    │
                    ▼
              BrokerAdapter（订单执行）
                    │
                    ▼
              AlertManager（飞书/钉钉推送）
```

---

## IPO Stars 港股打新模块（feature/ipo-stars）

```
触发 ──▶ IPOScanner 每日 09:00
                │
                ▼
    IPODataSource（东方财富 + 港交所 + 历史库）
                │
                ▼
    DataCrossValidator（多源交叉验证 + 数据质量评分）
                │
                ▼
    IPOAnalystEngine（5 个分析模块）
       ① 可比 IPO 定价锚点
       ② 机构持仓结构
       ③ 发行条款性价比
       ④ 市场窗口情绪
       ⑤ 挂单策略生成
                │
                ▼
    IPOAnalysisReport + IPORenderer ──▶ 飞书推送
```

**定位**：纯分析报告工具，不进入 PositionBook / 风控体系 / 操盘决策。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 数据获取 | AKShare、Futu OpenD、辉立暗盘（browser） |
| 存储 | SQLite（组合）、Parquet（历史数据/IPO） |
| HTTP API | Flask |
| 实时运行 | asyncio + threading |
| 因子框架 | 自研（22 因子） |
| ML | LightGBM + Walk-Forward |
| 优化 | scipy、numpy |
| 告警 | 飞书、钉钉 |
| 监控 | Prometheus + Grafana |
