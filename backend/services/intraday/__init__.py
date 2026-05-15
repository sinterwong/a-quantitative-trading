"""
backend.services.intraday — IntradayMonitor 子模块。

P2-7 拆分:把原 1836 行的 intraday_monitor.py 按职责拆为 5 个 Mixin。
原 IntradayMonitor 类通过 MRO 组合保留同样的对外接口与方法签名。
"""

from .market_hours import (
    is_market_open,
    next_market_seconds,
    MARKET_MORNING_START,
    MARKET_MORNING_END,
    MARKET_AFTERNOON_START,
    MARKET_AFTERNOON_END,
)
from .cooldown import CooldownTracker
from .data import DataMixin
from .signaling import SignalingMixin, BUY_THRESHOLD_NEW, BUY_THRESHOLD_ADD
from .risk import RiskMixin, MAX_POSITION_PCT
from .execution import ExecutionMixin
from .alerts import AlertsMixin

__all__ = [
    'is_market_open',
    'next_market_seconds',
    'MARKET_MORNING_START',
    'MARKET_MORNING_END',
    'MARKET_AFTERNOON_START',
    'MARKET_AFTERNOON_END',
    'CooldownTracker',
    'DataMixin',
    'SignalingMixin',
    'RiskMixin',
    'ExecutionMixin',
    'AlertsMixin',
    'BUY_THRESHOLD_NEW',
    'BUY_THRESHOLD_ADD',
    'MAX_POSITION_PCT',
]
