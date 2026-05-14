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
│  │ 每日 09:30–16:00 CST│  │ 盘中 5 分钟轮询 (09:30–15:00)    │  │
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
    ├─ SinaProvider        hq.sinajs.cn / money.finance.sina.com.cn（实时行情）
    ├─ EastmoneyProvider   push2.eastmoney.com（板块/北向资金）
    ├─ BaostockProvider    api.baostock.com（A股基本面+日K+资产负债表，免费无Token）
    ├─ AkShareProvider     akshare.net（宏观/基本面历史，最终备灾）
    └─ YFinanceProvider    yfinance（美股/港股指数，兜底）
```

### Provider × Capability 能力矩阵

| Capability | Tencent | Sina | Eastmoney | **Baostock** | AkShare | Yfinance |
|---|---|---|---|---|---|---|
| **QUOTE** | ✅ A/HK/INDEX/US | ✅ A/HK/INDEX | ✅ A/HK/INDEX | ❌ | ❌ | ❌ |
| **KLINE_DAILY** | ✅ A/HK/INDEX | ✅ A | ❌ | ✅ A股（第三备源） | ❌ | ✅ US/GLOBAL |
| **KLINE_MINUTE** | ✅ HK | ✅ A | ❌ | ❌ | ❌ | ❌ |
| **MARKET_INDEX** | ✅ A/HK/INDEX/US | ✅ A/INDEX | ✅ A/HK/INDEX | ❌ | ❌ | ✅ US/GLOBAL |
| **FUNDAMENTALS** | ❌ | ❌ | ❌ | ✅ A股（YoY增速/行业分类） | ✅ GLOBAL | ❌ |
| **FUNDAMENTALS_HISTORY** | ❌ | ❌ | ❌ | ✅ A股 | ✅ GLOBAL | ❌ |
| **BALANCE_SHEET** | ❌ | ❌ | ❌ | ✅ A股（资产负债率/流动/速动比率） | ❌ | ❌ |
| **SECTOR_RANKING** | ❌ | ❌ | ✅ GLOBAL（唯一） | ❌ | ❌ | ❌ |
| **SECTOR_CONSTITUENTS** | ❌ | ❌ | ✅ GLOBAL（唯一） | ❌ | ❌ | ❌ |
| **NORTH_FLOW** | ❌ | ❌ | ✅ GLOBAL（唯一） | ❌ | ❌ | ❌ |
| **MACRO** | ❌ | ❌ | ❌ | ❌ | ✅ GLOBAL | ❌ |

### 已知能力缺口（未实现）

| 优先级 | 缺口 | 说明 |
|---|---|---|
| **P0** | Sina → INDEX K-line | `normalize_to_sina("000300")` 深交所路径归一错误，腾讯已全覆盖，Sina 已排除 |
| **P0** | Tencent → US KLINE_DAILY | `web.ifzq.gtimg.cn` 接口仅返回1条历史数据，yfinance 已独家承接 |
| **P1** | AkShare → 港股实时行情 | 所有港股行情接口（stock_zh_a_spot_em 等）均 ConnectionError，依赖腾讯/东方财富/新浪三家兜底 |
| **P1** | Eastmoney → KLINE_DAILY | `push2his.eastmoney.com` RemoteDisconnected，接口被封禁，无法绕过 |

### 选源策略

**可合并数据类型**（Quote、Fundamentals）：并发问 top-K provider，字段级互补合并

```
请求 quote:sh600519
  → 并发问 Tencent + Sina（top-2）
  → 各返回一份 Quote dataclass
  → merge_field_level() 对每字段独立选最优来源
     score = provider_health × field_authority
  → 合并成一份完整 Quote
```

**不可合并类型**（K线/板块/北向等）：按健康度降序逐个尝试，第一个成功即返回

### 字段权威声明

Provider 可声明对特定字段的权威度权重（默认 1.0），影响字段级合并时的优选：

| Provider | 字段 | 权威权重 |
|---|---|---|
| Tencent | `pe_ttm / pb / market_cap / float_cap / high_52w / low_52w` | 1.3 |
| Tencent | `turnover_rate / amplitude / limit_up / limit_down` | 1.2 |
| Sina | `bid1_price / bid1_vol / ask1_price / ask1_vol` | 1.2 |

### 缓存策略

| 数据类型 | TTL |
|---------|-----|
| 实时行情 Quote | 30s |
| 基本面数据 | 60s |
| 板块排名/成分股 | 60s |
| 北向资金/指数 | 60s |
| 日 K 线 | 300s |
| 分钟 K 线 | 60s |
| 宏观数据 | 24h |
| 基本面历史时序 | 24h |

### 设计原则

- **Provider 声明"能力巨大"**：子类各取所需，`declare()` 声明能支持的全部能力，gateway 按需路由
- **Schema 与数据源解耦**：`schemas.py` 定义系统自身需要的数据形态，Provider 负责将原始字段映射到契约
- **冷启动评分**：`priority_hint` 字段决定无历史数据时的初始排序（腾讯/新浪 0.80+，东方财富 0.55，akshare 0.30）

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
quote = gw.quote('600519.SH')                        # 实时行情（字段级合并）
kline_day = gw.kline('600519.SH', interval='daily', days=120)   # 日K
kline_min = gw.kline('00700.HK', interval='5m', limit=100)      # 分钟K（仅HK）
index = gw.market_index('sh000001')                  # 指数快照
sectors = gw.sectors(limit=50)                       # 板块排名
north = gw.north_flow()                             # 北向资金
macro = gw.macro('PMI')                             # 宏观数据（MacroIndicator.PMI）
fundamentals = gw.fundamentals('600519.SH')          # 基本面快照
fundamentals_history = gw.fundamentals_history('600519.SH')  # 基本面历史时序
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
