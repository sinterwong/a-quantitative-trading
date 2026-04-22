# Quant Trading System — 商用级架构设计规范

> 目标：支撑 A 股 + 港股 + 美股，策略可组合，风控可配置，回测/实盘同一套代码
> 当前阶段：工程完整度 85分，策略深度 55分 → 架构重构 → 90分工程 + 75分策略

---

## 一、当前架构的问题诊断

### 1.1 结构性缺陷（不重构无法走向商用）

```
问题               影响               根因
────────────────────────────────────────────────────────
策略耦合在脚本里    无法多策略组合      RSI/MACD 硬编码在 signals.py
日线信号盘中用      信号质量差/偷价     数据层缺失 tick 处理
回测/实盘分离      策略到实盘差异巨大  没有统一信号接口
风控只在持仓层面    无法管理相关性      缺组合层面的风控引擎
没有因子框架        RSI 只能靠参数调    因子=特征×权重 才是可组合的
SQLite 日志         无法支撑毫秒级      需 TimescaleDB / Dolphin
订单管理内置在 Broker  无法券商解耦       需 OMS 抽象层
```

### 1.2 当前系统的层次（好与坏）

```
层                       当前状态    问题
─────────────────────────────────────────
数据获取 (data_loader)     ⚠️ 碎片化   各脚本独立，数据不统一
信号生成 (signals.py)     ⚠️ 硬编码   RSI/MACD 写死，无法组合
组合优化 (broker.py)      ⚠️ Kelly 基础  无 Black-Litterman，无协方差
风控 (broker.py intraday) ⚠️ 单持仓   缺组合层面 VaR/希谢
执行层 (PaperBroker)      ❌ 模拟    真实订单执行差异巨大
报告层                   ✅ 完整     飞书/日志/归因都有了
```

---

## 二、商用架构设计

### 2.1 核心理念

```
三个核心原则：
1. 因子 × 信号 × 组合 × 执行 完全解耦
2. 回测代码 = 实盘代码（同一因子/信号接口）
3. 一切皆事件（EventBus 作为中央总线）
```

### 2.2 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                    StrategyRunner (事件循环)                  │
│          主进程：连接 DataBus / EventBus / RiskEngine          │
└───────────────────────┬─────────────────────────────────────┘
                        │ EventBus (ZeroMQ / asyncio)
          ┌─────────────┼──────────────────┐
          ▼             ▼                  ▼
┌──────────────┐  ┌──────────┐  ┌─────────────────────────┐
│ DataLayer    │  │ Signal   │  │ RiskEngine              │
│ (Tick/Daily │  │ Engine   │  │ (预检/实时监控/止损执行)  │
│  FactorDB)   │  │ (因子表达│  │                         │
│              │  │ 多信号组│  │ • PreTrade: 持仓限制/净暴露│
│  · Tick     │  │ 合/生成 │  │ • RealTime: VaR/希谢/相关性│
│  · DailyBar  │  │ 信号)   │  │ • PostTrade: 绩效归因      │
│  · News     │  │         │  │ • Alert: 飞书/钉钉推送   │
│  · 北向资金 │  └──────────┘  └─────────────────────────┘
└──────────────┘
          ▲             ▲                  ▲
          │             │                  │
   ┌──────┴──────┐    │                  │
   │ Backtester   │    │                  │
   │ (因子表达式) │◄───┘                  │
   │ 历史数据回测 │                      │
   │ 事件驱动重现 │──────────────────────┘
   └─────────────┘
```

---

## 三、核心模块设计

### 3.1 EventBus（事件总线）

```python
class EventBus:
    """
    所有组件通过事件通信，解耦策略/风控/执行/数据。
    事件类型：
      - MarketEvent:       tick/bar 数据
      - SignalEvent:       因子信号
      - OrderEvent:       订单请求
      - FillEvent:        成交回报
      - RiskEvent:        风控预警
      - AlertEvent:       通知事件
    """
    def publish(event: Event): ...
    def subscribe(event_type, handler): ...
    def create_pipeline(filters: List[SignalFilter], strategy: Strategy): ...
