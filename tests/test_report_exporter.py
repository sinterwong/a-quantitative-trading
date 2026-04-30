"""
tests/test_report_exporter.py — 回测报告 PDF 导出测试

覆盖：
  - BacktestReportExporter.export(): 生成有效 PDF 文件
  - 携带 wfa_results 和 factor_ic 时能正常生成
  - equity_curve 为空时不抛异常
  - trades 为空时不抛异常
  - reportlab 缺失时抛出 ImportError（mock）
  - _fetch_factor_ic_snapshot 辅助（通过 factor_ic 参数传入）
  - PDF 文件大小 > 0
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Dict, List
from unittest.mock import patch

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 构造最小 BacktestResult
# ---------------------------------------------------------------------------

def _make_result(n_days: int = 120, n_trades: int = 10):
    """构造一个完整的 mock BacktestResult。"""
    from core.backtest_engine import BacktestResult, BacktestConfig, TradeRecord

    rng = np.random.default_rng(42)
    dates = pd.date_range('2023-01-01', periods=n_days, freq='B')
    equity = pd.Series(
        1.0 + np.cumsum(rng.normal(0.0005, 0.008, n_days)),
        index=dates,
    )

    # 构造 trades（使用实际的 TradeRecord 字段名）
    trades = []
    for i in range(n_trades):
        pnl_val = float(rng.normal(50, 200))
        tr = TradeRecord(
            timestamp=dates[i * max(n_days // max(n_trades, 1), 1)].to_pydatetime(),
            symbol='000001.SZ',
            direction='BUY' if i % 2 == 0 else 'SELL',
            price=10.0 + i * 0.1,
            shares=100,
            value=(10.0 + i * 0.1) * 100,
            commission=5.0,
            slippage_bps=5.0,
            signal_reason='test',
            signal_strength=0.6,
            holding_period=3600 * 24 * 3,
            pnl=pnl_val,
            realized_pnl=pnl_val,
        )
        trades.append(tr)

    cfg = BacktestConfig(initial_equity=100000)

    # 计算基础指标
    total_return = float(equity.iloc[-1] - 1.0)
    log_ret = np.log(equity / equity.shift(1)).dropna()
    annual_vol = float(log_ret.std() * np.sqrt(252))
    annual_return = float((equity.iloc[-1] ** (252 / n_days)) - 1.0)
    sharpe = annual_return / annual_vol if annual_vol > 0 else 0
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max
    max_dd = float(dd.min())

    result = BacktestResult(
        equity_curve=equity,
        daily_stats=[],
        trades=trades,
        positions={},
        config=cfg,
        total_days=n_days,
        n_trades=n_trades,
        total_return=total_return,
        annual_return=annual_return,
        annual_vol=annual_vol,
        sharpe=sharpe,
        max_drawdown=max_dd,
        max_drawdown_pct=abs(max_dd),
        win_rate=0.55,
        profit_factor=1.4,
        avg_holding_period=3600 * 24 * 3,
        calmar_ratio=annual_return / max(abs(max_dd), 1e-6),
        sortino_ratio=sharpe * 1.2,
        factor_ic=0.032,
        factor_ir=0.48,
    )
    return result


class TestExportBasic(unittest.TestCase):
    """基础 PDF 生成测试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.result = _make_result()

    def _pdf_path(self, name='test.pdf'):
        return os.path.join(self.tmp, name)

    def test_export_creates_file(self):
        from core.report_exporter import BacktestReportExporter
        path = self._pdf_path('basic.pdf')
        exporter = BacktestReportExporter(self.result)
        out = exporter.export(path)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 1024)  # > 1 KB

    def test_export_with_wfa_and_factor_ic(self):
        from core.report_exporter import BacktestReportExporter
        path = self._pdf_path('full.pdf')
        exporter = BacktestReportExporter(self.result, title='完整测试报告')
        wfa = {'train_sharpe': [0.9, 1.1, 0.8], 'test_sharpe': [0.7, 0.9, 0.6]}
        ic  = {
            'RSI':         {'ic_mean': 0.035, 'ic_ir': 0.52},
            'MACD':        {'ic_mean': 0.021, 'ic_ir': 0.33},
            'BollingerBand': {'ic_mean': -0.005, 'ic_ir': -0.08},
        }
        out = exporter.export(path, wfa_results=wfa, factor_ic=ic)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 2048)

    def test_returns_output_path(self):
        from core.report_exporter import BacktestReportExporter
        path = self._pdf_path('ret.pdf')
        exporter = BacktestReportExporter(self.result)
        returned = exporter.export(path)
        self.assertEqual(returned, path)

    def test_creates_output_directory(self):
        from core.report_exporter import BacktestReportExporter
        nested = os.path.join(self.tmp, 'a', 'b', 'c', 'report.pdf')
        exporter = BacktestReportExporter(self.result)
        exporter.export(nested)
        self.assertTrue(os.path.exists(nested))


class TestExportEdgeCases(unittest.TestCase):
    """边界情况测试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _pdf_path(self, name='test.pdf'):
        return os.path.join(self.tmp, name)

    def test_empty_equity_curve(self):
        """equity_curve 为空时不抛异常。"""
        from core.report_exporter import BacktestReportExporter
        result = _make_result(n_trades=0)
        result.equity_curve = pd.Series([], dtype=float)
        result.trades = []
        exporter = BacktestReportExporter(result)
        path = self._pdf_path('empty.pdf')
        exporter.export(path)           # should not raise
        self.assertTrue(os.path.exists(path))

    def test_no_trades(self):
        """无交易记录时不抛异常。"""
        from core.report_exporter import BacktestReportExporter
        result = _make_result(n_days=60, n_trades=0)
        result.trades = []
        exporter = BacktestReportExporter(result)
        path = self._pdf_path('notrades.pdf')
        exporter.export(path)
        self.assertTrue(os.path.exists(path))

    def test_missing_reportlab_raises(self):
        """reportlab 不可导入时抛出 ImportError。"""
        from core.report_exporter import BacktestReportExporter
        result = _make_result()
        exporter = BacktestReportExporter(result)
        with patch.dict('sys.modules', {'reportlab': None,
                                         'reportlab.lib.pagesizes': None,
                                         'reportlab.platypus': None}):
            with self.assertRaises((ImportError, TypeError)):
                exporter.export(self._pdf_path('fail.pdf'))


class TestHelpers(unittest.TestCase):
    """辅助函数测试。"""

    def test_color_sign_positive(self):
        from core.report_exporter import _color_sign, _GREEN
        self.assertEqual(_color_sign(0.5), _GREEN)

    def test_color_sign_negative(self):
        from core.report_exporter import _color_sign, _RED
        self.assertEqual(_color_sign(-0.1), _RED)

    def test_color_sign_zero(self):
        from core.report_exporter import _color_sign, _GREEN
        self.assertEqual(_color_sign(0.0), _GREEN)

    def test_hex_format(self):
        from core.report_exporter import _hex
        self.assertEqual(_hex(1.0, 1.0, 1.0), 'FFFFFF')
        self.assertEqual(_hex(0.0, 0.0, 0.0), '000000')


if __name__ == '__main__':
    unittest.main()
