# core — 商用级量化系统核心包
"""
EventBus + FactorExpression + OMS + RiskEngine + DataSources

Phase 1 (✅): EventBus + FactorExpression + SignalEngine
Phase 2 (✅): DataSources (SP期货/VIX/恒指/北向) + OMS抽象层 + RiskEngine
Phase 3 (✅): BrokerFactory + SafetyMode + 真实券商 STUB
Phase 4 (✅): Level2 数据源 + 订单簿因子
Phase 5 (✅): 组合优化器 (BL + MeanVariance + RiskParity)
Phase 6 (✅): 回测引擎 + 因子研究框架

──────────────────────────────────────────────────────────────────
架构现状（重要）：
  - 当前生产链路（Scheduler → IntradayMonitor → backend.services.broker.PaperBroker）
    采用同步阻塞调用，未启用事件驱动。
  - 本包导出的 EventBus / OMS / SignalEngine / EventDrivenPaperBroker
    是为事件驱动模式预留的高级 API，目前仅在 paper_trade_validator、
    streamlit_app 与单测中实例化，主交易链路不消费。
  - 命名注意：
      from core.oms import EventDrivenPaperBroker     # 事件驱动版
      from backend.services.broker import PaperBroker # 生产用
    两者不可互换。
──────────────────────────────────────────────────────────────────
"""

from core.event_bus import (
    EventBus, Event, MarketEvent, SignalEvent,
    OrderEvent, FillEvent, RiskEvent, AlertEvent,
)
from core.factors.base import Factor, FactorCategory, Signal
from core.factors.price_momentum import RSIFactor, BollingerFactor, MACDFactor, ATRFactor
from core.strategies.signal_engine import SignalEngine, CompositeSignalEngine
from core.oms import OMS, EventDrivenPaperBroker, Order, Fill, Position, BrokerAdapter
from core.risk_engine import RiskEngine, RiskResult, RiskPosition, PositionBook
from core.data_sources import (
    MarketSnapshot,
    NorthBoundDataSource,
)
from core.level2 import (
    Level2DataSource, OrderBook, TickBarAggregator, TickBar,
    OrderImbalanceFactor, BidAskSpreadFactor, MidPriceDriftFactor,
    VolumeRateFactor, AmihudIlliquidityFactor,
)
from core.backtest_engine import (
    BacktestEngine, BacktestConfig, BacktestResult,
    PerformanceAnalyzer, TradeRecord, PositionSnapshot,
)
from core.research import (
    FactorResearcher, WalkForwardAnalyzer, FactorAnalysisResult,
)

__all__ = [
    # EventBus
    'EventBus', 'Event', 'MarketEvent', 'SignalEvent',
    'OrderEvent', 'FillEvent', 'RiskEvent', 'AlertEvent',
    # Factors
    'Factor', 'FactorCategory', 'Signal',
    'RSIFactor', 'BollingerFactor', 'MACDFactor', 'ATRFactor',
    'OrderImbalanceFactor', 'BidAskSpreadFactor', 'MidPriceDriftFactor',
    'VolumeRateFactor', 'AmihudIlliquidityFactor',
    # Strategies
    'SignalEngine', 'CompositeSignalEngine',
    # OMS（事件驱动栈，当前生产未对接）
    'OMS', 'EventDrivenPaperBroker', 'Order', 'Fill', 'Position', 'BrokerAdapter',
    # Risk
    'RiskEngine', 'RiskResult', 'RiskPosition', 'PositionBook',
    # DataSources
    'MarketSnapshot',
    'NorthBoundDataSource',
    # Level2
    'Level2DataSource', 'OrderBook', 'TickBarAggregator', 'TickBar',
    # Backtest
    'BacktestEngine', 'BacktestConfig', 'BacktestResult',
    'PerformanceAnalyzer', 'TradeRecord', 'PositionSnapshot',
    # Research
    'FactorResearcher', 'WalkForwardAnalyzer', 'FactorAnalysisResult',
]
