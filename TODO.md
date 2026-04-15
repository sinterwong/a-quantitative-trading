# TODO — 量化交易系统开发路线图

> 最后更新：2026-04-15
> 当前综合评分：**68/100**（工程 85 + 策略 55 ÷ 2）
> 目标：商用级量化系统 → **85+/100**

---

## 当前系统评分卡

| 维度 | 得分 | 说明 |
|------|------|------|
| 工程完整性 | 85/100 | 模块化好，EventBus 除外 |
| 策略质量 | 55/100 | RSI 单因子，历史验证不足 |
| 数据质量 | 60/100 | 日线为主，缺 Tick/外盘 |
| 风险管理 | 75/100 | 止损/Kelly/熔断三层 |
| 可扩展性 | 65/100 | 函数耦合，难加新策略 |
| **综合** | **68/100** | 工程扎实，策略单薄 |

---

## 已完成里程碑

| 阶段 | 内容 | 核心文件 |
|------|------|---------|
| P0 | RSI WFA + ATR 过滤 | `backtest_cli.py` `signals.py` |
| P1 | Kelly 仓位 + 熔断机制 | `broker.py` `intraday_monitor.py` |
| P2 | 涨跌停 + 行业集中度 + 压力测试 | `signals.py` `broker.py` |
| P3 | 北向资金追踪 | `northbound.py` |
| P4 | 市场环境识别 + 策略组合 | `regime_detector.py` `strategy_ensemble.py` |
| P5 | 全自动 PaperTrade 闭环 | `morning_runner.py` `afternoon_report.py` |
| P6 | 绩效归因 + 参数自适应 WFA | `performance_report.py` `regime_wfa.py` |
| P7 | Chandelier Exit + 仓位上限 | `signals.py` `broker.py` |
| P8 | KAMT 多源缓存 | `data_cache.py` |

---

## 核心问题诊断

**策略端（最急需解决的）：**
- 只有 RSI 一个因子，无法多信号共振
- 没有外盘数据（S&P 期货 / VIX → A 股开盘跳空方向）
- 回测用日线，盘中用日线 → "偷价"嫌疑
- 没有 Tick 数据，订单簿信息完全缺失

**工程端（架构性缺陷）：**
- `signals.py` 硬编码 RSI/MACD → 加新策略要改核心
- `morning_runner.py` / `intraday_monitor.py` 通过函数调用耦合
- `PaperBroker` 内置在 OMS 逻辑里 → 接真实券商要重写
- 没有因子库，新策略不可复用

---

## Phase 1 · EventBus + FactorExpression（当前最优先级）✅ 进行中

> **目标**：把策略/风控/执行全部事件化，新策略注册即用，不碰核心
> **架构文件**：`core/event_bus.py` + `core/factors/` + `core/strategies/signal_engine.py`
> **状态**：✅ 完成（13/13 测试通过），已提交 `d150d85`

### ✅ 架构骨架（已实现）

**`core/event_bus.py`** — 事件总线（新增）
```python
class Event:
    type: str

class MarketEvent(Event): pass    # tick/bar 行情
class SignalEvent(Event): pass    # 因子信号
class OrderEvent(Event): pass     # 订单请求
class FillEvent(Event): pass      # 成交回报
class RiskEvent(Event): pass      # 风控预警

class EventBus:
    """单例模式，所有组件通过事件通信"""
    _handlers: Dict[str, List[Callable]]

    def emit(self, event: Event): ...
    def on(self, event_type, handler): ...
    def pipeline(self, *handlers): ...  # 链式处理
```

**`core/__init__.py`** — 新包入口
```
quant_repo/core/           # 新包根目录
  __init__.py
  event_bus.py
  oms.py
  risk_engine.py
  data_layer.py
  backtester.py
  factors/
    __init__.py
    base.py          # Factor 基类
    rsi.py
    macd.py
    atr.py
    bollinger.py
    north_flow.py
  strategies/
    __init__.py
    base.py          # Strategy 基类
    mean_reversion.py
    momentum.py
```

### ✅ Factor 基类