```

**为什么重要：** 当前系统的 signals.py / broker.py / intraday_monitor.py 通过函数调用耦合，
无法并行执行多个策略。EventBus 让所有策略/风控/执行都是独立消费者。

### 3.2 FactorExpression（因子表达式系统）

```python
class Factor:
    """因子基类"""
    def evaluate(self, market_data: pd.DataFrame) -> pd.Series: ...

class RSI(Factor):
    windows = [14, 28]
    def evaluate(self, data):
        return ta.rsi(data['close'], self.windows)

class MACD(Factor):
    def evaluate(self, data):
        return ta.macd(data['close'])

class CompositeFactor(Factor):
    """因子组合"""
    def __init__(self, factors: List[Factor], weights: List[float]):
        self.factors = factors
        self.weights = weights
    def evaluate(self, data):
        signals = [f.evaluate(data) for f in self.factors]
        return sum(w * s for w, s in zip(self.weights, signals))


class SignalGenerator:
    """
    策略 = 因子 × 信号阈值 × 过滤条件
    所有策略通过因子表达，回测和实盘同一套代码
    """
    def __init__(self, factor: Factor, rules: List[SignalRule]):
        self.factor = factor
        self.rules = rules

    def generate(self, market_data) -> List[Signal]:
        # 因子值 × 规则 → 信号列表
        factor_values = self.factor.evaluate(market_data)
        signals = []
        for rule in self.rules:
            if rule.match(factor_values):
                signals.append(rule.create_signal(factor_values))
        return signals
```

**当前系统对应：**
- `Factor` = `signals.py` 的 `evaluate_signal()` 系列
- `SignalGenerator` = `signals.py` 的 `evaluate_signal()` + 阈值逻辑
- **重构价值**：可以把 RSI/MACD/布林带/北向资金/情绪因子全部注册到因子库，
  StrategyRunner 根据配置组合，不需要每个策略写一个 signals.py

### 3.3 OrderManagementSystem（订单管理系统）

```python
class OMS:
    """
    统一订单管理，支持多券商解耦。
    当前 PaperBroker → 未来: FutuOS / Tiger / Alpaca / Interactive Brokers
    """
    def __init__(self, broker: BrokerAdapter):
        self.broker = broker
        self.pending_orders: Dict[str, Order] = {}
        self.position_book: Dict[str, Position] = {}

    def submit(self, signal: Signal, broker: str = 'paper') -> Order:
        # 1. 风控预检
        risk_check = risk_engine.check(signal)
        if not risk_check.passed:
            raise RiskRejectedError(risk_check.reason)

        # 2. 转换为券商订单
        order = self.broker.to_order(signal)

        # 3. 发送至券商
        filled = self.broker.send(order)

        # 4. 更新持仓账本
        self.position_book[order.symbol] = filled
        return filled

    def cancel(self, order_id: str): ...
    def get_positions(self) -> List[Position]: ...
    def get_pending_orders(self) -> List[Order]: ...


class BrokerAdapter(Protocol):
    """券商适配器接口"""
    def send(self, order: Order) -> Fill: ...
    def cancel(self, order_id: str): ...
    def get_quote(self, symbol: str) -> Quote: ...


