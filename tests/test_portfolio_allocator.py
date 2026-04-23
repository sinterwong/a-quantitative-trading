"""tests/test_portfolio_allocator.py — PortfolioAllocator 单元测试"""

from __future__ import annotations

import unittest

from core.portfolio_allocator import (
    AllocConfig,
    PortfolioAllocator,
    StrategyAccount,
    WeightMode,
)


class TestEqualWeightMode(unittest.TestCase):

    def setUp(self):
        self.alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.EQUAL, reserve_ratio=0.0),
        )
        self.alloc.add_strategy('A').add_strategy('B').add_strategy('C')

    def test_equal_weights_sum_to_one(self):
        weights = self.alloc.get_weights()
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_equal_weights_are_equal(self):
        weights = self.alloc.get_weights()
        values = list(weights.values())
        self.assertAlmostEqual(values[0], values[1], places=6)
        self.assertAlmostEqual(values[1], values[2], places=6)

    def test_budgets_sum_to_capital(self):
        budgets = self.alloc.get_budgets()
        self.assertAlmostEqual(sum(budgets.values()), 1_000_000, places=0)

    def test_three_strategies_each_get_third(self):
        budgets = self.alloc.get_budgets()
        for v in budgets.values():
            self.assertAlmostEqual(v, 333_333.33, delta=1)

    def test_get_available_equals_budget_when_no_usage(self):
        avail = self.alloc.get_available()
        budgets = self.alloc.get_budgets()
        for name in budgets:
            self.assertAlmostEqual(avail[name], budgets[name], places=1)


class TestFixedWeightMode(unittest.TestCase):

    def setUp(self):
        self.alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.FIXED, reserve_ratio=0.0),
        )
        self.alloc.add_strategy('RSI',  weight=0.5)
        self.alloc.add_strategy('MACD', weight=0.3)
        self.alloc.add_strategy('OI',   weight=0.2)

    def test_weights_sum_to_one(self):
        weights = self.alloc.get_weights()
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=5)

    def test_rsi_largest_budget(self):
        budgets = self.alloc.get_budgets()
        self.assertGreater(budgets['RSI'], budgets['MACD'])
        self.assertGreater(budgets['MACD'], budgets['OI'])

    def test_budgets_reflect_weights(self):
        budgets = self.alloc.get_budgets()
        weights = self.alloc.get_weights()
        for name in budgets:
            self.assertAlmostEqual(budgets[name], 1_000_000 * weights[name], delta=1)


class TestReserveRatio(unittest.TestCase):

    def test_reserve_reduces_deployable(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(reserve_ratio=0.10, mode=WeightMode.EQUAL),
        )
        alloc.add_strategy('X').add_strategy('Y')
        budgets = alloc.get_budgets()
        # 10% reserve → 900_000 deployed
        self.assertAlmostEqual(sum(budgets.values()), 900_000, places=0)

    def test_summary_shows_reserve(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(reserve_ratio=0.05),
        )
        alloc.add_strategy('X')
        s = alloc.summary()
        self.assertAlmostEqual(s['reserve'], 50_000, places=0)


class TestUsageAndAvailable(unittest.TestCase):

    def setUp(self):
        self.alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.EQUAL, reserve_ratio=0.0),
        )
        self.alloc.add_strategy('A').add_strategy('B')

    def test_update_usage_reduces_available(self):
        self.alloc.update_usage('A', 200_000)
        avail = self.alloc.get_available()
        budget_a = self.alloc.get_budgets()['A']
        self.assertAlmostEqual(avail['A'], budget_a - 200_000, places=1)

    def test_utilization_after_update(self):
        self.alloc.update_usage('A', 200_000)
        acc = self.alloc.get_account('A')
        self.assertAlmostEqual(acc.utilization, 200_000 / acc.budget, places=4)


class TestNeedsRebalance(unittest.TestCase):

    def setUp(self):
        self.alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(
                mode=WeightMode.EQUAL,
                reserve_ratio=0.0,
                rebalance_threshold=0.05,
            ),
        )
        self.alloc.add_strategy('A').add_strategy('B')

    def test_no_rebalance_needed_when_balanced(self):
        # 各 50%, 无偏离
        mv = {'A': 500_000, 'B': 500_000}
        self.assertFalse(self.alloc.needs_rebalance(mv))

    def test_rebalance_needed_when_large_drift(self):
        # A=80%, B=20% → 偏离 30% > 5% 阈值
        mv = {'A': 800_000, 'B': 200_000}
        self.assertTrue(self.alloc.needs_rebalance(mv))

    def test_no_rebalance_when_no_usage(self):
        self.assertFalse(self.alloc.needs_rebalance())


