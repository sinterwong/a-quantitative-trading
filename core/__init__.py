# core — 商用级量化系统核心包
"""
EventBus + FactorExpression + OMS + RiskEngine + DataSources

Phase 1 (✅): EventBus + FactorExpression + SignalEngine
Phase 2 (✅): DataSources (SP期货/VIX/恒指/北向) + OMS抽象层 + RiskEngine
Phase 3 (✅): BrokerFactory + SafetyMode + 真实券商 STUB
Phase 4 (✅): Level2 数据源 + 订单簿因子
"""

from core.event_bus import (
    EventBus, Event, MarketEvent, SignalEvent,
    OrderEvent, FillEvent, RiskEvent, AlertEvent,
)
from core.factors.base import Factor, FactorCategory, Signal
from core.factors.price_momentum import RSIFactor, BollingerFactor, MACDFactor, ATRFactor
from core.strategies.signal_engine import SignalEngine, CompositeSignalEngine
from core.oms import OMS, PaperBroker, Order, Fill, Position, BrokerAdapter
from core.risk_engine import RiskEngine, RiskResult, RiskPosition, PositionBook
from core.data_sources import (
    DataSource,
    SPFuturesDataSource, VIXDataSource, HSIFuturesDataSource,
    TencentMinuteDataSource, NorthBoundDataSource,
    CompositeMarketDataSource, MarketSnapshot,
)

__all__ = [
    # EventBus
    'EventBus', 'Event', 'MarketEvent', 'SignalEvent',
    'OrderEvent', 'FillEvent', 'RiskEvent', 'AlertEvent',
    # Factors
    'Factor', 'FactorCategory', 'Signal',
    'RSIFactor', 'BollingerFactor', 'MACDFactor', 'ATRFactor',
    # Strategies
    'SignalEngine', 'CompositeSignalEngine',
    # OMS
    'OMS', 'PaperBroker', 'Order', 'Fill', 'Position', 'BrokerAdapter',
    # Risk
    'RiskEngine', 'RiskResult', 'RiskPosition', 'PositionBook',
    # DataSources
    'DataSource',
    'SPFuturesDataSource', 'VIXDataSource', 'HSIFuturesDataSource',
    'TencentMinuteDataSource', 'NorthBoundDataSource',
    'CompositeMarketDataSource', 'MarketSnapshot',
    # Level2
    'Level2DataSource', 'OrderBook', 'TickBarAggregator', 'TickBar',
    'OrderImbalanceFactor', 'BidAskSpreadFactor', 'MidPriceDriftFactor',
    'VolumeRateFactor', 'AmihudIlliquidityFactor',
]
