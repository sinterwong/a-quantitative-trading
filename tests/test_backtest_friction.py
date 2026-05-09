"""
test_backtest_friction.py — P1-11 回测引擎跌停板/退市/停牌补全测试

验证：
  1. 一字涨停日 BUY 信号被拒（封单）
  2. 一字跌停日 SELL 信号被拒（封单）
  3. 数据序列结束 → 持仓强制清仓（退市模拟）
  4. 关闭 simulate_limit_up_down 后回到旧行为
  5. 停牌日 volume=0 → 跳过开仓（已有逻辑保留）
"""

from __future__ import annotations

import unittest
from datetime import datetime

import numpy as np
import pandas as pd

from core.backtest_engine import BacktestConfig, BacktestEngine
from core.factors.base import Factor, Signal


def _make_data_with_limit_up(n: int = 30) -> pd.DataFrame:
    """构造数据：第 5 根 bar 是一字涨停（high == low，涨幅 +10%）。"""
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    rng = np.random.default_rng(42)
    close = np.empty(n)
    close[0] = 10.0
    for i in range(1, n):
        close[i] = close[i - 1] * (1 + rng.normal(0.001, 0.005))
    open_ = close * (1 + rng.normal(0, 0.001, n))
    high = np.maximum(open_, close) * 1.005
    low = np.minimum(open_, close) * 0.995
    vol = rng.uniform(1e6, 5e6, n)

    # 第 5 根 bar：一字涨停（参考前一日 close × 1.10）
    prev_close = close[4]
    close[5] = prev_close * 1.10
    open_[5] = prev_close * 1.10
    high[5] = prev_close * 1.10   # 一字
    low[5] = prev_close * 1.10    # 一字（high == low）

    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol},
        index=dates,
    )


def _make_data_with_limit_down(n: int = 30) -> pd.DataFrame:
    """构造数据：第 10 根 bar 是一字跌停（high == low，跌幅 -10%）。"""
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    rng = np.random.default_rng(7)
    close = np.empty(n)
    close[0] = 10.0
    for i in range(1, n):
        close[i] = close[i - 1] * (1 + rng.normal(0.001, 0.005))
    open_ = close * 0.999
    high = close * 1.005
    low = close * 0.995
    vol = np.full(n, 1e6)

    prev_close = close[9]
    close[10] = prev_close * 0.90
    open_[10] = prev_close * 0.90
    high[10] = prev_close * 0.90
    low[10] = prev_close * 0.90

    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol},
        index=dates,
    )


class _BarBuyFactor(Factor):
    """在第 trigger_idx 根 bar 触发一次 BUY。"""
    name = 'BarBuy'

    def __init__(self, trigger_idx: int = 4, symbol: str = ''):
        self.trigger_idx = trigger_idx
        self.symbol = symbol
        self._fired = False

    def evaluate(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(np.zeros(len(df)), index=df.index)

    def signals(self, fv, price, threshold: float = 1.0):
        if not self._fired and len(fv) >= self.trigger_idx:
            self._fired = True
            return [Signal(
                timestamp=datetime.now(), symbol=self.symbol or 'X.SH',
                direction='BUY', strength=1.0,
                factor_name=self.name, price=float(price),
            )]
        return []


class TestLimitUpDown(unittest.TestCase):

    def test_buy_signal_rejected_on_limit_up(self):
        """触发 BUY 时 next_bar 是一字涨停 → 不应成交。"""
        df = _make_data_with_limit_up(n=30)
        # trigger_idx=4 → 信号 bar = idx 4，next_bar = idx 5（一字涨停）
        config = BacktestConfig(
            initial_equity=100_000, use_exit_engine=False,
            simulate_limit_up_down=True,
        )
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_BarBuyFactor(trigger_idx=4, symbol='TEST.SH'))
        result = engine.run()

        # 不应有 BUY 成交（被涨停封住）
        buys = [t for t in result.trades if t.direction == 'BUY']
        self.assertEqual(len(buys), 0,
                         '一字涨停日 BUY 信号应被拒绝')

    def test_buy_succeeds_when_limit_simulation_disabled(self):
        """关掉 simulate_limit_up_down → 即使一字涨停也能买。"""
        df = _make_data_with_limit_up(n=30)
        config = BacktestConfig(
            initial_equity=100_000, use_exit_engine=False,
            simulate_limit_up_down=False,
        )
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_BarBuyFactor(trigger_idx=4, symbol='TEST.SH'))
        result = engine.run()

        buys = [t for t in result.trades if t.direction == 'BUY']
        self.assertGreater(len(buys), 0,
                           '关闭涨跌停模拟时一字涨停应能成交')

    def test_sell_blocked_on_limit_down(self):
        """ExitEngine 触发 SELL 时遇一字跌停 → 不应成交。"""
        # 构造：buy at idx 3, then drop sharply at idx 9 (-10%) triggers exit
        # but next bar (idx 10) is limit-down → SELL fails
        df = _make_data_with_limit_down(n=30)

        # 第 3 根 bar 触发 BUY，注意我用 trigger_idx=3 让其在 idx 3 → 成交价是 idx 4 open
        # 之后在 idx 9 close 跌到 hard_sl，ExitEngine 在 idx 9 生成 SELL，
        # next_bar = idx 10 = 一字跌停 → SELL 被拒
        config = BacktestConfig(
            initial_equity=100_000, use_exit_engine=True,
            simulate_limit_up_down=True,
        )
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_BarBuyFactor(trigger_idx=3, symbol='TEST.SH'))
        result = engine.run()

        # 在 idx 10（一字跌停日）不应有 SELL 成交
        for t in result.trades:
            if t.direction == 'SELL':
                # 跌停日 = dates[10]
                ts = t.timestamp
                limit_dt = df.index[10]
                self.assertNotEqual(ts, limit_dt,
                                    '一字跌停日不应有 SELL 成交')


