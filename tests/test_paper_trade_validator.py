"""tests/test_paper_trade_validator.py — PaperTradeValidator 单元测试"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import List

from core.brokers.simulated import SimulatedBroker, SimConfig
from core.paper_trade_validator import (
    PaperTradeValidator,
    TradeComparison,
    ValidationReport,
)


# ---------------------------------------------------------------------------
# 辅助：最简 BacktestResult 伪造
# ---------------------------------------------------------------------------

@dataclass
class FakeTrade:
    symbol: str
    direction: str
    price: float
    shares: int


class FakeBtResult:
    def __init__(self, trades):
        self.trades = trades


def _broker(cash: float = 2_000_000) -> SimulatedBroker:
    b = SimulatedBroker(SimConfig(
        initial_cash=cash,
        price_source='manual',
        slippage_bps=5.0,
        commission_rate=0.0003,
        stamp_tax_rate=0.001,
        enforce_lot=True,
    ))
    b.connect()
    return b


# ---------------------------------------------------------------------------
# 测试 validate_from_signals
# ---------------------------------------------------------------------------

class TestValidateFromSignals(unittest.TestCase):

    def setUp(self):
        self.validator = PaperTradeValidator(threshold_bps=20.0)
        self.broker = _broker()

    def _signals(self):
        return [
            {'symbol': '600519.SH', 'direction': 'BUY', 'price': 1800.0, 'shares': 100},
            {'symbol': '000858.SZ', 'direction': 'BUY', 'price': 100.0,  'shares': 200},
        ]

    def test_returns_validation_report(self):
        for sig in self._signals():
            self.broker.set_quote(sig['symbol'], sig['price'])
        report = self.validator.validate_from_signals(self._signals(), self.broker)
        self.assertIsInstance(report, ValidationReport)

    def test_n_trades_matches_signals(self):
        report = self.validator.validate_from_signals(self._signals(), self.broker)
        self.assertEqual(report.n_trades, 2)

    def test_pass_rate_in_range(self):
        report = self.validator.validate_from_signals(self._signals(), self.broker)
        self.assertGreaterEqual(report.pass_rate, 0.0)
        self.assertLessEqual(report.pass_rate, 1.0)

    def test_slippage_bps_5_passes_threshold_20(self):
        """5 bps 滑点 < 20 bps 阈值，应通过。"""
        report = self.validator.validate_from_signals(self._signals(), self.broker)
        self.assertTrue(report.passed)

    def test_avg_deviation_positive_buy(self):
        """买入有正向滑点，均偏差应 > 0。"""
        sigs = [{'symbol': '600519.SH', 'direction': 'BUY', 'price': 1800.0, 'shares': 100}]
        report = self.validator.validate_from_signals(sigs, self.broker)
        self.assertGreater(report.avg_deviation_bps, 0)

    def test_sell_signal_comparison(self):
        """卖出信号应产生负向偏差（成交价 < 参考价）。"""
        sigs = [{'symbol': '600519.SH', 'direction': 'SELL', 'price': 1800.0, 'shares': 100}]
        report = self.validator.validate_from_signals(sigs, self.broker)
        self.assertEqual(report.n_trades, 1)
        comp = report.comparisons[0]
        self.assertLess(comp.deviation_bps, 0)

    def test_empty_signals_returns_failed_report(self):
        report = self.validator.validate_from_signals([], self.broker)
        self.assertFalse(report.passed)
        self.assertEqual(report.n_trades, 0)

    def test_comparisons_list_populated(self):
        report = self.validator.validate_from_signals(self._signals(), self.broker)
        self.assertEqual(len(report.comparisons), 2)
        self.assertIsInstance(report.comparisons[0], TradeComparison)


# ---------------------------------------------------------------------------
# 测试 validate_from_backtest
# ---------------------------------------------------------------------------

class TestValidateFromBacktest(unittest.TestCase):

    def setUp(self):
        self.validator = PaperTradeValidator(threshold_bps=20.0)
        self.broker = _broker()

    def test_empty_backtest_returns_failed_report(self):
        report = self.validator.validate_from_backtest(FakeBtResult([]), self.broker)
        self.assertFalse(report.passed)
        self.assertEqual(report.n_trades, 0)

    def test_buy_trade_passes(self):
        trades = [FakeTrade('600519.SH', 'BUY', 1800.0, 100)]
        report = self.validator.validate_from_backtest(FakeBtResult(trades), self.broker)
        self.assertEqual(report.n_trades, 1)
        self.assertTrue(report.passed)

    def test_sell_trade_produces_comparison(self):
        trades = [FakeTrade('600519.SH', 'SELL', 1800.0, 100)]
        report = self.validator.validate_from_backtest(FakeBtResult(trades), self.broker)
        self.assertEqual(report.n_trades, 1)


# ---------------------------------------------------------------------------
# 测试 classify_deviation
# ---------------------------------------------------------------------------

class TestClassifyDeviation(unittest.TestCase):

    def _cls(self, bps, direction='BUY'):
        return PaperTradeValidator._classify_deviation(bps, direction)

    def test_minimal(self):
        self.assertEqual(self._cls(3), 'minimal')

    def test_normal_slippage(self):
        self.assertEqual(self._cls(15), 'normal_slippage')

    def test_high_slippage(self):
        self.assertEqual(self._cls(35), 'high_slippage')

    def test_liquidity_impact(self):
        self.assertEqual(self._cls(75), 'liquidity_impact')

    def test_execution_delay(self):
        self.assertEqual(self._cls(150), 'execution_delay_or_halt')


# ---------------------------------------------------------------------------
# 测试大偏差归因
# ---------------------------------------------------------------------------

class TestLargeDeviationAttribution(unittest.TestCase):

    def test_high_slippage_broker_creates_large_deviations(self):
        """高滑点 broker (500 bps) 应触发大偏差归因。"""
        broker = SimulatedBroker(SimConfig(
            initial_cash=5_000_000,
            price_source='manual',
            slippage_bps=500.0,   # 极高滑点
            enforce_lot=True,
        ))
        broker.connect()
        validator = PaperTradeValidator(threshold_bps=20.0, large_dev_bps=50.0)
        sigs = [{'symbol': '600519.SH', 'direction': 'BUY', 'price': 1800.0, 'shares': 100}]
        report = validator.validate_from_signals(sigs, broker)
        self.assertGreater(len(report.large_deviations), 0)

    def test_notes_populated_on_failure(self):
        broker = SimulatedBroker(SimConfig(
            initial_cash=5_000_000,
            price_source='manual',
            slippage_bps=500.0,
            enforce_lot=True,
        ))
        broker.connect()
        validator = PaperTradeValidator(threshold_bps=20.0)
        sigs = [{'symbol': '600519.SH', 'direction': 'BUY', 'price': 1800.0, 'shares': 100}]
        report = validator.validate_from_signals(sigs, broker)
        self.assertFalse(report.passed)
        self.assertGreater(len(report.notes), 0)


# ---------------------------------------------------------------------------
# 测试报告保存
# ---------------------------------------------------------------------------

class TestValidationReportSave(unittest.TestCase):

    def test_save_creates_file(self):
        import os, tempfile
        validator = PaperTradeValidator()
        broker = _broker()
        sigs = [{'symbol': '600519.SH', 'direction': 'BUY', 'price': 1800.0, 'shares': 100}]
        report = validator.validate_from_signals(sigs, broker)
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name
        try:
            saved = report.save(path)
            self.assertTrue(os.path.exists(saved))
            import json
            with open(saved, encoding='utf-8') as f:
                data = json.load(f)
            self.assertIn('summary', data)
            self.assertIn('n_trades', data['summary'])
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