# 当前: PaperBroker → 未来适配器
class PaperBroker(BrokerAdapter): ...
class FutuBroker(BrokerAdapter): ...   #  富途
class TigerBroker(BrokerAdapter): ...  #  老虎
class IBBroker(BrokerAdapter): ...       #  IBKR
```

### 3.4 PortfolioOptimizer（组合优化器）

```python
class PortfolioOptimizer:
    """
    Mean-Variance 组合优化 + Black-Litterman 观点。
    当前 Kelly 半仓 → BL 权重让系统可配置多策略分配
    """
    def __init__(self, method: str = 'equal_weight'):
        self.method = method

    def optimize(
        self,
        signals: Dict[str, float],       # {symbol: signal_strength}
        positions: Dict[str, Position],    # 现有持仓
        cov_matrix: np.ndarray,            # 收益协方差矩阵
        risk_aversion: float = 1.0,
    ) -> Dict[str, float]:
        """
        Returns: {symbol: target_weight}
        """
        if self.method == 'kelly_half':
            return self._kelly_half(signals)
        elif self.method == 'black_litterman':
            return self._black_litterman(signals, cov_matrix)
        elif self.method == 'risk_parity':
            return self._risk_parity(signals, cov_matrix)
        raise ValueError(f'Unknown method: {self.method}')
```

### 3.5 RiskEngine（风控引擎）

```python
class RiskEngine:
    """
    三层风控：
    1. PreTrade: 下单前检查（持仓限制/净暴露/单标的仓位上限）
    2. RealTime: 持仓期间监控（VaR/希谢比率/相关性/止损）
    3. PostTrade: 成交后的组合层面风控
    """
    def check_pre_trade(self, order: Order, book: PositionBook) -> RiskResult:
        checks = [
            self._check_position_limit(order, book),    # 单标的 ≤ 25%
            self._check_net_exposure(order, book),        # 总净暴露 ≤ 90%
            self._check_concentration(order, book),       # 行业集中度 ≤ 30%
            self._check_margin_ratio(order, book),        # 保证金比率
            self._check_loss_limit(order, book),         # 日亏损熔断
        ]
        failed = [c for c in checks if not c.passed]
        return RiskResult(passed=not failed, reasons=[c.reason for c in failed])

    def _check_correlation_risk(self, book: PositionBook):
        """协方差矩阵检测：组合总 VaR（当前缺失的！"""
        # 计算: σ_portfolio = sqrt(w^T × Σ × w)
        # VaR_99% = z_score * σ_portfolio * position_value
        # 超过阈值 → 触发减仓
        ...

    def monitor_intraday(self, book: PositionBook, prices: Dict[str, float]):
        # 实时止损检查（Chandelier Exit）
        for pos in book.positions:
            if self._check_atr_trailing_stop(pos, prices):
                yield AlertEvent(symbol=pos.symbol, reason='ATR_STOP')
```

### 3.6 DataLayer（数据层）

```python
class DataLayer:
    """
    统一数据抽象：
    - 历史数据 (PostgreSQL / Dolphin)
    - 实时行情 (WebSocket)
    - 新闻/舆情 (API)
    - 北向资金 (EastMoney)
    - 外盘期货 ( Quandl / Bloomberg )
    """
    def __init__(self):
        self.tick_db: TickDB      # TimescaleDB (毫秒级)
        self.bar_db: BarDB         # PostgreSQL (分钟/日线)
        self.cache: RedisCache     # 60s TTL
        self.stream: WebSocketPool # 实时行情订阅

    async def subscribe(self, symbols: List[str], handler: Callable):
        """实时行情订阅 → EventBus.publish(MarketEvent)"""
        ...

    def get_bars(self, symbol: str, start, end, freq='1min') -> pd.DataFrame:
        """统一接口：回测和实盘都用这个获取 K 线"""
        if freq == 'tick':
            return self.tick_db.query(symbol, start, end)
        return self.bar_db.query(symbol, start, end, freq)


class TickDB:
    """
    TimescaleDB 超表：
    - 毫秒级 A 股 tick 数据（2010-至今）
    - 压缩存储，保留 30 天热数据
    """
    def query(self, symbol, start, end) -> pd.DataFrame:
        return self.engine.execute(
            f"SELECT time, last FROM ticks WHERE symbol='{symbol}' AND time BETWEEN '{start}' AND '{end}'"
        )
