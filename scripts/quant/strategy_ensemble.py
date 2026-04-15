"""
strategy_ensemble.py — 多策略组合器
===================================
根据当前市场环境自动选择最优策略。

核心逻辑：
  1. 读取 regime_detector 当前环境
  2. 从 allowed_strategies 中选择对应信号函数
  3. 返回 ensemble 信号（加权投票或单一最优）

使用方法：
  ensemble = StrategyEnsemble(regime_detector)
  signal = ensemble.get_signal(symbol, kline, params)
"""

import os
import sys
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger('ensemble')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)


# ─── 信号函数注册表 ─────────────────────────────────────────────────────────

def _build_signal_registry():
    """
    延迟导入策略函数，避免循环依赖。
    Returns: {name: (signal_func_class, param_names)}
    """
    try:
        from backtest import RSISignalFunc, MACDSignalFunc
        from backtest import RSISignalWithATRFilter
        return {
            'RSI':           RSISignalFunc,
            'RSI+MACD':      RSISignalFunc,   # placeholder — see below
            'RSI+BBANDS':    RSISignalFunc,   # placeholder — see below
            'RSI+ATR':       RSISignalWithATRFilter,
        }
    except ImportError as e:
        logger.warning('Cannot import backtest signals: %s', e)
        return {}


# ─── 信号函数工厂（支持回测 + 实时）─────────────────────────────────────────

class StrategySignal:
    """
    统一信号包装器，兼容回测框架信号类和实时 evaluate_signal。
    """
    def __init__(self, signal: str, confidence: float, reason: str,
                 regime: str = 'CALM', strategy_name: str = 'RSI'):
        self.signal = signal
        self.confidence = confidence  # 0.0 ~ 1.0
        self.reason = reason
        self.regime = regime
        self.strategy_name = strategy_name

    def __repr__(self):
        return (f'<Signal {self.signal} conf={self.confidence:.2f} '
                f'regime={self.regime} strategy={self.strategy_name}>')


class StrategyEnsemble:
    """
    多策略组合器 — 根据市场环境选择并执行最优策略。

    Args:
        regime_detector_module: regime_detector 模块（已初始化的）
        use_multi_strategy: 是否在 allowed_strategies 中选择（默认 True）
    """

    def __init__(self, regime_detector_module=None,
                 use_multi_strategy: bool = True):
        self._regime = regime_detector_module
        self.use_multi_strategy = use_multi_strategy
        self._registry = _build_signal_registry()
        self._current_regime = None
        self._params = None

    def detect(self) -> str:
        """检测并缓存当前环境。"""
        if self._regime is None:
            self._current_regime = 'CALM'
            self._params = {
                'rsi_buy': 25, 'rsi_sell': 65,
                'atr_threshold': 0.85, 'stop_loss': 0.05,
                'take_profit': 0.20, 'atr_multiplier': 3.0,
                'allowed_strategies': ['RSI', 'RSI+MACD'],
            }
            return self._current_regime

        cached = self._regime.get_cached_regime()
        self._current_regime = cached['regime']
        self._params = self._regime.get_params_for_regime(self._current_regime)
        return self._current_regime

    def get_params(self) -> dict:
        """获取当前环境对应的策略参数。"""
        if self._params is None:
            self.detect()
        return self._params

    def get_allowed_strategies(self) -> list:
        """获取当前环境允许的策略列表。"""
        return self.get_params().get('allowed_strategies', ['RSI'])

    # ─── 实时信号评估（兼容 intraday_monitor）───────────────────────────────

    def evaluate(self, symbol: str, rsi_now: float, rsi_prev: float,
                 volume_ratio: float, day_chg: float,
                 positions: list = None,
                 price: float = None,
                 pct: float = None) -> Optional[StrategySignal]:
        """
        评估当前是否触发买入信号。

        Args:
            symbol: 标的代码
            rsi_now / rsi_prev: 当前 / 前日 RSI(14)
            volume_ratio: 量比
            day_chg: 当日涨跌幅（%）
            positions: 持仓列表（传给 evaluate_signal）
            price: 当前价
            pct: 当日涨跌幅（小数）

        Returns:
            StrategySignal or None（无信号时）
        """
        if self._current_regime is None:
            self.detect()

        params = self._params or self.get_params()
        allowed = params.get('allowed_strategies', ['RSI'])

        # 只在有 RSI 信号时评估
        if not (rsi_now < params['rsi_buy'] and rsi_prev >= params['rsi_buy']):
            return None

        # 尝试主策略（第一个 allowed）
        primary = allowed[0] if allowed else 'RSI'
        confidence = self._calc_confidence(primary, rsi_now, volume_ratio, day_chg)

        reason = (f'{self._current_regime}市[{primary}]'
                  f' RSI买({rsi_now:.1f}<{params["rsi_buy"]})'
                  f' 量比{volume_ratio:.2f}')

        return StrategySignal(
            signal='RSI_BUY',
            confidence=confidence,
            reason=reason,
            regime=self._current_regime,
            strategy_name=primary,
        )

    def _calc_confidence(self, strategy: str,
                         rsi_now: float, volume_ratio: float,
                         day_chg: float) -> float:
        """
        计算信号置信度（0.0 ~ 1.0）。
        - RSI 超卖越多 → 置信度越高
        - 量比 > 1.5 → 置信度加分
        - 跌幅 > 2% → 可能是超跌反弹，置信度提高
        """
        params = self._params or self.get_params()
        rsi_buy = params['rsi_buy']

        # RSI 偏离基础分（RSI 越低越超卖）
        rsi_score = max(0.0, (rsi_buy - rsi_now) / rsi_buy)

        # 量比加分
        vol_score = min(0.2, max(0.0, (volume_ratio - 1.0) * 0.1))

        # 跌幅加分（超跌反弹）
        chg_score = min(0.2, max(0.0, abs(day_chg) / 10 * 0.2)) if day_chg < 0 else 0.0

        base = 0.5
        return min(1.0, base + rsi_score + vol_score + chg_score)

    # ─── 回测接口 ─────────────────────────────────────────────────────────

    def get_signal_for_backtest(self, symbol: str, kline_data: dict) -> str:
        """
        回测时使用的信号生成器（由 backtest.py 调用）。
        Returns: 'BUY' | 'SELL' | 'HOLD'
        """
        regime = self.detect()
        params = self.get_params()

        try:
            from services.signals import evaluate_signal
        except ImportError:
            logger.warning('Cannot import evaluate_signal for backtest')
            return 'HOLD'

        closes = kline_data.get('closes', [])
        volumes = kline_data.get('volumes', [])
        if len(closes) < 20:
            return 'HOLD'

        pct = 0.0
        if len(closes) >= 2:
            pct = (closes[-1] / closes[-2] - 1) * 100

        result = evaluate_signal(
            symbol=symbol,
            prev_rsi=50.0,
            rsi_now=30.0,
            volume_ratio=1.0,
            day_chg=pct,
            positions=[],
        )
        return result.signal if result else 'HOLD'

    def __repr__(self):
        r = self._current_regime or 'unknown'
        p = self._params or {}
        return (f'<StrategyEnsemble regime={r} '
                f'allowed={p.get("allowed_strategies", [])}>')
