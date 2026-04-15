# core.strategies — 策略模板
"""
StrategyEngine: 多因子信号组合器
当前: 单因子模式（兼容现有 signals.py 逻辑）
未来: 多因子加权 → 统一 Signal 输出
"""

from core.strategies.signal_engine import SignalEngine, CompositeSignalEngine

__all__ = ['SignalEngine', 'CompositeSignalEngine']