```python
class Factor(Protocol):
    """因子基类，所有因子实现此接口"""
    name: str
    category: FactorCategory  # PRICE_MOMENTUM / REGIME / FUNDAMENTAL / SENTIMENT / EXTERNAL

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        """返回因子值序列，索引 = data.index"""
        ...

class RSI(Factor):
    name = 'RSI'
    category = FactorCategory.PRICE_MOMENTUM
    def __init__(self, period: int = 14, buy: float = 30, sell: float = 70):
        ...

    def evaluate(self, data) -> pd.Series:
        return ...  # 返回 z-score 归一化的因子值

    def signals(self, factor_values: pd.Series) -> List[Signal]:
        """从因子值生成信号列表"""
        ...
```

### ✅ Signal 标准格式

```python
@dataclass
class Signal:
    timestamp: datetime
    symbol: str
    direction: Literal['BUY', 'SELL']
    strength: float           # 因子信号强度 0~1
    factor_name: str         # 来源因子
    metadata: dict           # 自由扩展

    @property
    def signal_key(self) -> str:
        return f"{self.symbol}:{self.direction}:{self.timestamp}"
```

### ✅ OMS 抽象层

```python
class BrokerAdapter(Protocol):
    """券商适配器接口，当前 PaperBroker 实现"""
    def send(self, order: Order) -> Fill: ...
    def cancel(self, order_id: str): ...
    def quote(self, symbol: str) -> Quote: ...

class PaperBroker(BrokerAdapter):
    """保留当前 PaperBroker 逻辑"""

class FutuBroker(BrokerAdapter):
    """富途适配器（Phase 2）"""

class OMS:
    def __init__(self, broker: BrokerAdapter):
        self.broker = broker
        self.event_bus = EventBus.global_bus()

    def submit(self, signal: Signal) -> Optional[Fill]:
        # PreTrade 风控检查 → broker.send → emit(FillEvent)
```

### ✅ RiskEngine 三层

```python
class RiskEngine:
    """三层风控：PreTrade / InTrade / PostTrade"""
    def check(self, signal: Signal) -> RiskResult: ...

    def check_pre_trade(self, signal, book) -> RiskResult:
        return RiskResult(
            passed=True,
            position_limit=book.position_pct(signal.symbol) < 0.25,   # 仓位上限
            loss_limit=book.today_pnl > -0.02,                    # 日亏 2% 熔断
            correlation=book.correlation(signal.symbol) < 0.7,      # 相关性
        )
```

---

## Phase 2 · 外盘数据 + OMS抽象层 ✅ 完成

> **状态**：完成，测试通过，已提交 `d150d85`
> **注意**：yfinance 在当前 IP 存在限速，外盘数据需等待解限或接入商业数据源（Bloomberg/Wind）

### 外盘数据源（DataSource 接口）

### 外部 Alpha 信号（A股最大缺失）

| 信号 | 数据源 | 逻辑 |
|------|--------|------|
| S&P 期货隔夜领先 | CME futures API | S&P 涨跌 → A 股开盘方向 |
| VIX 波动率预警 | CBOE API（fallback yfinance） | VIX>25 → A 股波动加大 |
| 恒指期货 | HKEX futures | 港股 → A 股情绪 |
| 北向分钟级 | cached_kamt() | 10:00 / 14:00 净流入 → 领先 30min |

### 已实现组件

**`core/data_sources.py`** — 统一数据源接口
**`core/oms.py`** — BrokerAdapter + PaperBroker + OMS（含 Kelly 仓位）
**`core/risk_engine.py`** — 三层风控（PreTrade/InTrade/PostTrade）

### 北向分钟级因子

```python
class NorthBoundMinuteFactor(Factor):
    """北向资金分钟粒度因子（当前只有日度，需要升级到分钟）"""
    category = FactorCategory.FUNDAMENTAL

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        # KAMT 每分钟北向累计净流入
        # → z-score 归一化
        # → 信号方向：>0 = 外资净买入 A 股
```

---

## Phase 3 · Broker 抽象层 + SafetyMode ✅ 完成

> **状态**：框架完成，真实券商为 STUB（不可调用），已提交 `ba2198f`

### Broker 架构

**SafetyMode 三级安全**：
```
PAPER ──► SIMULATED ──► LIVE
（当前）   （同PAPER）  （需三步解锁）

三步解锁 LIVE：
  1. config/brokers.json: {safety_mode: LIVE, broker: futu}
  2. env QUANT_LIVE_CONFIRM=1
  3. 文件 config/live_armed 存在
```

