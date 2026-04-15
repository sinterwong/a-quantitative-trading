"""
Factor — 因子基类

所有因子实现：
1. evaluate() → z-score 归一化因子值（所有因子可比）
2. signals() → 从因子值生成 Signal 列表

因子分类：
- PRICE_MOMENTUM: 价量动量（RSI / MACD / 布林带）
- REGIME: 市场环境（ATR ratio / 趋势强度）
- FUNDAMENTAL: 基本面（PE / PB / 北向持仓）
- SENTIMENT: 情绪（新闻 / 舆情）
- EXTERNAL: 外部（美股期货 / VIX / 汇率）
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Dict, Any, Optional, Literal
import pandas as pd
import numpy as np


class FactorCategory(Enum):
    PRICE_MOMENTUM = 'price_momentum'
    REGIME = 'regime'
    FUNDAMENTAL = 'fundamental'
    SENTIMENT = 'sentiment'
    EXTERNAL = 'external'


@dataclass
class Signal:
    """标准化信号输出"""
    timestamp: datetime
    symbol: str
    direction: Literal['BUY', 'SELL']
    strength: float          # 0~1，z-score 绝对值映射
    factor_name: str
    price: float = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.direction}:{self.timestamp.isoformat()}"


class Factor(ABC):
    """
    因子基类。
    所有子类实现：
    - name: 因子名称
    - category: 因子类别
    - evaluate(data): 返回 z-score 归一化因子值（Series，索引 = data.index）
    - signals(factor_values, price): 可选，从因子值生成 Signal
    """

    name: str = 'Factor'
    category: FactorCategory = FactorCategory.PRICE_MOMENTUM

    @abstractmethod
    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        """
        计算因子值，返回 z-score 归一化的 pd.Series。
        索引必须与 data.index 一致。
        z_score = (raw - mean) / std，标准差为 0 时返回 0。
        """
        ...

    def normalize(self, raw: pd.Series) -> pd.Series:
        """z-score 归一化"""
        mean = raw.mean()
        std = raw.std()
        if std == 0 or pd.isna(std):
            return pd.Series(0, index=raw.index)
        return (raw - mean) / std

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        threshold: float = 1.0,
    ) -> List[Signal]:
        """
        从因子值生成信号。
        默认逻辑：z > threshold → SELL，z < -threshold → BUY
        子类可覆盖。
        """
        signals = []
        latest = factor_values.iloc[-1]
        direction: Literal['BUY', 'SELL']
        if latest < -threshold:
            direction = 'BUY'
        elif latest > threshold:
            direction = 'SELL'
        else:
            return []
        strength = min(abs(latest) / threshold, 1.0)
        return [Signal(
            timestamp=datetime.now(),
            symbol=getattr(self, 'symbol', ''),
            direction=direction,
            strength=strength,
            factor_name=self.name,
            price=price,
            metadata={'raw_factor_value': latest}
        )]

    def set_symbol(self, symbol: str) -> 'Factor':
        """因子绑定标的，方便 Signal 生成时携带标的"""
        self.symbol = symbol
        return self
