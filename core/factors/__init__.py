# core.factors — 因子库
"""
所有因子实现 Factor 基类，返回 z-score 归一化的因子值。
可任意组合、权重叠加。
"""

from core.factors.base import Factor, FactorCategory, Signal

__all__ = ['Factor', 'FactorCategory', 'Signal']