class TestRebalance(unittest.TestCase):

    def test_rebalance_returns_budgets_dict(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.EQUAL, reserve_ratio=0.0),
        )
        alloc.add_strategy('A').add_strategy('B')
        result = alloc.rebalance(trigger='manual')
        self.assertIsInstance(result, dict)
        self.assertIn('A', result)
        self.assertIn('B', result)

    def test_rebalance_history_grows(self):
        alloc = PortfolioAllocator(total_capital=1_000_000)
        alloc.add_strategy('A').add_strategy('B')
        alloc.rebalance()
        alloc.rebalance()
        s = alloc.summary()
        self.assertEqual(s['n_rebalances'], 2)

    def test_rebalance_updates_used(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.EQUAL, reserve_ratio=0.0),
        )
        alloc.add_strategy('A').add_strategy('B')
        alloc.rebalance(current_mv={'A': 600_000, 'B': 400_000})
        acc_a = alloc.get_account('A')
        self.assertAlmostEqual(acc_a.used, 600_000, places=0)


class TestRemoveStrategy(unittest.TestCase):

    def test_remove_strategy_rebalances(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.EQUAL, reserve_ratio=0.0),
        )
        alloc.add_strategy('A').add_strategy('B').add_strategy('C')
        alloc.remove_strategy('C')
        weights = alloc.get_weights()
        self.assertNotIn('C', weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=5)

    def test_remove_nonexistent_is_safe(self):
        alloc = PortfolioAllocator(total_capital=1_000_000)
        alloc.add_strategy('A')
        alloc.remove_strategy('NOPE')  # should not raise
        self.assertEqual(len(alloc.get_weights()), 1)


class TestRiskParityMode(unittest.TestCase):

    def test_risk_parity_weights_sum_to_one(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.RISK_PARITY, reserve_ratio=0.0),
        )
        alloc.add_strategy('A').add_strategy('B').add_strategy('C')
        # 没有收益历史 → 各策略 vol=1.0 → 等权
        weights = alloc.get_weights()
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=5)

    def test_risk_parity_lower_vol_gets_higher_weight(self):
        import random
        random.seed(42)
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(
                mode=WeightMode.RISK_PARITY,
                reserve_ratio=0.0,
                min_strategy_weight=0.01,
                max_strategy_weight=0.99,
            ),
        )
        alloc.add_strategy('LowVol').add_strategy('HighVol')
        # LowVol: 小幅随机波动; HighVol: 大幅随机波动（std 差 10 倍）
        for _ in range(30):
            alloc.record_return('LowVol',  random.gauss(0, 0.002))
            alloc.record_return('HighVol', random.gauss(0, 0.020))
        alloc.rebalance(trigger='periodic')
        weights = alloc.get_weights()
        self.assertGreater(weights['LowVol'], weights['HighVol'])


class TestStrategyAccountVolatility(unittest.TestCase):

    def test_volatility_returns_one_with_few_data(self):
        acc = StrategyAccount(name='X', target_weight=0.5, budget=100_000)
        self.assertAlmostEqual(acc.volatility, 1.0)

    def test_volatility_computed_after_enough_data(self):
        acc = StrategyAccount(name='X', target_weight=0.5, budget=100_000)
        for _ in range(30):
            acc.update_return(0.01)
        self.assertGreater(acc.volatility, 0)
        self.assertLess(acc.volatility, 10)

    def test_daily_returns_capped_at_252(self):
        acc = StrategyAccount(name='X', target_weight=0.5, budget=100_000)
        for _ in range(300):
            acc.update_return(0.001)
        self.assertEqual(len(acc.daily_returns), 252)


class TestSummaryAndSave(unittest.TestCase):

    def test_summary_structure(self):
        alloc = PortfolioAllocator(
            total_capital=1_000_000,
            config=AllocConfig(mode=WeightMode.EQUAL),
        )
        alloc.add_strategy('A').add_strategy('B')
        s = alloc.summary()
        self.assertIn('total_capital', s)
        self.assertIn('strategies', s)
        self.assertIn('n_strategies', s)
        self.assertEqual(s['n_strategies'], 2)

    def test_save_history_creates_file(self):
        import os, tempfile
        alloc = PortfolioAllocator(total_capital=500_000)
        alloc.add_strategy('X').add_strategy('Y')
        alloc.rebalance()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            saved = alloc.save_history(path)
            self.assertTrue(os.path.exists(saved))
            import json
            with open(saved, encoding='utf-8') as f:
                data = json.load(f)
            self.assertIn('rebalance_history', data)
            self.assertEqual(len(data['rebalance_history']), 1)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
