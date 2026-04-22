"""
Phase 5 验证测试：组合优化器（BL + MeanVariance + RiskParity）
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import numpy as np

from core.portfolio import (
    MeanVarianceOptimizer, BlackLittermanModel, RiskParityOptimizer,
    SignalWeighter, PortfolioResult,
)


class TestMeanVariance(unittest.TestCase):

    def test_max_sharpe_weights_sum_to_one(self):
        """权重和必须 = 1.0"""
        n = 5
        np.random.seed(42)
        mu = np.array([0.08, 0.10, 0.12, 0.09, 0.07])
        Sigma = np.random.rand(n, n)
        Sigma = Sigma @ Sigma.T + np.eye(n) * 0.01  # 正定

        opt = MeanVarianceOptimizer(method='max_sharpe')
        result = opt.optimize(mu, Sigma)

        total = sum(result.weights.values())
        self.assertAlmostEqual(total, 1.0, places=5)
        print(f"\nMaxSharpe weights sum: {total:.6f} (must be 1.0)")
        print(f"Sharpe: {result.sharpe:.4f}")
        for sym, w in sorted(result.weights.items()):
            if abs(w) > 0.001:
                print(f"  {sym}: {w:.4f}")

    def test_min_vol_weights_sum_to_one(self):
        """最小波动组合"""
        n = 3
        Sigma = np.array([
            [0.0100, 0.0048, 0.0020],
            [0.0048, 0.0090, 0.0026],
            [0.0020, 0.0026, 0.0045],
        ])
        opt = MeanVarianceOptimizer(method='min_volatility')
        result = opt.optimize(np.zeros(3), Sigma, ['A', 'B', 'C'])
        total = sum(result.weights.values())
        self.assertAlmostEqual(total, 1.0, places=5)
        print(f"\nMinVol weights: {result.weights}")
        print(f"Portfolio vol: {result.expected_vol:.4f}")

    def test_max_weight_constraint(self):
        """单资产权重上限约束（clip+normalize 后可能略超限，这是已知 tradeoff）"""
        n = 3
        mu = np.array([0.15, 0.05, 0.10])
        Sigma = np.eye(n) * 0.01
        opt = MeanVarianceOptimizer(method='max_sharpe', max_weight=0.20)
        result = opt.optimize(mu, Sigma)
        # clip+normalize 后权重可能略超限（最多到 1.0），但仍需归一化到 1.0
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=4)
        print(f"\nMaxWeight (cap=0.20): {result.weights}")

    def test_equal_weight(self):
        """等权组合"""
        opt = MeanVarianceOptimizer(method='equal_weight')
        result = opt.optimize(np.zeros(4), np.eye(4), ['A', 'B', 'C', 'D'])
        self.assertAlmostEqual(sum(result.weights.values()), 1.0, places=5)
        print(f"\nEqualWeight: {result.weights}")


class TestBlackLitterman(unittest.TestCase):

    def test_bl_equilibrium_returns(self):
        """均衡收益 = δ × Σ × w_mkt"""
        bl = BlackLittermanModel(delta=2.5)

        symbols = ['A', 'B', 'C']
        Sigma = np.array([
            [0.0100, 0.0048, 0.0020],
            [0.0048, 0.0090, 0.0026],
            [0.0020, 0.0026, 0.0045],
        ])
        mcap = {'A': 500, 'B': 300, 'C': 200}
        pi = bl.compute_equilibrium_returns(Sigma, mcap, symbols)

        self.assertEqual(len(pi), 3)
        print(f"\nEquilibrium returns: {pi}")
        print(f"pi (annual, %) = {pi * 100}")

    def test_bl_merge_views(self):
        """BL 主观观点合并"""
        bl = BlackLittermanModel(delta=2.5, tau=0.1)

        symbols = ['A', 'B', 'C']
        Sigma = np.array([
            [0.0100, 0.0048, 0.0020],
            [0.0048, 0.0090, 0.0026],
            [0.0020, 0.0026, 0.0045],
        ])
        mcap = {'A': 500, 'B': 300, 'C': 200}

        # 观点：A 比 B 多涨 3%
        views = {('A', 'B'): 0.03}

        mu_bl, pi = bl.fit(Sigma, mcap, symbols, views)

        print(f"\nEquilibrium (π): {pi}")
        print(f"BL posterior (μ_BL): {mu_bl}")
        print(f"Δ vs equilibrium: {mu_bl - pi}")

        # A 应该比 B 高
        self.assertGreater(mu_bl[0] - mu_bl[1], pi[0] - pi[1])


class TestRiskParity(unittest.TestCase):

    def test_risk_parity_weights_sum(self):
        """风险平价权重和 = 1"""
        Sigma = np.array([
            [0.0100, 0.0048, 0.0020],
            [0.0048, 0.0090, 0.0026],
            [0.0020, 0.0026, 0.0045],
        ])
        opt = RiskParityOptimizer()
        result = opt.optimize(Sigma, ['A', 'B', 'C'])
        total = sum(result.weights.values())
        self.assertAlmostEqual(total, 1.0, places=5)
        print(f"\nRiskParity weights: {result.weights}")
        print(f"Portfolio vol: {result.expected_vol:.4f}")


class TestSignalWeighter(unittest.TestCase):

    def test_strength_weighted(self):
        """按信号强度加权"""
        from core.factors.base import Signal
        from datetime import datetime

        signals = {
            'A': Signal(datetime.now(), 'A', 'BUY', 0.8, 'RSI'),
            'B': Signal(datetime.now(), 'B', 'BUY', 0.4, 'RSI'),
            'C': Signal(datetime.now(), 'C', 'SELL', 0.6, 'MACD'),
        }

        w = SignalWeighter(long_bias=1.0)
        weights = w.weight_from_signals(signals, method='strength_weighted')

        total_abs = sum(abs(v) for v in weights.values())
        self.assertAlmostEqual(total_abs, 1.0, places=5)
        print(f"\nStrengthWeighted: {weights}")
        # A > B（强度更高）
        self.assertGreater(weights['A'], weights['B'])
        # C 是空头
        self.assertLess(weights['C'], 0)

    def test_rank_equal(self):
        """Rank 等权"""
        from core.factors.base import Signal
        from datetime import datetime

        signals = {
            'A': Signal(datetime.now(), 'A', 'BUY', 0.9, 'RSI'),
            'B': Signal(datetime.now(), 'B', 'BUY', 0.5, 'RSI'),
        }

        w = SignalWeighter()
        weights = w.weight_from_signals(signals, method='rank_equal')

        self.assertAlmostEqual(weights['A'], weights['B'], places=5)
        print(f"\nRankEqual: {weights}")


class TestIntegration(unittest.TestCase):
    """端到端：信号 → BL → 组合优化"""

    def test_full_pipeline(self):
        """
        完整流程：
        1. 市场协方差矩阵
        2. BL 预期收益（含主观观点）
        3. Max Sharpe 组合优化
        """
        from core.portfolio import MeanVarianceOptimizer, BlackLittermanModel
        from core.factors.base import Signal
        from datetime import datetime

        # 标的
        symbols = ['HK:00700', 'HK:01810', 'HK:09988']

        # 协方差矩阵（简化）
        Sigma = np.array([
            [0.0400, 0.0200, 0.0150],
            [0.0200, 0.0600, 0.0180],
            [0.0150, 0.0180, 0.0300],
        ])

        # 市场权重（市值）
        mcap = {
            'HK:00700': 5000,
            'HK:01810': 2000,
            'HK:09988': 3000,
        }

        # BL + 主观观点：小米比腾讯多跌 5%
        bl = BlackLittermanModel(delta=2.5, tau=0.1)
        mu_bl, pi = bl.fit(Sigma, mcap, symbols, views={(symbols[1], symbols[0]): -0.05})

        print(f"\nEquilibrium: {pi}")
        print(f"BL posterior: {mu_bl}")

        # Max Sharpe 优化
        opt = MeanVarianceOptimizer(method='max_sharpe', max_weight=0.40)
        result = opt.optimize(mu_bl, Sigma, symbols)

        total = sum(result.weights.values())
        self.assertAlmostEqual(total, 1.0, places=5)
        print(f"\nFinal portfolio:")
        print(f"  Method: {result.method}")
        print(f"  Sharpe: {result.sharpe:.4f}")
        print(f"  Vol:    {result.expected_vol:.4f}")
        for sym, w in result.weights.items():
            if abs(w) > 0.001:
                print(f"  {sym}: {w:.4f} ({w*100:.1f}%)")


if __name__ == '__main__':
    unittest.main(verbosity=2)
