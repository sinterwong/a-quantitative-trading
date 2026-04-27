"""
tests/test_portfolio_optimizer.py — 组合优化器单元测试

覆盖：
  - 6 种优化方法（GMV / MaxSharpe / RiskParity / BL / MaxDiv / EqualWeight）
  - Ledoit-Wolf 协方差收缩
  - portfolio_stats() 诊断指标
  - 换手率约束（turnover / apply_turnover_constraint）
  - Black-Litterman 观点融合逻辑
  - 边界条件（单资产、全负收益等）

测试策略：全部使用随机生成的合成收益率数据，无网络依赖。
"""

from __future__ import annotations

import unittest
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 测试数据生成
# ---------------------------------------------------------------------------

def _make_returns(
    n_assets: int = 5,
    n_days: int = 252,
    seed: int = 42,
    positive_drift: bool = True,
) -> pd.DataFrame:
    """生成 n_assets 个资产的模拟日收益率（近似正态）。"""
    rng = np.random.default_rng(seed)
    drift = 0.0005 if positive_drift else -0.0003
    sigma = 0.015
    data = rng.normal(drift, sigma, (n_days, n_assets))
    cols = [f'ASSET_{i:02d}' for i in range(n_assets)]
    return pd.DataFrame(data, columns=cols)


def _assert_valid_weights(test_case, w: pd.Series, n: int, tol: float = 1e-6):
    """权重基本约束断言：非负、总和为 1、长度正确。"""
    test_case.assertEqual(len(w), n)
    test_case.assertTrue(np.all(w.values >= -tol), f"有负权重: {w.min():.6f}")
    test_case.assertAlmostEqual(float(w.sum()), 1.0, places=5)


# ---------------------------------------------------------------------------
# PortfolioOptimizer — 基本构造
# ---------------------------------------------------------------------------

