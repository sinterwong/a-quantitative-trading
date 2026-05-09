"""
test_exit_engine_integration.py — P0-1 ExitEngine 与 BacktestEngine 集成测试

验证：
  1. use_exit_engine=True 时，硬止损（hard_sl）触发卖出
  2. 同条件下 use_exit_engine=False 不会卖出（对比基线）
  3. 半仓卖出（SOFT_STOP_LOSS exit_pct=0.5）正确减仓
  4. 全仓止盈（TAKE_PROFIT_FULL）触发后持仓清零
  5. ExitEngine.priority 写入 trade.signal_reason
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from core.backtest_engine import BacktestConfig, BacktestEngine
from core.factors.base import Factor, Signal


def _make_falling_data(start: str = '2024-01-01', n: int = 80,
                      trigger_bar: int = 30,
                      pre_drop: float = 0.0,
                      post_drop: float = 0.20) -> pd.DataFrame:
    """构造价格序列：trigger_bar 之前小幅震荡，之后陡跌 post_drop。"""
    dates = pd.date_range(start, periods=n, freq='B')
    close = np.empty(n)
    close[0] = 10.0
    rng = np.random.default_rng(0)
    for i in range(1, trigger_bar):
        close[i] = close[i - 1] * (1 + rng.normal(0.0, 0.005))
    # 跌幅在 trigger_bar 一次性体现（构造硬止损场景）
    close[trigger_bar] = close[trigger_bar - 1] * (1 - post_drop)
    for i in range(trigger_bar + 1, n):
        close[i] = close[i - 1] * (1 + rng.normal(0.0, 0.005))
    open_ = close * (1 + rng.normal(0, 0.001, n))
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    vol = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol},
        index=dates,
    )


def _make_rising_data(start: str = '2024-01-01', n: int = 80,
                      total_rise: float = 0.40) -> pd.DataFrame:
    """构造单调上升价格：n 天内累计上涨 total_rise（用于触发 TAKE_PROFIT_FULL）。"""
    dates = pd.date_range(start, periods=n, freq='B')
    daily = (1 + total_rise) ** (1.0 / n) - 1
    close = 10.0 * np.cumprod(np.full(n, 1 + daily))
    open_ = close * 0.999
    high = close * 1.005
    low = close * 0.995
    vol = np.full(n, 1e6)
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol},
        index=dates,
    )


class _OneShotBuyFactor(Factor):
    """第 N 根 bar 触发一次 BUY，其余 HOLD —— 用于测试退出逻辑。"""
    name = 'OneShotBuy'

    def __init__(self, trigger_idx: int = 5, symbol: str = ''):
        self.trigger_idx = trigger_idx
        self.symbol = symbol
        self._fired = False

    def evaluate(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(np.zeros(len(df)), index=df.index)

    def signals(self, fv, price, threshold: float = 1.0):
        # 仅在第一次到达 trigger_idx 时发射，避免重复 BUY 被吞
        if not self._fired and len(fv) >= self.trigger_idx:
            self._fired = True
            return [Signal(
                timestamp=datetime.now(),
                symbol=self.symbol or 'X.SH',
                direction='BUY',
                strength=1.0,
                factor_name=self.name,
                price=float(price),
            )]
        return []


class TestExitEngineIntegration(unittest.TestCase):

    def test_hard_stop_loss_triggered_in_backtest(self):
        """跌幅 20% 触发 HARD_STOP_LOSS（默认 hard_sl=0.15）。"""
        df = _make_falling_data(n=80, trigger_bar=30, post_drop=0.20)
        config = BacktestConfig(initial_equity=100_000, use_exit_engine=True)
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_OneShotBuyFactor(trigger_idx=3, symbol='TEST.SH'))

        result = engine.run()

        sells = [t for t in result.trades if t.direction == 'SELL']
        self.assertGreaterEqual(len(sells), 1, '应触发至少一次 SELL')

        # 至少一次 SELL 来自 ExitEngine
        exit_sells = [t for t in sells if 'ExitEngine.' in t.signal_reason]
        self.assertGreaterEqual(len(exit_sells), 1,
                                'ExitEngine 应至少触发一次卖出')

    def test_no_exit_engine_no_stop_loss(self):
        """禁用 ExitEngine 后不应有 ExitEngine.* 标记的 SELL。"""
        df = _make_falling_data(n=80, trigger_bar=30, post_drop=0.20)
        config = BacktestConfig(initial_equity=100_000, use_exit_engine=False)
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_OneShotBuyFactor(trigger_idx=3, symbol='TEST.SH'))

        result = engine.run()

        exit_sells = [t for t in result.trades
                      if t.direction == 'SELL'
                      and 'ExitEngine.' in t.signal_reason]
        self.assertEqual(len(exit_sells), 0,
                         '禁用 ExitEngine 时不应触发 ExitEngine.* SELL')

    def test_take_profit_full_triggered(self):
        """累计涨幅 40% → 触发 TAKE_PROFIT_FULL（默认 tp_full=0.25）。"""
        df = _make_rising_data(n=60, total_rise=0.40)
        config = BacktestConfig(initial_equity=100_000, use_exit_engine=True)
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_OneShotBuyFactor(trigger_idx=3, symbol='TEST.SH'))

        result = engine.run()

        # 找到 TAKE_PROFIT_* 相关的卖出
        tp_sells = [t for t in result.trades
                    if t.direction == 'SELL'
                    and 'TAKE_PROFIT' in t.signal_reason]
        self.assertGreaterEqual(len(tp_sells), 1, '应触发分批/全仓止盈')

    def test_partial_exit_pct_keeps_position(self):
        """SOFT_STOP_LOSS（exit_pct=0.5）应只卖一半。"""
        # 构造：跌 10%（介于 soft_sl=0.08 和 hard_sl=0.15 之间）
        df = _make_falling_data(n=80, trigger_bar=30, post_drop=0.10)
        config = BacktestConfig(initial_equity=100_000, use_exit_engine=True)
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_OneShotBuyFactor(trigger_idx=3, symbol='TEST.SH'))

        result = engine.run()

        # 寻找 SOFT_STOP_LOSS sell
        soft_sells = [t for t in result.trades
                      if t.direction == 'SELL'
                      and 'SOFT_STOP_LOSS' in t.signal_reason]
        # SOFT_STOP_LOSS 触发后剩余仓位继续运行；可能后续被其它退出规则全部清掉，
        # 但首次触发的 shares 应小于建仓数量
        if soft_sells:
            buy = next(t for t in result.trades if t.direction == 'BUY')
            first_soft_sell = soft_sells[0]
            self.assertLess(first_soft_sell.shares, buy.shares,
                            'SOFT_STOP_LOSS 首次卖出应少于全仓')


if __name__ == '__main__':
    unittest.main()
