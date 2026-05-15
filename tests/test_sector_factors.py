"""
tests/test_sector_factors.py — 板块层因子单元测试

覆盖 (W3-1 / W3-2):
  - SectorFlowFactor: 显式注入 / Store 读取 / 降级 / 信号
  - SectorFlowStore: 持久化 / 读写 / series_for
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


def _make_price_df(n: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    close = 10.0 + np.cumsum(rng.normal(0, 0.2, n))
    return pd.DataFrame({
        'open': close, 'high': close * 1.01,
        'low': close * 0.99, 'close': close,
        'volume': rng.integers(1000, 100000, n).astype(float),
    }, index=dates)


class TestSectorFlowFactor(unittest.TestCase):
    """SectorFlowFactor 行为测试。"""

    def setUp(self):
        self.price = _make_price_df(60)

    def test_no_data_returns_zero(self):
        from core.factors.sector import SectorFlowFactor
        f = SectorFlowFactor()
        result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_injected_data_takes_priority(self):
        """显式注入 sector_flow_data 时直接使用。"""
        from core.factors.sector import SectorFlowFactor
        n = 60
        sf = pd.DataFrame({
            'net_flow': np.linspace(-1e8, 1e9, n),  # 持续流入
        }, index=self.price.index)
        f = SectorFlowFactor(
            sector_code='BK_TEST', sector_flow_data=sf, window=5,
        )
        result = f.evaluate(self.price)
        # 末段持续流入 → 因子值偏正
        self.assertGreater(result.iloc[-10:].mean(), result.iloc[:20].mean())

    def test_outflow_negative(self):
        from core.factors.sector import SectorFlowFactor
        n = 60
        sf = pd.DataFrame({
            'net_flow': np.linspace(1e9, -1e9, n),
        }, index=self.price.index)
        f = SectorFlowFactor(sector_flow_data=sf, window=5)
        result = f.evaluate(self.price)
        self.assertLess(result.iloc[-10:].mean(), result.iloc[:20].mean())

    def test_store_path_returns_zero_when_empty(self):
        """sector_code 给出但 Store 无数据 → 降级。"""
        from core.factors.sector import SectorFlowFactor
        with patch('core.factors.sector.SectorFlowStore') as MockStore:
            mock = MagicMock()
            mock.series_for.return_value = pd.Series(dtype=float)
            MockStore.return_value = mock
            f = SectorFlowFactor(sector_code='BK_NA')
            result = f.evaluate(self.price)
        self.assertTrue((result == 0).all())

    def test_store_path_loads_series(self):
        """sector_code 提供且 Store 有数据时,从 Store 读取并计算。"""
        from core.factors.sector import SectorFlowFactor
        idx = self.price.index
        ser = pd.Series(np.linspace(-5e8, 8e8, len(idx)), index=idx)
        with patch('core.factors.sector.SectorFlowStore') as MockStore:
            mock = MagicMock()
            mock.series_for.return_value = ser
            MockStore.return_value = mock
            f = SectorFlowFactor(sector_code='BK0716', window=5)
            result = f.evaluate(self.price)
        # 应为非零变化
        self.assertGreater(result.abs().sum(), 0)

    def test_signals_buy(self):
        from core.factors.sector import SectorFlowFactor
        vals = pd.Series([0.0] * 19 + [2.0])
        f = SectorFlowFactor(sector_code='BK0716', symbol='sh600519')
        sigs = f.signals(vals, price=100.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'BUY')
        self.assertEqual(sigs[0].metadata.get('sector_code'), 'BK0716')

    def test_signals_sell(self):
        from core.factors.sector import SectorFlowFactor
        vals = pd.Series([0.0] * 19 + [-2.0])
        f = SectorFlowFactor(sector_code='BK0716')
        sigs = f.signals(vals, price=100.0, threshold=1.0)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].direction, 'SELL')

    def test_registry_create(self):
        from core.factor_registry import registry
        f = registry.create('SectorFlow')
        self.assertEqual(f.name, 'SectorFlow')


class TestSectorFlowStore(unittest.TestCase):
    """SectorFlowStore 持久化测试。"""

    def setUp(self):
        # 使用临时文件,避免污染实际 Parquet
        self.tmpfile = tempfile.NamedTemporaryFile(
            suffix='.parquet', delete=False,
        ).name
        os.unlink(self.tmpfile)   # 删除空文件,让 Store 自行创建

    def tearDown(self):
        if os.path.exists(self.tmpfile):
            os.unlink(self.tmpfile)

    def test_read_returns_empty_when_no_file(self):
        from core.factors.sector import SectorFlowStore
        store = SectorFlowStore(parquet_path=self.tmpfile)
        df = store.read()
        self.assertTrue(df.empty)

    def test_update_today_persists_snapshot(self):
        from core.factors.sector import SectorFlowStore
        from core.data_gateway.schemas import SectorRanking

        sectors = [
            SectorRanking(code='BK0001', name='A', net_flow=1e8),
            SectorRanking(code='BK0002', name='B', net_flow=-2e8),
        ]
        gw_mock = MagicMock()
        gw_mock.sectors.return_value = sectors

        store = SectorFlowStore(parquet_path=self.tmpfile)
        with patch('core.data_gateway.get_gateway', return_value=gw_mock):
            store.update_today(limit=10)

        df = store.read()
        self.assertFalse(df.empty)
        self.assertIn('BK0001', df.columns)
        self.assertEqual(df['BK0001'].iloc[-1], 1e8)

    def test_update_today_replaces_same_day(self):
        from core.factors.sector import SectorFlowStore
        from core.data_gateway.schemas import SectorRanking

        store = SectorFlowStore(parquet_path=self.tmpfile)

        # 第一次写
        with patch('core.data_gateway.get_gateway', return_value=MagicMock(
            sectors=MagicMock(return_value=[SectorRanking(code='X', name='x', net_flow=1.0)])
        )):
            store.update_today()
        # 同日再写,值应被覆盖
        with patch('core.data_gateway.get_gateway', return_value=MagicMock(
            sectors=MagicMock(return_value=[SectorRanking(code='X', name='x', net_flow=99.0)])
        )):
            store.update_today()

        df = store.read()
        self.assertEqual(len(df), 1)
        self.assertEqual(df['X'].iloc[-1], 99.0)

    def test_series_for_returns_history(self):
        from core.factors.sector import SectorFlowStore

        # 直接构造 Parquet
        df_init = pd.DataFrame({'BK0001': [1.0, 2.0, 3.0]},
                               index=pd.to_datetime(['2026-05-12', '2026-05-13', '2026-05-14']))
        df_init.to_parquet(self.tmpfile)

        store = SectorFlowStore(parquet_path=self.tmpfile)
        ser = store.series_for('BK0001')
        self.assertEqual(len(ser), 3)
        self.assertEqual(ser.iloc[-1], 3.0)

    def test_series_for_unknown_sector_empty(self):
        from core.factors.sector import SectorFlowStore
        df_init = pd.DataFrame({'BK0001': [1.0]},
                               index=pd.to_datetime(['2026-05-14']))
        df_init.to_parquet(self.tmpfile)
        store = SectorFlowStore(parquet_path=self.tmpfile)
        ser = store.series_for('BK_NOT_EXIST')
        self.assertTrue(ser.empty)


if __name__ == '__main__':
    unittest.main()