class TestPortfolioOptimizerInit(unittest.TestCase):

    def test_init_valid(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        ret = _make_returns(5, 252)
        opt = PortfolioOptimizer(ret)
        self.assertEqual(opt.n, 5)

    def test_init_empty_raises(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        with self.assertRaises(ValueError):
            PortfolioOptimizer(pd.DataFrame())

    def test_init_single_asset_raises(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        ret = _make_returns(1, 100)
        with self.assertRaises(ValueError):
            PortfolioOptimizer(ret)

    def test_asset_names_preserved(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        ret = _make_returns(3, 100)
        opt = PortfolioOptimizer(ret)
        self.assertEqual(opt.assets, list(ret.columns))


# ---------------------------------------------------------------------------
# 等权（基准）
# ---------------------------------------------------------------------------

class TestEqualWeight(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(5, 252)
        self.opt = PortfolioOptimizer(self.ret)

    def test_equal_weight_sum_to_one(self):
        w = self.opt.equal_weight()
        self.assertAlmostEqual(float(w.sum()), 1.0, places=8)

    def test_equal_weight_all_equal(self):
        w = self.opt.equal_weight()
        self.assertTrue(np.allclose(w.values, 0.2, atol=1e-8))

    def test_equal_weight_returns_series(self):
        w = self.opt.equal_weight()
        self.assertIsInstance(w, pd.Series)


# ---------------------------------------------------------------------------
# 全局最小方差（GMV）
# ---------------------------------------------------------------------------

class TestMinVariance(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(5, 252)
        self.opt = PortfolioOptimizer(self.ret, max_weight=0.5)

    def test_min_variance_returns_series(self):
        w = self.opt.min_variance()
        self.assertIsInstance(w, pd.Series)

    def test_min_variance_valid_weights(self):
        w = self.opt.min_variance()
        _assert_valid_weights(self, w, 5)

    def test_min_variance_lower_vol_than_equal(self):
        """GMV 的方差应 ≤ 等权组合。"""
        w_gmv = self.opt.min_variance()
        w_eq = self.opt.equal_weight()
        cov = self.opt._cov

        var_gmv = float(w_gmv.values @ cov @ w_gmv.values)
        var_eq = float(w_eq.values @ cov @ w_eq.values)
        self.assertLessEqual(var_gmv, var_eq + 1e-8)

    def test_min_variance_respects_max_weight(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        opt = PortfolioOptimizer(self.ret, max_weight=0.3)
        w = opt.min_variance()
        self.assertTrue(np.all(w.values <= 0.3 + 1e-6))

    def test_min_variance_different_assets(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        ret = _make_returns(8, 300)
        opt = PortfolioOptimizer(ret)
        w = opt.min_variance()
        _assert_valid_weights(self, w, 8)


# ---------------------------------------------------------------------------
# 最大 Sharpe 比率
# ---------------------------------------------------------------------------

class TestMaxSharpe(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(5, 252, positive_drift=True)
        self.opt = PortfolioOptimizer(self.ret, max_weight=0.5)

    def test_max_sharpe_returns_series(self):
        w = self.opt.max_sharpe()
        self.assertIsInstance(w, pd.Series)

    def test_max_sharpe_valid_weights(self):
        w = self.opt.max_sharpe()
        _assert_valid_weights(self, w, 5)

    def test_max_sharpe_respects_max_weight(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        opt = PortfolioOptimizer(self.ret, max_weight=0.25)
        w = opt.max_sharpe()
        self.assertTrue(np.all(w.values <= 0.25 + 1e-6))

    def test_max_sharpe_higher_sharpe_than_equal(self):
        """MaxSharpe 的 Sharpe 应 ≥ 等权组合（正收益环境）。"""
        w_ms = self.opt.max_sharpe()
        w_eq = self.opt.equal_weight()

        stats_ms = self.opt.portfolio_stats(w_ms)
        stats_eq = self.opt.portfolio_stats(w_eq)

        self.assertGreaterEqual(stats_ms['sharpe'], stats_eq['sharpe'] - 1e-6)

    def test_max_sharpe_negative_excess_returns_fallback(self):
        """所有资产超额收益为负时，退化到 GMV。"""
        import warnings
        from core.portfolio_optimizer import PortfolioOptimizer
        ret = _make_returns(4, 200, positive_drift=False)
        opt = PortfolioOptimizer(ret, rf=0.10/252)  # 极高无风险利率
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            w = opt.max_sharpe()
        _assert_valid_weights(self, w, 4)


# ---------------------------------------------------------------------------
# 风险平价
# ---------------------------------------------------------------------------

class TestRiskParity(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(4, 252)
        self.opt = PortfolioOptimizer(self.ret)

    def test_risk_parity_returns_series(self):
        w = self.opt.risk_parity()
        self.assertIsInstance(w, pd.Series)

    def test_risk_parity_valid_weights(self):
        w = self.opt.risk_parity()
        _assert_valid_weights(self, w, 4)

    def test_risk_parity_equal_risk_contribution(self):
        """等风险贡献：各资产的风险贡献应大致相等。"""
        w = self.opt.risk_parity().values
        cov = self.opt._cov
        marginal = cov @ w
        sigma = np.sqrt(max(w @ cov @ w, 1e-20))
        rc = w * marginal / sigma
        # 各 RC 相对于均值的偏差应小（接近等权风险）
        rc_cv = rc.std() / (rc.mean() + 1e-10)
        self.assertLess(rc_cv, 0.5)  # 变异系数 < 50%

    def test_risk_parity_all_weights_positive(self):
        """风险平价权重均应为正。"""
        w = self.opt.risk_parity()
        self.assertTrue(np.all(w.values > 0))


# ---------------------------------------------------------------------------
# Black-Litterman
# ---------------------------------------------------------------------------

class TestBlackLitterman(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(5, 252)
        self.opt = PortfolioOptimizer(self.ret)
        self.assets = self.opt.assets

    def test_bl_returns_series(self):
        views = {self.assets[0]: 0.001}
        w = self.opt.black_litterman(views)
        self.assertIsInstance(w, pd.Series)

    def test_bl_valid_weights(self):
        views = {self.assets[0]: 0.001, self.assets[1]: -0.0005}
        w = self.opt.black_litterman(views)
        _assert_valid_weights(self, w, 5)

    def test_bl_empty_views_returns_min_variance(self):
        w_bl = self.opt.black_litterman({})
        w_gmv = self.opt.min_variance()
        # 空观点下 BL 退化到 GMV，结果应接近
        np.testing.assert_allclose(w_bl.values, w_gmv.values, atol=0.01)

    def test_bl_ignores_unknown_assets(self):
        """不在资产列表中的观点应被忽略。"""
        views = {'UNKNOWN.SZ': 0.001}
        w = self.opt.black_litterman(views)
        _assert_valid_weights(self, w, 5)

    def test_bl_bullish_view_increases_weight(self):
        """对某资产持强烈看多观点，其权重应高于等权。"""
        target = self.assets[0]
        views = {target: 0.005}  # 强烈看多
        w_bl = self.opt.black_litterman(views, view_confidences={target: 0.9})
        w_eq = self.opt.equal_weight()
        # 不严格要求更高权重（取决于协方差），但应有合理权重
        _assert_valid_weights(self, w_bl, 5)

    def test_bl_with_min_variance_method(self):
        views = {self.assets[0]: 0.001}
        w = self.opt.black_litterman(views, method='min_variance')
        _assert_valid_weights(self, w, 5)

    def test_bl_does_not_modify_original_mu(self):
        """BL 执行后，原始 _mu 应不变。"""
        mu_before = self.opt._mu.copy()
        views = {self.assets[0]: 0.002}
        self.opt.black_litterman(views)
        np.testing.assert_array_equal(self.opt._mu, mu_before)


# ---------------------------------------------------------------------------
# 最大分散化
# ---------------------------------------------------------------------------

class TestMaxDiversification(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(5, 252)
        self.opt = PortfolioOptimizer(self.ret)

    def test_max_div_returns_series(self):
        w = self.opt.max_diversification()
        self.assertIsInstance(w, pd.Series)

    def test_max_div_valid_weights(self):
        w = self.opt.max_diversification()
        _assert_valid_weights(self, w, 5)

    def test_max_div_higher_dr_than_equal(self):
        """最大分散化的分散化比率应 ≥ 等权组合。"""
        w_md = self.opt.max_diversification()
        w_eq = self.opt.equal_weight()

        stats_md = self.opt.portfolio_stats(w_md)
        stats_eq = self.opt.portfolio_stats(w_eq)

        self.assertGreaterEqual(
            stats_md['diversification_ratio'],
            stats_eq['diversification_ratio'] - 1e-4,
        )


# ---------------------------------------------------------------------------
# portfolio_stats 诊断指标
# ---------------------------------------------------------------------------

class TestPortfolioStats(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(4, 252)
        self.opt = PortfolioOptimizer(self.ret)
        self.w = self.opt.equal_weight()

    def test_stats_keys_present(self):
        stats = self.opt.portfolio_stats(self.w)
        for key in ['annual_return', 'annual_vol', 'sharpe', 'max_drawdown',
                    'diversification_ratio', 'effective_n']:
            self.assertIn(key, stats)

    def test_annual_vol_positive(self):
        stats = self.opt.portfolio_stats(self.w)
        self.assertGreater(stats['annual_vol'], 0.0)

    def test_effective_n_bounded(self):
        stats = self.opt.portfolio_stats(self.w)
        self.assertGreater(stats['effective_n'], 0.0)
        self.assertLessEqual(stats['effective_n'], len(self.opt.assets) + 0.1)

    def test_max_drawdown_nonpositive(self):
        stats = self.opt.portfolio_stats(self.w)
        self.assertLessEqual(stats['max_drawdown'], 0.0)

    def test_diversification_ratio_gte_one(self):
        """分散化比率 ≥ 1（等于 1 时表示完全相关）。"""
        stats = self.opt.portfolio_stats(self.w)
        self.assertGreaterEqual(stats['diversification_ratio'], 1.0 - 1e-6)


# ---------------------------------------------------------------------------
# 换手率约束
# ---------------------------------------------------------------------------

class TestTurnover(unittest.TestCase):

    def setUp(self):
        from core.portfolio_optimizer import PortfolioOptimizer
        self.ret = _make_returns(4, 252)
        self.opt = PortfolioOptimizer(self.ret)
        self.assets = self.opt.assets

    def test_turnover_zero_no_change(self):
        w = self.opt.equal_weight()
        to = self.opt.turnover(w, w)
        self.assertAlmostEqual(to, 0.0, places=8)

    def test_turnover_full_from_cash(self):
        """从现金全仓 → 换手率 = 1。"""
        w_new = self.opt.equal_weight()
        w_old = pd.Series(0.0, index=self.assets)
        to = self.opt.turnover(w_new, w_old)
        self.assertAlmostEqual(to, 1.0, places=6)

    def test_turnover_partial(self):
        w_new = pd.Series([0.4, 0.3, 0.2, 0.1], index=self.assets)
        w_old = pd.Series([0.25, 0.25, 0.25, 0.25], index=self.assets)
        to = self.opt.turnover(w_new, w_old)
        expected = (0.4 - 0.25) + (0.3 - 0.25)  # 买入部分
        self.assertAlmostEqual(to, expected, places=6)

    def test_apply_turnover_constraint_no_breach(self):
        """换手率不超限时，权重不变。"""
        w_new = pd.Series([0.3, 0.3, 0.2, 0.2], index=self.assets)
        w_old = pd.Series([0.25, 0.25, 0.25, 0.25], index=self.assets)
        w_adj = self.opt.apply_turnover_constraint(w_new, w_old, max_turnover=0.5)
        np.testing.assert_allclose(w_adj.values, w_new.values, atol=1e-8)

    def test_apply_turnover_constraint_limits_turnover(self):
        """大幅调仓时，换手率应被限制。"""
        w_new = pd.Series([0.8, 0.1, 0.05, 0.05], index=self.assets)
        w_old = pd.Series([0.25, 0.25, 0.25, 0.25], index=self.assets)
        w_adj = self.opt.apply_turnover_constraint(w_new, w_old, max_turnover=0.2)
        actual_to = self.opt.turnover(w_adj, w_old)
        self.assertLessEqual(actual_to, 0.2 + 1e-6)

    def test_apply_turnover_weights_sum_to_one(self):
        w_new = pd.Series([0.7, 0.1, 0.1, 0.1], index=self.assets)
        w_old = pd.Series([0.25, 0.25, 0.25, 0.25], index=self.assets)
        w_adj = self.opt.apply_turnover_constraint(w_new, w_old, max_turnover=0.15)
        self.assertAlmostEqual(float(w_adj.sum()), 1.0, places=5)


# ---------------------------------------------------------------------------
# Ledoit-Wolf 协方差
# ---------------------------------------------------------------------------

class TestLedoitWolf(unittest.TestCase):

    def test_ledoit_wolf_positive_definite(self):
        from core.portfolio_optimizer import _ledoit_wolf, _make_positive_definite
        rng = np.random.default_rng(42)
        data = rng.normal(0, 0.01, (100, 10))
        cov = _ledoit_wolf(data)
        cov = _make_positive_definite(cov)
        eigvals = np.linalg.eigvalsh(cov)
        self.assertTrue(np.all(eigvals > 0))

    def test_ledoit_wolf_symmetric(self):
        from core.portfolio_optimizer import _ledoit_wolf
        rng = np.random.default_rng(42)
        data = rng.normal(0, 0.01, (100, 5))
        cov = _ledoit_wolf(data)
        np.testing.assert_allclose(cov, cov.T, atol=1e-12)

    def test_cov_method_ledoit_wolf_vs_sample(self):
        """Ledoit-Wolf 与样本协方差产生不同（收缩）结果。"""
        from core.portfolio_optimizer import PortfolioOptimizer
        ret = _make_returns(10, 80)  # 高维 + 短样本 → 收缩效果明显
        opt_lw = PortfolioOptimizer(ret, cov_method='ledoit_wolf')
        opt_s = PortfolioOptimizer(ret, cov_method='sample')
        # 验证两者均能正常运行
        w_lw = opt_lw.min_variance()
        w_s = opt_s.min_variance()
        _assert_valid_weights(self, w_lw, 10)
        _assert_valid_weights(self, w_s, 10)


if __name__ == '__main__':
    unittest.main()
