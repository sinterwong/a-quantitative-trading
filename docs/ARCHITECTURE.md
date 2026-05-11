# 系统架构

---

## 分层架构

```
┌──────────────────────────────────────────────────────────────────┐
│                    Web UI (Streamlit :8501)                     │
│  组合概览 · 实时信号 · 动态选股 · 回测分析 · 持仓详情 · 历史交易   │
└────────────────────────────┬─────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼─────────────────────────────────────┐
│               Backend Service (Flask :5555)                        │
│                                                                      │
│  ┌──────────────────────┐  ┌─────────────────────────────────┐  │
│  │ Scheduler            │  │ IntradayMonitor                  │  │
│  │ 每日 08:30–16:00 CST│  │ 盘中 5 分钟轮询 (09:30–15:00)    │  │
│  │ 节假日感知            │  │ RSI 二次确认 · 止盈/止损 · 飞书推送│  │
│  └──────────────────────┘  └─────────────────────────────────┘  │
└──────────────┬─────────────────────────────┬──────────────────────┘
               │                             │
┌──────────────▼──────────────┐  ┌───────────▼──────────────────────┐
│     数据网关 (DataGateway)    │  │   策略执行层 (StrategyRunner)    │
│  ├─ Tencent / Sina / Eastmoney│  │  ├─ AsyncStrategyRunner (asyncio)│
│  ├─ AkShare / YFinance        │  │  ├─ DynamicWeightPipeline         │
│  ├─ 健康度动态路由             │  │  ├─ 10+ 因子 IC 动态加权          │
│  ├─ 字段级多源合并            │  │  └─ Regime 自适应（4 种市场状态）  │
│  ├─ 熔断器 (per provider×cap) │  └────────────┬────────────────────┘
│  └─ 内存缓存 (TTL 30–86400s)  │               │
└─────────────────────────────┘  ┌────────────▼────────────────────┐
                                 │        因子层（10+ 个因子）       │
                                 │  技术(4) · 基本面(4) · 宏观(2)    │
                                 └────────────┬────────────────────┘
                                              │
         ┌────────────────────────────────────▼─────────────────────┐
         │                  执行与优化层                              │
         │  OMS · VWAP/TWAP · ImpactEstimator                       │
         │  PortfolioAllocator（多策略资金分配 + 再平衡）            │
         └────────────────────────────────────┬─────────────────────┘
                                              │
         ┌────────────────────────────────────▼─────────────────────┐
         │                   风控体系（三层）                         │
         │  RiskEngine (PreTrade/InTrade/PostTrade)                 │
         │  CVaR · MonteCarloStressTest (10000 次)                  │
         └────────────────────────────────────┬─────────────────────┘
                                              │
         ┌────────────────────────────────────▼─────────────────────┐
         │                        券商适配层                          │
         │  SimulatedBroker · FutuBroker · IBKRBroker(stub)        │
         └──────────────────────────────────────────────────────────┘
```

---

## 数据网关（DataGateway）

`core/data_gateway/` 是全系统对外网数据的唯一出口，内部无任何业务逻辑。

### 架构

```
业务侧调用
    │
    ▼
DataGateway  ← 薄外观（core/data_layer.py 转发至此）
    │
    ├─ HealthTracker       健康度滑窗评分，按 (provider×capability) 排序
    ├─ CircuitBreaker     熔断器，失败累计触发硬开关
    ├─ MemoryCache         内存缓存，TTL 按数据类型区分
    │
    ▼
Provider 注册表
    ├─ TencentProvider    qt.gtimg.cn / web.ifzq.gtimg.cn（主选，字段最全）
    ├─ SinaProvider        sina.com.cn（实时行情备用）
    ├─ EastmoneyProvider   eastmoney.com（板块/资金流/指数）
    ├─ AkShareProvider     akshare.net（最终备灾，稳定性差）
    └─ YFinanceProvider    yfinance（美股/港股指数）
```

### 选源策略

**可合并数据类型**（Quote 基本面）：并发问 top-K provider，字段级互补合并

**不可合并类型**（K 线/板块/北向等）：按健康度降序逐个尝试，第一个成功即返回

### 缓存策略

| 数据类型 | TTL |
|---------|-----|
| 实时行情 Quote | 30s |
| 基本面数据 | 60s |
| 板块排名/成分股 | 60s |
| 北向资金/指数 | 60s |
| 日 K 线 | 300s（网络）/ 就近归档（Parquet）|
| 宏观数据 | 24h |
| 基本面历史时序 | 24h |

---

## 因子流水线

`core/pipeline_factory.py` 构建 `DynamicWeightPipeline`，供 StrategyRunner 和回测共用。

### 因子构成

**技术因子（must-have）**

| 因子 | 权重 |
|------|------|
| RSIFactor | 0.20 |
| MACDTrendFactor | 0.20 |
| BollingerFactor | 0.15 |
| ATRFactor | 0.10 |

**基本面因子**

| 因子 | 权重 |
|------|------|
| PEPercentileFactor | 0.10 |
| ROEMomentumFactor | 0.10 |
| RevenueGrowthFactor | 0.05 |
| CashFlowQualityFactor | 0.05 |

**宏观因子**

| 因子 | 权重 |
|------|------|
| PMIFactor | 0.05 |
| M2GrowthFactor | 0.05 |

### 动态权重

`DynamicWeightPipeline` 每 21 个交易日根据滚动 IC（63 天窗口）重新分配权重。连续 3 次 IC<0 的因子自动清零，IC 转正后以 50% 等权重复活。

---

## 策略运行

`StrategyRunner` 每 5 分钟执行一次 pipeline：

1. 获取标的列表（持仓 ∪ watchlist）
2. 调用 `DynamicWeightPipeline` 获取 `combined_score`
3. Regime 检测（CALM / BULL / BEAR / VOLATILE）
4. 输出 `BUY` / `SELL` / `HOLD`

`IntradayMonitor` 在此基础上做 RSI 二次确认，决定是否真实触发下单。

---

## 设计原则

1. **回测 = 实盘**：同一因子接口、同一信号格式，回测引擎和实盘运行器共享 `FactorPipeline`。
2. **数据源透明**：`DataGateway` 记录每条数据的 provenance（来源 provider），可追溯。
3. **券商适配层**：`BrokerAdapter` 接口抽象所有券商，切换只需替换适配器实例。

---

## 关键模块

### 数据网关 (`core/data_gateway/`)

```python
from core.data_gateway import get_gateway

gw = get_gateway()
quote = gw.quote('000001.SH')          # 实时行情（字段级合并）
kline = gw.kline('000001.SH', days=120)  # 日 K 线
sectors = gw.sectors(limit=50)        # 板块排名
north = gw.north_flow()               # 北向资金
```

### 因子流水线 (`core/pipeline_factory.py`)

```python
from core.pipeline_factory import build_pipeline

pipeline = build_pipeline(symbol="000001.SH")
result = pipeline.run(symbol="000001.SH", data=df, price=current_price)
```

### 风控 (`core/risk_engine.py`)

三层风控：PreTrade（下单前）、InTrade（持仓中）、PostTrade（收盘后）。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 数据获取 | 腾讯 / 新浪 / 东方财富 / AkShare / YFinance |
| 存储 | SQLite（组合）、Parquet（历史 K 线/IPO） |
| HTTP API | Flask |
| 实时运行 | asyncio + threading |
| 因子框架 | 自研（10+ 因子） |
| ML | LightGBM + Walk-Forward |
| 告警 | 飞书 |