```

### 3.7 StrategyRunner（主循环）

```python
class StrategyRunner:
    """
    事件驱动主循环，替代 cron 调度 + 脚本模式。
    三种运行模式：
      - backtest:   读取历史数据，事件重现
      - paper:      PaperBroker，真实行情模拟
      - live:       连接券商，真实订单
    """
    def __init__(self, mode: str, config: Config):
        self.mode = mode
        self.event_bus = EventBus()
        self.data = DataLayer(config)
        self.risk = RiskEngine(config)
        self.oms = OMS(config.broker)
        self.strategies: List[Strategy] = []

    async def start(self):
        if self.mode == 'backtest':
            await self._run_backtest()
        elif self.mode == 'live':
            await self._run_live()

    async def _run_live(self):
        """实时行情驱动"""
        # 1. 订阅实时 bar 数据
        await self.data.subscribe(self.symbols, self._on_market_data)
        # 2. 每分钟触发信号生成
        self.event_bus.subscribe(SignalEvent, self._on_signal)
        # 3. 订单路由
        self.event_bus.subscribe(OrderEvent, self._on_order)
        # 4. 风控监控
        self.event_bus.subscribe(FillEvent, self._on_fill)

    def _on_market_data(self, event: MarketEvent):
        for strategy in self.strategies:
            signals = strategy.generate(event.data)
            for sig in signals:
                self.event_bus.publish(SignalEvent(signal=sig))

    def _on_signal(self, event: SignalEvent):
        risk_result = self.risk.check_pre_trade(event.signal)
        if not risk_result.passed:
            return  # 风控拒绝，静默跳过
        self.oms.submit(event.signal)
```

---

## 四、因子研究框架

### 4.1 因子分类

```python
# 因子分为五类，可任意组合
class FactorCategory(Enum):
    PRICE_MOMENTUM = auto()   # 价量动量：RSI/MACD/布林带/价格
    FUNDAMENTAL = auto()       # 基本面：PE/PB/北向持仓/分析师预期
    SENTIMENT = auto()         # 情绪：新闻/舆情/资金流
    REGIME = auto()           # 环境：波动率/趋势/利率/汇率
    EXTERNAL = auto()          # 外部：美股期货/VIX/港股/KAMT

# 当前系统因子映射（Phase 2 更新）
FACTOR_MAP = {
    # ── 已接入 FactorRegistry（可通过 pipeline.add("Name") 使用）──
    'RSI(14)':           (FactorCategory.PRICE_MOMENTUM, 'core/factors/price_momentum.py RSIFactor'),
    'MACD(12,26,9)':    (FactorCategory.PRICE_MOMENTUM, 'core/factors/price_momentum.py MACDFactor'),
    'BollingerBands':    (FactorCategory.PRICE_MOMENTUM, 'core/factors/price_momentum.py BollingerFactor'),
    'ATR':               (FactorCategory.REGIME,         'core/factors/price_momentum.py ATRFactor'),
    'OrderImbalance':    (FactorCategory.PRICE_MOMENTUM, 'core/factors/price_momentum.py OrderImbalanceFactor'),  # P2-B
    # ── 趋势策略因子（直接接 WFA）──
    'MACDTrend':         (FactorCategory.PRICE_MOMENTUM, 'core/strategies/macd_trend.py MACDTrendFactor'),  # P2-A
    # ── 待接入 ──
    '北向资金':          (FactorCategory.FUNDAMENTAL,    'backend/services/northbound.py'),
    'News_sentiment':    (FactorCategory.SENTIMENT,      'scripts/quant/news_scorer.py'),
}
```

### 4.2 多因子模型

```python
class MultiFactorModel:
    """
    当前: RSI(25) 单一信号
    未来: 多因子加权信号 = Σ wi × fi / σi
    """
    def __init__(self, factors: List[Factor], weights: List[float]):
        self.factors = factors
        self.weights = weights  # 可用 Black-Litterman 动态更新

    def score(self, data: pd.DataFrame) -> pd.Series:
        """
        返回: 多因子综合评分（标准分数，z-score 归一化）
        """
        scores = []
        for factor, w in zip(self.factors, self.weights):
            raw = factor.evaluate(data)
            zscore = (raw - raw.mean()) / raw.std()
            scores.append(w * zscore)
        return sum(scores)
