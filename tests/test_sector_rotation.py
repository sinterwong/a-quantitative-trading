"""
tests/test_sector_rotation.py — SectorRotationStrategyV2 测试 (W3-3)

V1 (硬编码 ETF) 测试因依赖外部 OHLCV 数据集已不再覆盖;
本测试聚焦 V2 的数据驱动行为(纯 mock,无网络)。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestSectorRotationV2(unittest.TestCase):

    def _make_sector(self, code, name, change_pct, net_flow):
        from core.data_gateway.schemas import SectorRanking
        return SectorRanking(code=code, name=name,
                             change_pct=change_pct, net_flow=net_flow)

    def _make_constituent(self, symbol, change_pct=1.0, amount=1e9):
        from core.data_gateway.schemas import SectorConstituent
        return SectorConstituent(symbol=symbol, name='X', price=10.0,
                                 change_pct=change_pct, amount=amount)

    def test_score_sectors_combined_orders_correctly(self):
        from core.strategies.sector_rotation import SectorRotationStrategyV2
        s = SectorRotationStrategyV2()
        sectors = [
            self._make_sector('BK_A', '高流入', 1.0, net_flow=5e9),
            self._make_sector('BK_B', '中', 0.5, net_flow=0.0),
            self._make_sector('BK_C', '流出', -1.5, net_flow=-3e9),
        ]
        ranked = s._score_sectors(sectors)
        # 第一名应是高流入
        self.assertEqual(ranked[0]['code'], 'BK_A')
        # 最后是流出
        self.assertEqual(ranked[-1]['code'], 'BK_C')

    def test_score_sectors_flow_only(self):
        from core.strategies.sector_rotation import SectorRotationStrategyV2
        s = SectorRotationStrategyV2(sector_score_method='flow')
        sectors = [
            self._make_sector('A', 'A', 100.0, 0.0),     # 涨幅大但流出
            self._make_sector('B', 'B', -10.0, 5e9),     # 涨幅负但流入
        ]
        ranked = s._score_sectors(sectors)
        # 仅看 flow,B 应排第一
        self.assertEqual(ranked[0]['code'], 'B')

    def test_score_sectors_empty_input(self):
        from core.strategies.sector_rotation import SectorRotationStrategyV2
        s = SectorRotationStrategyV2()
        self.assertEqual(s._score_sectors([]), [])

    def test_latest_signal_returns_top_n_with_stocks(self):
        from core.strategies.sector_rotation import SectorRotationStrategyV2

        sectors = [
            self._make_sector('BK1', '板块1', 3.0, 5e9),
            self._make_sector('BK2', '板块2', 2.0, 3e9),
            self._make_sector('BK3', '板块3', 1.0, 1e9),
            self._make_sector('BK4', '板块4', -1.0, -1e9),
        ]

        # mock constituents:每个板块 2 个股
        def _consts(code, limit):
            return [
                self._make_constituent(f'sh_{code}_a', change_pct=2.0),
                self._make_constituent(f'sh_{code}_b', change_pct=1.5),
            ]

        gw = MagicMock()
        gw.sectors.return_value = sectors
        gw.sector_constituents.side_effect = _consts

        s = SectorRotationStrategyV2(top_sectors_n=2, stocks_per_sector=2)
        with patch('core.data_gateway.get_gateway', return_value=gw):
            sig = s.latest_signal()

        self.assertEqual(len(sig.top_sectors), 2)
        # buy_stocks 应来自 top 2 板块(各 2 个) = 4 个
        self.assertEqual(len(sig.buy_stocks), 4)
        # 每个 buy_stock 都应携带 sector_code
        for st in sig.buy_stocks:
            self.assertIn(st['sector_code'], ['BK1', 'BK2'])

    def test_latest_signal_handles_constituent_error(self):
        """单个板块成分股获取失败 → 其余板块仍正常输出。"""
        from core.strategies.sector_rotation import SectorRotationStrategyV2

        sectors = [
            self._make_sector('GOOD', 'OK', 2.0, 1e9),
            self._make_sector('BAD', 'X', 3.0, 2e9),  # 排名更高但成分股取失败
        ]

        def _consts(code, limit):
            if code == 'BAD':
                raise RuntimeError('mock fail')
            return [self._make_constituent('sh_good')]

        gw = MagicMock()
        gw.sectors.return_value = sectors
        gw.sector_constituents.side_effect = _consts

        s = SectorRotationStrategyV2(top_sectors_n=2, stocks_per_sector=1)
        with patch('core.data_gateway.get_gateway', return_value=gw):
            sig = s.latest_signal()

        # top_sectors 仍含两个板块(打分不依赖成分股)
        self.assertEqual(len(sig.top_sectors), 2)
        # 但 buy_stocks 只来自 GOOD
        codes = {s['sector_code'] for s in sig.buy_stocks}
        self.assertEqual(codes, {'GOOD'})

    def test_latest_signal_gateway_failure_returns_empty(self):
        from core.strategies.sector_rotation import SectorRotationStrategyV2

        gw = MagicMock()
        gw.sectors.side_effect = RuntimeError('net fail')
        s = SectorRotationStrategyV2()
        with patch('core.data_gateway.get_gateway', return_value=gw):
            sig = s.latest_signal()
        self.assertEqual(sig.buy_stocks, [])
        self.assertEqual(sig.universe_size, 0)


class TestSectorRotationV1BackwardCompat(unittest.TestCase):
    """V1 类仍可正常导入,不破坏既有调用方。"""

    def test_v1_class_importable(self):
        from core.strategies.sector_rotation import (
            SectorRotationStrategy, DEFAULT_SECTOR_ETFS,
        )
        s = SectorRotationStrategy(top_n=3)
        self.assertEqual(s.top_n, 3)
        self.assertTrue(len(DEFAULT_SECTOR_ETFS) > 0)


if __name__ == '__main__':
    unittest.main()