**已实现组件**：
- `core/brokers/facade.py` — BrokerFactory + SafetyMode
- `core/brokers/paper.py` — PaperBroker（生产可用）
- `core/brokers/futu.py` — 富途 STUB
- `core/brokers/tiger.py` — 老虎 STUB
- `core/brokers/ibkr.py` — IBKR STUB

**Phase 3 真实券商对接（预留）**

| 券商 | 状态 | 接口 |
|------|------|------|
| Futu OpenAPI | STUB | 港股 + A股通 |
| Tiger OpenAPI | STUB | A股通 |
| Interactive Brokers | STUB | 全球期货 |

接入条件：SafetyMode=LIVE + 三步解锁

---

## Phase 4 · Tick 数据 + 订单簿因子 ✅ 完成

> **状态**：完成，6/6 测试通过，已提交 `fd2b968`

### 已实现组件

**`core/level2.py`** — Level2 核心
- `Level2DataSource`: 新浪5档买卖盘口轮询（免费）
- `OrderBook`: 盘口数据模型（bid5/ask5）
- `TickBarAggregator`: tick→规则K线（time/volume/tick）
- `OrderImbalanceFactor`: OI订单不平衡度
- `BidAskSpreadFactor`: 买卖价差因子
- `MidPriceDriftFactor`: 中间价漂移
- `VolumeRateFactor`: 量比
- `AmihudIlliquidityFactor`: 非流动性（学术因子）

```python
class OrderFlowFactor(Factor):
    """订单簿因子（Level2 数据）：
    - 委托单不平衡度 (Order Imbalance)
    - VWAP 偏离度
    - 盘口价差因子
    当前系统缺失，这是 A 股最强的 alpha 源之一
    """
```

---

## Phase 6 · 回测引擎 + 因子研究框架 ✅ 完成

> **状态**：完成，5/5 测试通过，已提交 `f6169f3`

### 已实现组件

**`core/backtest_engine.py`**
- `BacktestEngine`: 事件驱动回测（三步API: load_data → add_strategy → run）
- `BacktestConfig`: commission/slippage/max_position 配置
- `BacktestResult`: Sharpe/Calmar/Sortino/最大回撤/胜率/IC/IR
- `PerformanceAnalyzer`: 因子绩效归因（按信号来源分层）

**`core/research.py`**
- `FactorResearcher`: 单因子多参数网格搜索（训练/测试分割）
- `WalkForwardAnalyzer`: WFA滚动窗口验证
- `FactorAnalysisResult`: IC/IR评估 + 状态分类（promising/stable/rejected）

用法：
```python
from core.research import FactorResearcher
researcher = FactorResearcher()
results = researcher.research(
    factor_class=RSIFactor,
    data={'TEST': df},
    param_grid={'period': [7,14,21], 'buy_threshold': [20,25,30]},
    train_days=504, test_days=252,
)
```

---

## 架构里程碑

```
Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6
EventBus       外盘数据    真实券商    Tick因子     组合优化   回测研究
FactorExpr     多因子共振              订单簿因子              因子研究

新增：HKStockDataSource (港股实时行情)
```

### 港股数据已接入 ✅

**`core/hk_data_source.py`** — 港股实时行情（新浪 hkXXXXX）
- 支持：腾讯(00700)/小米(01810)/阿里(09988)/美团/理想/恒指/恒科
- `HKStockDataSource('hk01810')` → 小米实时快照
- `to_orderBook()` → 兼容 Level2 订单簿因子（OI/Amihud等）
- 批量获取：`fetch_batch(['hk00700','hk01810'])`
- 轮询订阅：`subscribe(handler)` / `start_polling()`

**已知限制**：新浪港股历史K线返回 null，需后续接入专有数据源（TuShare Pro / Wind）

---

## 开发原则

1. **不重写已有稳定代码** — `core/` 是新增包，`scripts/` / `backend/` 完全不动
2. **EventBus 单例模式** — 全局 `EventBus.global_bus()` 广播，零配置集成
3. **Factor.evaluate() 返回 z-score** — 所有因子可比较、可加权
4. **回测代码 = 实盘代码** — 同一 Factor 接口，历史因子生成信号
5. **先有真实数据才有 alpha** — Phase 2 外盘数据优先于 Phase 1 工程

---

> 核心目标：6 个月从 68 分 → 80 分，核心路径是 EventBus 化 + 外盘数据。