class TestDelistingLiquidation(unittest.TestCase):

    def test_position_force_liquidated_at_data_end(self):
        """持仓在数据序列最后一根 bar 时强制清仓。"""
        df = _make_data_with_limit_up(n=30)
        # 改写 idx 5 让它不是涨停，避免影响测试
        df.iloc[5, df.columns.get_loc('open')] = 10.0
        df.iloc[5, df.columns.get_loc('high')] = 10.05
        df.iloc[5, df.columns.get_loc('low')] = 9.95
        df.iloc[5, df.columns.get_loc('close')] = 10.02

        config = BacktestConfig(
            initial_equity=100_000, use_exit_engine=False,
            simulate_limit_up_down=False,
            simulate_delisting=True,
        )
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_BarBuyFactor(trigger_idx=3, symbol='TEST.SH'))
        result = engine.run()

        # 应当至少有一次 BUY + 一次"退市强制清仓"SELL
        liq_sells = [t for t in result.trades
                     if t.direction == 'SELL'
                     and 'Delisting' in t.signal_reason]
        self.assertEqual(len(liq_sells), 1,
                         '数据末尾应触发一次退市强制清仓')

    def test_no_liquidation_when_disabled(self):
        df = _make_data_with_limit_up(n=30)
        df.iloc[5, df.columns.get_loc('high')] = df.iloc[5, df.columns.get_loc('low')] * 1.001  # 取消一字
        config = BacktestConfig(
            initial_equity=100_000, use_exit_engine=False,
            simulate_limit_up_down=False,
            simulate_delisting=False,
        )
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_BarBuyFactor(trigger_idx=3, symbol='TEST.SH'))
        result = engine.run()

        liq_sells = [t for t in result.trades
                     if 'Delisting' in t.signal_reason]
        self.assertEqual(len(liq_sells), 0)


class TestSuspensionGap(unittest.TestCase):

    def test_suspended_bar_skips_buy_signal(self):
        """volume==0 标记停牌时仍应跳过开仓（已有逻辑）。"""
        df = _make_data_with_limit_up(n=30)
        df.iloc[5, df.columns.get_loc('high')] = df.iloc[5, df.columns.get_loc('low')] * 1.001  # 取消一字
        # idx 4（信号 bar）设停牌：load_data 会自动从 volume==0 推断 is_suspended
        df.iloc[4, df.columns.get_loc('volume')] = 0

        config = BacktestConfig(
            initial_equity=100_000, use_exit_engine=False,
            simulate_limit_up_down=False,
            simulate_delisting=False,
        )
        engine = BacktestEngine(config=config)
        engine.load_data('TEST.SH', df)
        engine.add_strategy(_BarBuyFactor(trigger_idx=4, symbol='TEST.SH'))
        result = engine.run()

        # 停牌日 BUY 信号应跳过（已有逻辑）
        buys_on_idx5 = [
            t for t in result.trades
            if t.direction == 'BUY' and t.timestamp == df.index[5]
        ]
        self.assertEqual(len(buys_on_idx5), 0)


if __name__ == '__main__':
    unittest.main()