```

---

## 五、架构迁移路径（增量重构，不推倒重来）

### 原则：保留已有工程完整性，增量接入新架构

```
当前脚本模式          目标架构           迁移策略
──────────────────────────────────────────────────────
signals.py         FactorExpression    新增 SignalGenerator，
                                  signals.py 作为第一个 Factor

morning_runner.py  StrategyRunner     迁移到事件驱动，
                                  保留早报逻辑作为 scheduled task

afternoon_report  StrategyRunner     迁移到 OnBar 事件，
                                  收盘后自动触发

PaperBroker       BrokerAdapter      保持 PaperBroker 作为
                                  paper 模式适配器，
                                  新增 FutuAdapter

intraday_monitor  RiskEngine        迁移 PreTrade/Risk 检查
                                  到 RiskEngine.check_pre_trade

dynamic_selector  FactorLibrary     选股作为 Filter 因子，
                                  接入 EventBus

daily_journal    PostTradeEngine   保留，作为归因输出
```

### 5.1 Phase 1: EventBus + FactorExpression（新架构骨架）

```python
# 新文件: core/event_bus.py
class Event:
    type: str

class MarketEvent(Event): ...
class SignalEvent(Event): ...
class OrderEvent(Event): ...

class EventBus:
    _handlers: Dict[str, List[Callable]]
    def emit(self, event: Event): ...
    def on(self, event_type, handler): ...
```

```python
# 新文件: core/factors/rsi.py
class RSIFactor(Factor):
    def evaluate(self, data) -> pd.Series:
        return ta.rsi(data['close'], window=self.period)
```

**产出**: `core/event_bus.py` + `core/factors/` (RSI/MACD/ATR/Bollinger)

### 5.2 Phase 2: OMS + RiskEngine

```python
# 新文件: core/oms.py
class OMS:
    # 保留 PaperBroker 逻辑
    # 新增 BrokerAdapter 接口
    def submit(order: Order) -> Fill: ...
```

**产出**: `core/oms.py` + `core/risk_engine.py`

### 5.3 Phase 3: DataLayer + Backtester

```python
# 新文件: core/data_layer.py
class TickDB:
    # 用 SQLite + 文件缓存（避免引入 TimescaleDB 依赖）
    # 未来迁移 PostgreSQL
```

**产出**: `core/data_layer.py` + `core/backtester.py`

### 5.4 Phase 4: StrategyRunner 整合

```python
# 新文件: strategy_runner.py
# 替换 morning_runner.py + intraday_monitor.py
```

**最终架构**:
```
core/
  event_bus.py        # 事件总线
  oms.py            # 订单管理 + Broker 抽象
  risk_engine.py     # 风控引擎
  data_layer.py      # 数据层（含 ParquetCache + 分钟K线）
  backtest_engine.py # 事件驱动回测（修复前视偏差/印花税/Kelly）
  walkforward.py     # Walk-Forward 分析（≥5窗口 + 参数热力图）
  data_quality.py    # 数据质量检验
  regime.py          # 市场环境检测（BULL/BEAR/VOLATILE/CALM）  ← P2-E
  factor_registry.py # 因子注册表（registry.create("RSI", ...)）
  factor_pipeline.py # 因子流水线（加权合成 + signals 汇总）
  strategy_runner.py # 策略主循环（regime_aware Regime联动）  ← P2-E
  factors/           # 因子库
    base.py           # Factor(ABC) + Signal + FactorCategory
    price_momentum.py # RSI/MACD/Bollinger/ATR/OrderImbalance  ← P2-B
  strategies/        # 策略模块
    signal_engine.py  # 单/多因子信号引擎
    macd_trend.py     # MACDTrendFactor（ATR过滤，接入WFA）  ← P2-A
