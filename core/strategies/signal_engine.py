"""
SignalEngine — 信号生成引擎

SignalEngine: 单因子信号生成（兼容现有 evaluate_signal 逻辑）
CompositeSignalEngine: 多因子加权信号生成

两种模式:
  1. single_factor: 只用一个因子，保留当前 RSI(25/65) 逻辑
  2. composite: 多因子加权，z-score 叠加，返回综合信号
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Literal
import pandas as pd
import numpy as np

from core.factors.base import Factor, Signal
from core.event_bus import EventBus, MarketEvent


@dataclass
class SignalEngine:
    """
    单因子信号生成器。
    兼容当前 signals.py 的 evaluate_signal 逻辑，但接口更清晰。
    """
    factor: Factor
    bus: Optional[EventBus] = None
    mode: Literal['single', 'composite'] = 'single'

    def evaluate(
        self,
        data: pd.DataFrame,
        price: float,
        atr_threshold: float = 0.85,
        regime: str = 'CALM',
    ) -> List[Signal]:
        """
        主评估入口。
        data 必须包含: open, high, low, close, volume 列
        返回 Signal 列表（0~1个）
        """
        # 1. 计算因子值
        factor_values = self.factor.evaluate(data)

        # 2. 生成信号
        signals = self.factor.signals(factor_values, price)

        # 3. ATR 过滤（仅当因子是 RSI 时生效）
        if isinstance(self.factor, __import__('core.factors.price_momentum', fromlist=['RSIFactor']).RSIFactor):
            signals = self._atr_filter(signals, data, atr_threshold)

        return signals

    def _atr_filter(
        self,
        signals: List[Signal],
        data: pd.DataFrame,
        atr_threshold: float,
    ) -> List[Signal]:
        """ATR 波动率过滤：市场高波动期屏蔽 RSI BUY 信号"""
        high = data['high']
        low = data['low']
        close = data['close']
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        atr_max = atr.rolling(20).max()
        atr_ratio = atr.iloc[-1] / atr_max.iloc[-1] if atr_max.iloc[-1] != 0 else 1

        # 高波动期（ATR ratio > 阈值），屏蔽 RSI BUY
        if atr_ratio > atr_threshold:
            return [s for s in signals if s.direction != 'BUY']
        return signals

    def on_market_event(self, event: MarketEvent) -> None:
        """EventBus 消费者：从 MarketEvent 生成 SignalEvent"""
        if not hasattr(self, '_last_signal_time'):
            self._last_signal_time = datetime.min

        # 频率控制：每分钟最多一次信号
        if (datetime.now() - self._last_signal_time).total_seconds() < 60:
            return

        data = pd.DataFrame([event.data])
        data.index = [event.timestamp]
        signals = self.evaluate(data, price=event.close)
        if signals:
            self._last_signal_time = datetime.now()
            if self.bus:
                from core.event_bus import SignalEvent
                self.bus.emit(SignalEvent(signal=signals[0]))


@dataclass
class CompositeSignalEngine:
    """
    多因子加权信号生成器。
    各因子独立评估 → z-score 归一化 → 线性加权 → 综合信号

    优势：
      - 多信号共振 → 误信号率降低
      - 可配置权重（当前固定，未来接入 BL 模型动态调整）
      - 因子可替换（RSI ↔ MACD ↔ Bollinger 不改核心逻辑）
    """
    factors: Dict[str, Factor] = field(default_factory=dict)
    # 因子权重: {factor_name: weight}，权重总和应 ≈ 1.0
    weights: Dict[str, float] = field(default_factory=dict)
    bus: Optional[EventBus] = None

    def add_factor(self, name: str, factor: Factor, weight: float = 1.0) -> 'CompositeSignalEngine':
        self.factors[name] = factor
        self.weights[name] = weight
        return self

    def evaluate(
        self,
        data: pd.DataFrame,
        price: float,
        thresholds: Dict[str, float] = None,
    ) -> List[Signal]:
        """
        多因子综合评估。
        返回：按加权强度排序的 BUY/SELL 信号（通常最多各1个）
        """
        if not self.factors:
            return []

        thresholds = thresholds or {}

        # 1. 各因子独立评估 → raw scores
        raw_scores = {}
        for name, factor in self.factors.items():
            try:
                fv = factor.evaluate(data)
                raw_scores[name] = fv.iloc[-1] if len(fv) > 0 else 0
            except Exception:
                raw_scores[name] = 0

        # 2. 加权综合评分
        total_score = sum(
            raw_scores.get(name, 0) * self.weights.get(name, 1.0)
            for name in self.factors
        )
        total_weight = sum(self.weights.values())
        if total_weight > 0:
            total_score /= total_weight

        # 3. 从各因子生成独立信号，携带元数据
        sub_signals = []
        for name, factor in self.factors.items():
            try:
                fv = factor.evaluate(data)
                thresh = thresholds.get(name, 1.0)
                for sig in factor.signals(fv, price):
                    sig.metadata['weight'] = self.weights.get(name, 1.0)
                    sig.metadata['sub_score'] = raw_scores.get(name, 0)
                    sub_signals.append(sig)
            except Exception:
                pass

        # 4. 汇聚 BUY 信号（取强度最强的）
        buy_signals = sorted(
            [s for s in sub_signals if s.direction == 'BUY'],
            key=lambda s: s.strength * self.weights.get(s.factor_name, 1),
            reverse=True
        )
        sell_signals = sorted(
            [s for s in sub_signals if s.direction == 'SELL'],
            key=lambda s: s.strength * self.weights.get(s.factor_name, 1),
            reverse=True
        )

        result = []
        if buy_signals:
            top = buy_signals[0]
            top.metadata['composite_score'] = total_score
            top.metadata['all_buy'] = [s.factor_name for s in buy_signals]
            result.append(top)
        if sell_signals:
            top = sell_signals[0]
            top.metadata['composite_score'] = total_score
            top.metadata['all_sell'] = [s.factor_name for s in sell_signals]
            result.append(top)

        return result

    def on_market_event(self, event: MarketEvent) -> None:
        """EventBus 消费者"""
        if not hasattr(self, '_last_signal_time'):
            self._last_signal_time = datetime.min
        if (datetime.now() - self._last_signal_time).total_seconds() < 60:
            return
        data = pd.DataFrame([event.data])
        data.index = [event.timestamp]
        signals = self.evaluate(data, price=event.close)
        if signals:
            self._last_signal_time = datetime.now()
            if self.bus:
                from core.event_bus import SignalEvent
                self.bus.emit(SignalEvent(signal=signals[0]))
