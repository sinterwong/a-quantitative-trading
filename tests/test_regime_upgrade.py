"""
test_regime_upgrade.py — P1-13 Regime 升级测试

验证：
  1. 自适应 ATR 阈值（90 分位数）替代固定 0.85
  2. MA60 30 日斜率参与 BULL/BEAR 判定（横盘不再误判 BEAR）
  3. 切换冷却期 5 个交易日内保持原状态
  4. RegimeInfo.position_reduce_target_pct = 0.75 当 BEAR
  5. should_reduce_positions 属性正确
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

import numpy as np


def _bull_prices(n: int = 320) -> tuple:
    """构造单调上涨：日均 +0.05% → MA60 斜率为正，close > MA20 > MA60。"""
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0005, 0.005, n)
    closes = 1000 * np.cumprod(1 + rets)
    highs = closes * 1.01
    lows = closes * 0.99
    return closes, highs, lows


def _bear_prices(n: int = 320) -> tuple:
    """构造单调下跌：日均 -0.10% → MA60 斜率为负。"""
    rng = np.random.default_rng(7)
    rets = rng.normal(-0.0010, 0.005, n)
    closes = 1000 * np.cumprod(1 + rets)
    highs = closes * 1.005
    lows = closes * 0.995
    return closes, highs, lows


def _sideways_prices(n: int = 320) -> tuple:
    """构造横盘：MA60 斜率接近 0。"""
    rng = np.random.default_rng(13)
    rets = rng.normal(0.0, 0.008, n)
    closes = 1000 * np.cumprod(1 + rets)
    highs = closes * 1.005
    lows = closes * 0.995
    return closes, highs, lows


def _vol_spike_prices(n: int = 320) -> tuple:
    """构造低 vol → 突然高 vol（最后 20 天波动放大 5 倍）。"""
    rng = np.random.default_rng(99)
    rets = np.concatenate([
        rng.normal(0.0, 0.005, n - 20),
        rng.normal(0.0, 0.025, 20),     # 高波动尾部
    ])
    closes = 1000 * np.cumprod(1 + rets)
    highs = closes * 1.02
    lows = closes * 0.98
    return closes, highs, lows


class TestRegimeIndicators(unittest.TestCase):

    def test_compute_indicators_bull(self):
        from core.regime import _compute_indicators
        c, h, l = _bull_prices()
        d = _compute_indicators(c, h, l)
        self.assertIsNotNone(d)
        # 上涨 → MA60 斜率应为正
        self.assertGreater(d['ma60_slope'], 0)

    def test_compute_indicators_bear_slope(self):
        from core.regime import _compute_indicators
        c, h, l = _bear_prices()
        d = _compute_indicators(c, h, l)
        self.assertLess(d['ma60_slope'], 0)

    def test_atr_threshold_dynamic_within_bounds(self):
        """动态阈值应是 0~1 之间的合理值。"""
        from core.regime import _compute_indicators
        c, h, l = _bull_prices()
        d = _compute_indicators(c, h, l)
        self.assertGreater(d['atr_threshold_dynamic'], 0)
        self.assertLessEqual(d['atr_threshold_dynamic'], 1.0 + 1e-6)


class TestRegimeClassification(unittest.TestCase):

    def test_bull_requires_positive_slope(self):
        """BULL 状态需 close>MA20 + MA20>MA60 + slope>=0。"""
        from core.regime import _compute_indicators, _classify_regime
        c, h, l = _bull_prices()
        d = _compute_indicators(c, h, l)
        info = _classify_regime(d, '2026-05-01')
        self.assertEqual(info.regime, 'BULL')
        self.assertGreaterEqual(info.ma60_slope, 0)

    def test_bear_requires_negative_slope(self):
        from core.regime import _compute_indicators, _classify_regime
        c, h, l = _bear_prices()
        d = _compute_indicators(c, h, l)
        info = _classify_regime(d, '2026-05-01')
        self.assertEqual(info.regime, 'BEAR')
        self.assertLess(info.ma60_slope, 0)

    def test_short_pullback_with_positive_slope_no_bear(self):
        """
        构造场景：长期上涨 + 末端 8 天回调 → close < MA20，但 MA60 斜率仍 > 0。
        旧逻辑（无斜率确认）会判 BEAR；新逻辑应不判 BEAR。
        """
        from core.regime import _compute_indicators, _classify_regime

        rng = np.random.default_rng(31)
        # 312 天 +0.15% 漂移 + 末端 8 天 -2% 急跌
        rets = np.concatenate([
            rng.normal(0.0015, 0.005, 312),
            rng.normal(-0.020, 0.005, 8),
        ])
        closes = 1000 * np.cumprod(1 + rets)
        highs = closes * 1.005
        lows = closes * 0.995
        d = _compute_indicators(closes, highs, lows)
        # 30 日斜率应仍为正（8 天回调不足以让 30 日 MA60 反转）
        self.assertGreater(d['ma60_slope'], 0,
                           f"setup invalid: ma60_slope={d['ma60_slope']}")
        # close 应跌破 MA20（短期回调）
        self.assertLess(closes[-1], d['ma20'][-1])
        info = _classify_regime(d, '2026-05-01')
        # 关键断言：旧逻辑会判 BEAR，新逻辑应不判 BEAR
        self.assertNotEqual(info.regime, 'BEAR',
                            f"slope>0 + short pullback should not be BEAR, got {info.reason}")

    def test_volatile_uses_dynamic_threshold(self):
        """
        ATR 超动态阈值且不满足 BULL/BEAR → VOLATILE。

        手工构造 indicator dict：close 高于 MA20 但 MA60 斜率为负
        （不满足 BULL 的"slope>=0"），ATR 高位 → VOLATILE。
        """
        from core.regime import _classify_regime
        d = {
            'closes': np.array([100.0]),
            'ma20': np.array([99.0]),    # close > MA20
            'ma60': np.array([98.0]),    # MA20 > MA60 → 但 slope<0 阻断 BULL
            'atr_ratio': 0.95,           # 高
            'atr': 0.5,
            'atr_threshold_dynamic': 0.80,
            'ma60_slope': -0.005,        # 负斜率：不满足 BULL
        }
        info = _classify_regime(d, '2026-05-01')
        self.assertEqual(info.regime, 'VOLATILE',
                         f"expected VOLATILE, got {info.regime}: {info.reason}")


class TestRegimeReduceTarget(unittest.TestCase):

    def test_bear_should_reduce(self):
        from core.regime import RegimeInfo, BEAR_POSITION_REDUCE_PCT
        info = RegimeInfo(
            regime='BEAR', close=1000, ma20=1010, ma60=1020,
            atr_ratio=0.5, atr=10, reason='', date_str='2026-05-01',
        )
        self.assertTrue(info.should_reduce_positions)
        self.assertAlmostEqual(info.position_reduce_target_pct, BEAR_POSITION_REDUCE_PCT)

    def test_non_bear_no_reduce(self):
        from core.regime import RegimeInfo
        for regime in ('BULL', 'VOLATILE', 'CALM'):
            info = RegimeInfo(
                regime=regime, close=1000, ma20=1010, ma60=1020,
                atr_ratio=0.5, atr=10, reason='', date_str='2026-05-01',
            )
            self.assertFalse(info.should_reduce_positions, regime)
            self.assertEqual(info.position_reduce_target_pct, 1.0, regime)


class TestRegimeCooldown(unittest.TestCase):

    def setUp(self):
        from core.regime import reset_state
        reset_state()

    def tearDown(self):
        from core.regime import reset_state
        reset_state()

    def test_first_call_sets_persistent_regime(self):
        """首次调用应立即固化检测结果。"""
        from core.regime import get_regime, _compute_indicators, _classify_regime
        import core.regime as mod

        c, h, l = _bull_prices()
        d = _compute_indicators(c, h, l)
        bull_info = _classify_regime(d, date.today().isoformat())

        with patch.object(mod, 'detect_regime', return_value=bull_info):
            info = get_regime(force_refresh=True)
        self.assertEqual(info.regime, 'BULL')

    def test_cooldown_blocks_immediate_switch(self):
        """冷却期内的反向切换应被压制。"""
        from core.regime import get_regime
        import core.regime as mod

        # 第一次：BULL
        c, h, l = _bull_prices()
        d = mod._compute_indicators(c, h, l)
        bull_info = mod._classify_regime(d, date.today().isoformat())
        with patch.object(mod, 'detect_regime', return_value=bull_info):
            info1 = get_regime(force_refresh=True)
        self.assertEqual(info1.regime, 'BULL')

        # 第二次：检测为 BEAR，但冷却期内 → 仍返回 BULL
        c, h, l = _bear_prices()
        d2 = mod._compute_indicators(c, h, l)
        bear_info = mod._classify_regime(d2, date.today().isoformat())
        with patch.object(mod, 'detect_regime', return_value=bear_info):
            # 强制刷新但走冷却逻辑（注意 _cache_date 与 today 相同所以不刷新缓存，
            # 必须使日期前进，所以我们用 reset_state 后立即第二次检测）
            mod._cache = None
            mod._cache_date = None
            info2 = get_regime(force_refresh=True)
        # 同一天且距上次切换 0 个交易日 < 5 → 保持 BULL
        self.assertEqual(info2.regime, 'BULL')
        self.assertIn('冷却期', info2.reason)


if __name__ == '__main__':
    unittest.main()