scripts/
  morning_runner.py  # 重写为 StrategyRunner 调用
  afternoon_report.py
backend/
  services/          # 保留飞书推送 / northbound
  broker.py           # 保留 PaperBroker
```

---

## 六、关键决策

### Q1: 为什么不用 Backtrader/vnpy/QuantConnect?

| 框架 | 优点 | 缺点 |
|------|------|------|
| Backtrader | 成熟 | 策略写死在框架里，无法解耦 |
| vnpy | 中文社区 | 过度封装，定制困难 |
| QuantConnect | 云端完整 | 不开源，数据隐私 |
| **自研** | 完全可控/可定制 | 工程量大 |

**选择**: 在 Backtrader 理念基础上自研核心 EventBus + FactorExpression，
保留现有 signals.py / broker.py / portfolio.py，逐步迁移

### Q2: 数据存储：SQLite → TimescaleDB？

**现状**: 一切基于 API，内存/文件缓存，无持久化 tick
**路径**: SQLite → PostgreSQL → TimescaleDB（3年后）
**理由**: A 股 tick 数据量：每秒 ~100 条，1天 ≈ 500MB，
TimescaleDB 压缩后 1天 ≈ 50MB，可接受

### Q3: 策略未来扩展？

```
当前：RSI 单信号
未来扩展顺序：
  1. MACD + RSI 双因子（已有框架）
  2. 北向资金共振（已有数据）
  3. 外盘领先信号（VIX / 美股期货）
  4. 期权波动率曲面
  5. 统计套利（ETF 溢价）
```

---

## 七、真实 Alpha 来源分析（A 股）

| Alpha 来源 | 可行性 | 当前系统 |
|-----------|--------|---------|
| RSI 均值回归 | ⚠️ 有上限 | ✅ RSI(25/65) |
| ATR 波动过滤 | ⚠️ 拥挤 | ✅ ATR ratio |
| 北向资金领先 | ⚠️ 数据质量 | ⚠️ KAMT 单位存疑 |
| 外盘领先信号 | ✅ 强逻辑 | ❌ 未接入 |
| 新闻舆情 | ⚠️ 非结构化 | ⚠️ 关键词打分 |
| 分析师预期 | ✅ 有数据库 | ❌ 未接入 |
| 订单流 (OrderFlow) | ✅ 最强 | ❌ 无 Level2 数据 |

**最重要的真实 Alpha（当前系统缺失）：**
1. **美股期货隔夜领先 A 股开盘** — S&P 期货 / Nasdaq 期货 → A 股 9:30 开盘跳空
2. **北向资金日内领先信号** — 10:00 / 14:00 的 KAMT 变化 → 领先个股 30 分钟
3. **波动率曲面** — IV / RV 价差 → 择时能力

---

## 八、架构评估

| 维度 | 当前 | 目标架构后 | 路径难度 |
|------|------|-----------|--------|
| 工程完整性 | 85/100 | 90/100 | 中（EventBus 重构）|
| 策略可组合性 | 30/100 | 75/100 | 高（需要因子研究）|
| 数据质量 | 55/100 | 75/100 | 高（需要 Tick 数据）|
| 风控层次 | 60/100 | 80/100 | 低（已有逻辑）|
| 执行层 | 40/100 | 70/100 | 中（需要券商适配）|
| 组合优化 | 20/100 | 65/100 | 高（需要因子协方差）|

**核心建议**：
1. 先把 `EventBus` + `FactorExpression` 做出来（Phase 1），这是所有其余模块的骨架
2. 同时接入外盘数据（S&P 期货 / VIX），这是目前最大的 alpha 缺失
3. 把 RSI/MACD/Bollinger 写成因子表达式注册到库，这是可复用的基础
