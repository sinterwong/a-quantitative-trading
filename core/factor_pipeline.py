"""
FactorPipeline — 因子流水线

职责：
- 批量执行多个因子的 evaluate()
- 加权合成综合得分（z-score 空间，权重归一化）
- 汇总所有因子的 signals()
- 返回结构化 PipelineResult

设计原则：
- Pipeline 本身无状态，可重复调用
- 每次 run() 都用当前传入的 DataFrame，不缓存历史
- DataLayer 获取数据由调用方负责（StrategyRunner 或 Backtest）

用法：
    pipeline = FactorPipeline()
    pipeline.add('RSI',   weight=0.4, params={'period': 14})
    pipeline.add('MACD',  weight=0.3)
    pipeline.add('ATR',   weight=0.3)

    result = pipeline.run(symbol='600519.SH', data=df, price=current_price)
    print(result.combined_score)   # float，正=超买倾向，负=超卖倾向
    print(result.signals)          # List[Signal]
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union, Type
import pandas as pd
import numpy as np

from core.factors.base import Factor, Signal
from core.factor_registry import registry as _global_registry, FactorRegistry

# IC 计算窗口（月）
_IC_ROLLING_MONTHS = 3


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FactorResult:
    """单个因子的执行结果。"""
    name: str
    weight: float
    factor_values: pd.Series          # z-score 序列，索引对齐输入 data
    latest_value: float               # factor_values.iloc[-1]
    signals: List[Signal]
    error: Optional[str] = None       # 非 None 时表示计算失败


@dataclass
class PipelineResult:
    """
    FactorPipeline.run() 的返回值。

    Attributes
    ----------
    symbol:
        标的代码
    combined_score:
        加权综合得分（z-score 空间）
        > 0 偏多头，< 0 偏空头；通常在 [-3, 3] 范围内
    factor_results:
        每个因子的独立结果
    signals:
        所有因子信号的合并列表（按 strength 降序）
    dominant_signal:
        强度最高的信号的方向（'BUY' / 'SELL' / 'HOLD'）
    metadata:
        附加诊断信息
    """
    symbol: str
    combined_score: float
    factor_results: List[FactorResult]
    signals: List[Signal]
    dominant_signal: str              # 'BUY' / 'SELL' / 'HOLD'
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def factor_score(self, name: str) -> Optional[float]:
        """返回指定因子的最新 z-score，未找到则 None。"""
        for fr in self.factor_results:
            if fr.name == name:
                return fr.latest_value
        return None

    def has_error(self) -> bool:
        """是否有任何因子计算失败。"""
        return any(fr.error is not None for fr in self.factor_results)

    def errors(self) -> Dict[str, str]:
        """返回失败因子的 {name: error} 字典。"""
        return {fr.name: fr.error for fr in self.factor_results if fr.error}

    @property
    def buy_strength(self) -> float:
        """所有 BUY 信号强度之和（归一化到 [0,1]）。"""
        total = sum(s.strength for s in self.signals if s.direction == 'BUY')
        return min(total, 1.0)

    @property
    def sell_strength(self) -> float:
        """所有 SELL 信号强度之和（归一化到 [0,1]）。"""
        total = sum(s.strength for s in self.signals if s.direction == 'SELL')
        return min(total, 1.0)


# ---------------------------------------------------------------------------
# FactorEntry (internal)
# ---------------------------------------------------------------------------

@dataclass
class _FactorEntry:
    factor: Factor
    weight: float


# ---------------------------------------------------------------------------
# FactorPipeline
# ---------------------------------------------------------------------------

class FactorPipeline:
    """
    因子流水线。

    Parameters
    ----------
    reg:
        使用的 FactorRegistry（默认使用全局 registry）
    min_bars:
        数据最少行数要求，不满足时所有因子返回 HOLD
    """

    def __init__(
        self,
        reg: Optional[FactorRegistry] = None,
        min_bars: int = 30,
    ) -> None:
        self._reg = reg or _global_registry
        self._entries: List[_FactorEntry] = []
        self.min_bars = min_bars

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def add(
        self,
        name_or_cls: Union[str, Type[Factor]],
        *,
        weight: float = 1.0,
        params: Optional[Dict[str, Any]] = None,
        symbol: str = '',
    ) -> 'FactorPipeline':
        """
        添加因子到流水线。

        Parameters
        ----------
        name_or_cls:
            因子名称（str，从 registry 查找）或直接传入 Factor 子类
        weight:
            权重（正数；run() 时自动归一化）
        params:
            实例化参数，覆盖 registry default_params
        symbol:
            绑定标的代码（影响 Signal.symbol）

        Returns
        -------
        self（支持链式调用）
        """
        if weight <= 0:
            raise ValueError(f"weight must be positive, got {weight}")

        kw = dict(params or {})
        if symbol:
            kw['symbol'] = symbol

        if isinstance(name_or_cls, str):
            factor = self._reg.create(name_or_cls, **kw)
        elif isinstance(name_or_cls, type) and issubclass(name_or_cls, Factor):
            factor = name_or_cls(**kw)
        else:
            raise TypeError(
                f"name_or_cls must be a str or Factor subclass, got {type(name_or_cls)}"
            )

        self._entries.append(_FactorEntry(factor=factor, weight=weight))
        return self

    def clear(self) -> None:
        """清空所有因子。"""
        self._entries.clear()

    @property
    def factor_names(self) -> List[str]:
        return [e.factor.name for e in self._entries]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(
        self,
        symbol: str,
        data: pd.DataFrame,
        price: Optional[float] = None,
    ) -> PipelineResult:
        """
        对 symbol 运行所有因子并汇总结果。

        Parameters
        ----------
        symbol:
            标的代码（写入 Signal.symbol 和 PipelineResult.symbol）
        data:
            包含 open/high/low/close/volume 列的 DataFrame，按日期升序
        price:
            当前价格（用于 Signal.price）；默认取 data['close'].iloc[-1]

        Returns
        -------
        PipelineResult
        """
        if price is None and len(data) > 0:
            price = float(data['close'].iloc[-1])
        price = price or 0.0

        if len(data) < self.min_bars:
            return self._empty_result(symbol, price, reason='insufficient_data')

        if not self._entries:
            return self._empty_result(symbol, price, reason='no_factors')

        factor_results: List[FactorResult] = []
        total_weight = sum(e.weight for e in self._entries)

        for entry in self._entries:
            fr = self._run_one(entry, symbol, data, price)
            factor_results.append(fr)

        # 加权综合得分（仅使用成功的因子）
        combined_score = 0.0
        valid_weight = 0.0
        all_signals: List[Signal] = []

        for fr, entry in zip(factor_results, self._entries):
            if fr.error is None:
                w = entry.weight / total_weight
                combined_score += fr.latest_value * w
                valid_weight += w
                all_signals.extend(fr.signals)

        if valid_weight > 0 and valid_weight < 1.0:
            # 有失败因子时，重新归一化
            combined_score = combined_score / valid_weight if valid_weight else 0.0

        # 按强度排序信号
        all_signals.sort(key=lambda s: s.strength, reverse=True)

        dominant = self._dominant_signal(all_signals)

        return PipelineResult(
            symbol=symbol,
            combined_score=round(combined_score, 6),
            factor_results=factor_results,
            signals=all_signals,
            dominant_signal=dominant,
            metadata={
                'bars_used': len(data),
                'factors_ok': sum(1 for fr in factor_results if fr.error is None),
                'factors_total': len(factor_results),
                'price': price,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_one(
        self,
        entry: _FactorEntry,
        symbol: str,
        data: pd.DataFrame,
        price: float,
    ) -> FactorResult:
        factor = entry.factor
        # 确保 signal 里有正确的 symbol
        if hasattr(factor, 'symbol') and not factor.symbol:
            factor.symbol = symbol

        try:
            values = factor.evaluate(data)
            latest = float(values.iloc[-1]) if len(values) else 0.0
            if np.isnan(latest):
                latest = 0.0
            sigs = factor.signals(values, price)
        except Exception as exc:  # noqa: BLE001
            return FactorResult(
                name=factor.name,
                weight=entry.weight,
                factor_values=pd.Series(dtype=float),
                latest_value=0.0,
                signals=[],
                error=str(exc),
            )

        return FactorResult(
            name=factor.name,
            weight=entry.weight,
            factor_values=values,
            latest_value=latest,
            signals=sigs,
        )

    @staticmethod
    def _dominant_signal(signals: List[Signal]) -> str:
        if not signals:
            return 'HOLD'
        buy_w = sum(s.strength for s in signals if s.direction == 'BUY')
        sell_w = sum(s.strength for s in signals if s.direction == 'SELL')
        if buy_w == 0 and sell_w == 0:
            return 'HOLD'
        return 'BUY' if buy_w >= sell_w else 'SELL'

    @staticmethod
    def _empty_result(symbol: str, price: float, reason: str) -> PipelineResult:
        return PipelineResult(
            symbol=symbol,
            combined_score=0.0,
            factor_results=[],
            signals=[],
            dominant_signal='HOLD',
            metadata={'reason': reason, 'price': price},
        )


# ---------------------------------------------------------------------------
# DynamicWeightPipeline — 基于滚动 IC 自动调整因子权重
# ---------------------------------------------------------------------------

class DynamicWeightPipeline(FactorPipeline):
    """
    动态权重因子流水线。

    每隔 ``update_freq_days`` 根据各因子滚动 IC（预测下期收益的 Spearman 相关系数）
    重新计算权重：
        w_i = max(IC_i_rolling, 0) / Σ max(IC_j_rolling, 0)
    若所有因子 IC 均 ≤ 0，退回等权。

    Parameters
    ----------
    ic_window_days : int
        计算 IC 的回溯窗口（默认 63 个交易日 ≈ 3 个月）
    update_freq_days : int
        重新计算权重的频率（默认 21 个交易日 ≈ 1 个月）
    min_bars : int
        同 FactorPipeline.min_bars
    """

    def __init__(
        self,
        ic_window_days: int = 63,
        update_freq_days: int = 21,
        reg: Optional[FactorRegistry] = None,
        min_bars: int = 30,
    ) -> None:
        super().__init__(reg=reg, min_bars=min_bars)
        self.ic_window_days = ic_window_days
        self.update_freq_days = update_freq_days

        # 运行时状态
        self._ic_history: Dict[str, List[float]] = {}   # factor_name → IC 序列
        self._dynamic_weights: Dict[str, float] = {}    # factor_name → 最新权重
        self._bars_since_update: int = 0
        self._weight_history: List[Dict[str, float]] = []  # 权重历史（诊断用）

    # ------------------------------------------------------------------
    # Override run() — 在执行前更新动态权重
    # ------------------------------------------------------------------

    def run(
        self,
        symbol: str,
        data: pd.DataFrame,
        price: Optional[float] = None,
    ) -> PipelineResult:
        if len(data) >= self.min_bars + self.ic_window_days:
            self._maybe_update_weights(data)

        # 用动态权重替换 _entries 的权重（临时覆盖）
        original_weights = [e.weight for e in self._entries]
        for entry in self._entries:
            name = entry.factor.name
            if name in self._dynamic_weights:
                entry.weight = self._dynamic_weights[name]

        result = super().run(symbol, data, price)

        # 恢复原始权重
        for entry, w in zip(self._entries, original_weights):
            entry.weight = w

        result.metadata['dynamic_weights'] = dict(self._dynamic_weights)
        return result

    # ------------------------------------------------------------------
    # IC 计算 & 权重更新
    # ------------------------------------------------------------------

    def _maybe_update_weights(self, data: pd.DataFrame) -> None:
        self._bars_since_update += 1
        if self._bars_since_update < self.update_freq_days and self._dynamic_weights:
            return
        self._bars_since_update = 0
        self._update_weights(data)

    def _update_weights(self, data: pd.DataFrame) -> None:
        """用滚动 IC 重新计算各因子权重。"""
        window = data.iloc[-self.ic_window_days:]
        if len(window) < 20:
            return

        # 次日收益（预测目标）
        fwd_returns = window['close'].pct_change().shift(-1).dropna()
        if len(fwd_returns) < 10:
            return

        ic_map: Dict[str, float] = {}
        for entry in self._entries:
            name = entry.factor.name
            try:
                vals = entry.factor.evaluate(window)
                aligned = vals.reindex(fwd_returns.index).dropna()
                rets_aligned = fwd_returns.reindex(aligned.index).dropna()
                aligned = aligned.reindex(rets_aligned.index)
                if len(aligned) < 5:
                    ic_map[name] = 0.0
                    continue
                rx = pd.Series(aligned.values).rank().values
                ry = pd.Series(rets_aligned.values).rank().values
                corr = float(np.corrcoef(rx, ry)[0, 1])
                ic_map[name] = corr if not np.isnan(corr) else 0.0
            except Exception:
                ic_map[name] = 0.0

        # 权重 = max(IC, 0)，负 IC 因子权重归零（等效于不使用）
        positive_ic = {k: max(v, 0.0) for k, v in ic_map.items()}
        total_pos = sum(positive_ic.values())

        if total_pos > 1e-8:
            self._dynamic_weights = {k: v / total_pos for k, v in positive_ic.items()}
        else:
            # 全部因子 IC ≤ 0，退回等权
            n = len(self._entries)
            self._dynamic_weights = {e.factor.name: 1.0 / n for e in self._entries}

        self._weight_history.append(dict(self._dynamic_weights))

    # ------------------------------------------------------------------
    # 诊断接口
    # ------------------------------------------------------------------

    def weight_history_df(self) -> pd.DataFrame:
        """
        返回权重历史 DataFrame（行=更新时间序列，列=因子名）。
        可用于绘制权重随时间的变化图。
        """
        if not self._weight_history:
            return pd.DataFrame()
        return pd.DataFrame(self._weight_history)

    def current_weights(self) -> Dict[str, float]:
        """返回当前动态权重（若尚未计算，返回等权）。"""
        if self._dynamic_weights:
            return dict(self._dynamic_weights)
        n = len(self._entries)
        return {e.factor.name: 1.0 / n for e in self._entries} if n else {}
